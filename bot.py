from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional

from dotenv import load_dotenv
from telegram import Update, MessageEntity
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.helpers import escape_markdown

from hangman.core import HangmanGame, is_letter, normalize_letter, render_gallows

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hangman-bot")

# Активные игры по chat_id
GAMES: Dict[int, HangmanGame] = {}
# Признак активной игры в чате
ACTIVE_GAME: Dict[int, bool] = {}
# Ожидание слова: user_id -> target_chat_id
WAITING_WORD: Dict[int, int] = {}
# Попытки пользователей в текущих играх: chat_id -> user_id -> attempts
ATTEMPTS: Dict[int, Dict[int, int]] = {}

DB_PATH = os.getenv("HANGMAN_DB_PATH", "hangman.db")

def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db() -> None:
    with _db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS word_records (
                word TEXT PRIMARY KEY,
                best_user_id INTEGER,
                best_attempts INTEGER,
                worst_user_id INTEGER,
                worst_attempts INTEGER,
                total_games INTEGER NOT NULL
            )
            """
        )

def _upsert_user(conn: sqlite3.Connection, user) -> None:
    conn.execute(
        """
        INSERT INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
        """,
        (user.id, user.username, user.first_name),
    )

def _get_user_label(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute(
        "SELECT username, first_name FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row and row["username"]:
        return f"@{row['username']}"
    if row and row["first_name"]:
        return row["first_name"]
    return f"user {user_id}"

def _fetch_word_record(conn: sqlite3.Connection, word_key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT word, best_user_id, best_attempts, worst_user_id, worst_attempts, total_games
        FROM word_records
        WHERE word = ?
        """,
        (word_key,),
    ).fetchone()

def _update_word_record(
    conn: sqlite3.Connection,
    word_key: str,
    winner_user_id: Optional[int],
    winner_attempts: Optional[int],
    has_winner: bool,
) -> Dict[str, Optional[int]]:
    record = _fetch_word_record(conn, word_key)
    if record is None:
        best_user_id = winner_user_id if has_winner else None
        best_attempts = winner_attempts if has_winner else None
        worst_user_id = winner_user_id if has_winner else None
        worst_attempts = winner_attempts if has_winner else None
        total_games = 1
        conn.execute(
            """
            INSERT INTO word_records
            (word, best_user_id, best_attempts, worst_user_id, worst_attempts, total_games)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (word_key, best_user_id, best_attempts, worst_user_id, worst_attempts, total_games),
        )
        return {
            "best_user_id": best_user_id,
            "best_attempts": best_attempts,
            "worst_user_id": worst_user_id,
            "worst_attempts": worst_attempts,
            "total_games": total_games,
        }

    best_user_id = record["best_user_id"]
    best_attempts = record["best_attempts"]
    worst_user_id = record["worst_user_id"]
    worst_attempts = record["worst_attempts"]
    total_games = record["total_games"] + 1

    if has_winner and winner_attempts is not None:
        if best_attempts is None or winner_attempts < best_attempts:
            best_attempts = winner_attempts
            best_user_id = winner_user_id
        if worst_attempts is None or winner_attempts > worst_attempts:
            worst_attempts = winner_attempts
            worst_user_id = winner_user_id

    conn.execute(
        """
        UPDATE word_records
        SET best_user_id = ?, best_attempts = ?, worst_user_id = ?, worst_attempts = ?, total_games = ?
        WHERE word = ?
        """,
        (best_user_id, best_attempts, worst_user_id, worst_attempts, total_games, word_key),
    )
    return {
        "best_user_id": best_user_id,
        "best_attempts": best_attempts,
        "worst_user_id": worst_user_id,
        "worst_attempts": worst_attempts,
        "total_games": total_games,
    }

def _record_game_results(
    conn: sqlite3.Connection,
    chat_id: int,
    word_key: str,
    participants: Dict[int, int],
    winner_user_id: Optional[int],
) -> None:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for user_id, attempts in participants.items():
        result = "win" if winner_user_id is not None and user_id == winner_user_id else "lose"
        conn.execute(
            """
            INSERT INTO games (chat_id, word, user_id, attempts, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, word_key, user_id, attempts, result, created_at),
        )

def _increment_attempt(chat_id: int, user_id: int) -> None:
    ATTEMPTS.setdefault(chat_id, {})
    ATTEMPTS[chat_id][user_id] = ATTEMPTS[chat_id].get(user_id, 0) + 1

async def _post_game_stats(
    update: Update,
    word_display: str,
    winner_user_id: int,
    winner_attempts: int,
    previous_record: Optional[sqlite3.Row],
    updated_record: Dict[str, Optional[int]],
) -> None:
    if not previous_record or previous_record["best_attempts"] is None:
        return

    is_new_record = winner_attempts < previous_record["best_attempts"]
    word_label = word_display.upper()

    with _db_connect() as conn:
        winner_label = _get_user_label(conn, winner_user_id)
        if is_new_record:
            text = (
                "🎉 Новый рекорд!\n"
                f"{winner_label} угадал(а) слово «{word_label}» за {winner_attempts} попыток — быстрее всех!"
            )
            await update.message.reply_text(
                escape_markdown(text, 2),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        best_user_id = updated_record.get("best_user_id")
        worst_user_id = updated_record.get("worst_user_id")
        best_attempts = updated_record.get("best_attempts")
        worst_attempts = updated_record.get("worst_attempts")
        total_games = updated_record.get("total_games")

        if best_user_id is None or worst_user_id is None or best_attempts is None or worst_attempts is None:
            return

        champion = _get_user_label(conn, int(best_user_id))
        outsider = _get_user_label(conn, int(worst_user_id))

    text = "\n".join(
        [
            f"✅ Слово «{word_label}» угадано за {winner_attempts} попыток",
            f"🏆 Чемпион: {champion} — {best_attempts} попыток",
            f"🐌 Аутсайдер: {outsider} — {worst_attempts} попыток",
            f"📊 Всего игр с этим словом: {total_games}",
        ]
    )
    await update.message.reply_text(
        escape_markdown(text, 2),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

# --- Команды ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    user = update.effective_user
    if args and len(args) >= 1 and args[0].startswith("ask_"):
        try:
            chat_id = int(args[0].split("ask_", 1)[1])
        except ValueError:
            await update.message.reply_text("Некорректная ссылка. Попробуйте ещё раз из группы, упомянув бота и слово «загадать».")
            return
        WAITING_WORD[user.id] = chat_id
        await update.message.reply_text(
            "Введите ваше слово (буквы рус/лат, можно пробелы и дефисы). "
            "Я не покажу его в группе — только маску."
        )
        return

    await update.message.reply_text(
        "Привет! Чтобы начать игру в группе, напишите в том чате: «@<бот> загадать». "
        "Я пришлю ссылку сюда для ввода секретного слова."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Как играть:\n"
        "1) В группе напишите «@бот загадать».\n"
        "2) В личке введите слово.\n"
        "3) В группе угадывайте буквы или слово целиком, упоминая бота."
    )

# --- Обработка приватного ввода слова ---

def _sanitize_secret(s: str) -> str:
    # Удалим лишние пробелы по краям, заменим множественные пробелы на один.
    s = re.sub(r"\s+", " ", s.strip())
    # Проверка, что есть хотя бы одна буква.
    has_letter = any(is_letter(ch) for ch in s)
    return s if has_letter else ""

def _sanitize_guess(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    if not s:
        return ""
    for ch in s:
        if is_letter(ch) or ch in (" ", "-"):
            continue
        return ""
    has_letter = any(is_letter(ch) for ch in s)
    return s if has_letter else ""

def _normalize_phrase(s: str) -> str:
    return "".join(normalize_letter(ch) if is_letter(ch) else ch for ch in s)

def _extract_single_letter(text: str) -> Optional[str]:
    cleaned = text.strip()
    if len(cleaned) != 1:
        return None
    if not is_letter(cleaned):
        return None
    return normalize_letter(cleaned)

async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        return
    if user_id not in WAITING_WORD:
        await update.message.reply_text("Чтобы загадать слово в группе, сначала напишите в группе «@бот загадать».")
        return

    chat_id = WAITING_WORD[user_id]
    secret = _sanitize_secret(text)
    if not secret:
        await update.message.reply_text("Нужно ввести хотя бы одну букву (разрешены буквы, пробелы и дефисы). Попробуйте снова.")
        return

    # Заводим игру
    game = HangmanGame(chat_id=chat_id, secret=secret, host_user_id=user_id, max_attempts=6)
    GAMES[chat_id] = game
    ACTIVE_GAME[chat_id] = True
    ATTEMPTS[chat_id] = {}
    del WAITING_WORD[user_id]

    # Сообщение в группу
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{escape_markdown('🧩 Слово загадано!', 2)}\n{game.progress_message()}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await update.message.reply_text("Готово! Я объявил игру в группе. Пусть друзья начинают угадывать буквы 😊")
    except Exception as e:
        log.exception("Не удалось отправить сообщение в группу: %s", e)
        await update.message.reply_text(
            "Не удалось сообщить в группу. Убедитесь, что бот добавлен в тот чат и у него есть права писать сообщения."
        )

# --- Обработка упоминаний в группах ---

def _mentioned_this_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.entities:
        return False
    bot_username = context.bot.username.lower()
    for ent in update.message.entities:
        if ent.type == MessageEntity.MENTION:
            mention = update.message.text[ent.offset: ent.offset + ent.length]
            if mention.lower() == f"@{bot_username}":
                return True
    # На всякий случай fallback по строке
    return f"@{context.bot.username.lower()}" in (update.message.text or "").lower()

async def on_group_mention(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    msg = update.message
    text = (msg.text or "").strip()

    chat_id = update.effective_chat.id
    bot_username = context.bot.username
    # Ветка старта: достаточно просто упомянуть бота
    if _mentioned_this_bot(update, context) and not ACTIVE_GAME.get(chat_id):
        if chat_id in GAMES:
            await msg.reply_text(
                f"{escape_markdown('Игра уже идёт. Текущий прогресс ниже.', 2)}\n{GAMES[chat_id].progress_message()}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        deep_link = f"https://t.me/{bot_username}?start=ask_{chat_id}"
        await msg.reply_text(
            f"Кто загадывает слово — перейдите по ссылке в личку бота:\n{deep_link}\n"
            "Там введите секретное слово.",
            disable_web_page_preview=True,
        )
        return

    # Ветка игры во время активной сессии
    if ACTIVE_GAME.get(chat_id):
        game = GAMES.get(chat_id)
        if game is None:
            ACTIVE_GAME.pop(chat_id, None)
            return
        mentioned = _mentioned_this_bot(update, context)
        user = update.effective_user
        with _db_connect() as conn:
            _upsert_user(conn, user)

        if mentioned:
            # Уберём все упоминания ботов, чтобы не мешали парсингу
            cleaned = re.sub(r"@\w+", " ", text, flags=re.I)
            guess_text = _sanitize_guess(cleaned)
            if not guess_text:
                await msg.reply_text(
                    "Отправьте одну букву или слово целиком (разрешены буквы, пробелы и дефисы)."
                )
                return

            letters = [normalize_letter(ch) for ch in guess_text if is_letter(ch)]
            if len(letters) == 1 and len(guess_text) == 1:
                letter = letters[0]
                if game.already_tried(letter):
                    await msg.reply_text(
                        f"{escape_markdown(f'Буква «{letter}» уже называлась.', 2)}\n{game.progress_message()}",
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                    return

                _increment_attempt(chat_id, user.id)
                is_correct, is_win, is_lose = game.guess(letter)
            else:
                _increment_attempt(chat_id, user.id)
                is_win = _normalize_phrase(guess_text) == _normalize_phrase(game.secret)
                is_lose = not is_win
                is_correct = is_win
        else:
            # Без упоминания принимаем только одну букву
            letter = _extract_single_letter(text)
            if not letter:
                return
            if game.already_tried(letter):
                await msg.reply_text(
                    f"{escape_markdown(f'Буква «{letter}» уже называлась.', 2)}\n{game.progress_message()}",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return

            _increment_attempt(chat_id, user.id)
            is_correct, is_win, is_lose = game.guess(letter)

        if is_win:
            word_key = _normalize_phrase(game.secret)
            participants = ATTEMPTS.get(chat_id, {})
            winner_attempts = participants.get(user.id, 0)
            with _db_connect() as conn:
                previous_record = _fetch_word_record(conn, word_key)
                _record_game_results(conn, chat_id, word_key, participants, user.id)
                updated_record = _update_word_record(conn, word_key, user.id, winner_attempts, True)

            await msg.reply_text(
                f"{escape_markdown('🎉 Победа! Слово отгадано:', 2)}\n"
                f"`{game.secret}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await _post_game_stats(
                update=update,
                word_display=game.secret,
                winner_user_id=user.id,
                winner_attempts=winner_attempts,
                previous_record=previous_record,
                updated_record=updated_record,
            )
            del GAMES[chat_id]
            ACTIVE_GAME.pop(chat_id, None)
            ATTEMPTS.pop(chat_id, None)
            return

        if is_lose:
            word_key = _normalize_phrase(game.secret)
            participants = ATTEMPTS.get(chat_id, {})
            with _db_connect() as conn:
                _record_game_results(conn, chat_id, word_key, participants, None)
                _update_word_record(conn, word_key, None, None, False)
            await msg.reply_text(
                f"{escape_markdown('💀 Поражение. Вы повешены.', 2)}\n"
                f"{escape_markdown('Секретное слово было:', 2)} `{game.secret}`\n"
                f"```\n{render_gallows(game.max_attempts, game.max_attempts)}\n```",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            del GAMES[chat_id]
            ACTIVE_GAME.pop(chat_id, None)
            ATTEMPTS.pop(chat_id, None)
            return

        # Промежуточный прогресс
        if is_correct:
            await msg.reply_text(
                f"{escape_markdown('Есть такая буква!', 2)}\n{game.progress_message()}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await msg.reply_text(
                f"{escape_markdown('Мимо.', 2)}\n{game.progress_message()}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    if _mentioned_this_bot(update, context):
        await msg.reply_text("Сначала начните игру: упомяните бота и напишите «загадать».")
        return

# --- Запуск ---

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не найден в окружении. Создайте .env и задайте токен.")

    _init_db()
    app: Application = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # Приватные сообщения — ввод секретного слова
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, on_private_text))

    # Группы — любые тексты, но мы внутри проверим упоминание и логику
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_mention))

    log.info("Starting bot...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

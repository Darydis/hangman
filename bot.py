from __future__ import annotations

import logging
import os
import re
from typing import Dict

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
# Ожидание слова: user_id -> target_chat_id
WAITING_WORD: Dict[int, int] = {}

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

    if not _mentioned_this_bot(update, context):
        return

    chat_id = update.effective_chat.id
    bot_username = context.bot.username
    # Ветка "загадать": упоминание бота + слово "загадать"
    if re.search(r"\bзагадать\b", text, flags=re.IGNORECASE):
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

    # Ветка угадывания буквы
    if chat_id not in GAMES:
        await msg.reply_text("Сначала начните игру: упомяните бота и напишите «загадать».")
        return

    # Уберём все упоминания ботов, чтобы не мешали парсингу
    cleaned = re.sub(r"@\w+", " ", text, flags=re.I)
    guess_text = _sanitize_guess(cleaned)
    if not guess_text:
        await msg.reply_text(
            "Отправьте одну букву или слово целиком (разрешены буквы, пробелы и дефисы)."
        )
        return

    game = GAMES[chat_id]
    letters = [normalize_letter(ch) for ch in guess_text if is_letter(ch)]
    if len(letters) == 1 and len(guess_text) == 1:
        letter = letters[0]
        if game.already_tried(letter):
            await msg.reply_text(
                f"{escape_markdown(f'Буква «{letter}» уже называлась.', 2)}\n{game.progress_message()}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        is_correct, is_win, is_lose = game.guess(letter)
    else:
        is_win = _normalize_phrase(guess_text) == _normalize_phrase(game.secret)
        is_lose = not is_win
        is_correct = is_win

    if is_win:
        await msg.reply_text(
            f"{escape_markdown('🎉 Победа! Слово отгадано:', 2)}\n"
            f"`{game.secret}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        del GAMES[chat_id]
        return

    if is_lose:
        await msg.reply_text(
            f"{escape_markdown('💀 Поражение. Вы повешены.', 2)}\n"
            f"{escape_markdown('Секретное слово было:', 2)} `{game.secret}`\n"
            f"```\n{render_gallows(game.max_attempts, game.max_attempts)}\n```",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        del GAMES[chat_id]
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

# --- Запуск ---

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не найден в окружении. Создайте .env и задайте токен.")

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

from __future__ import annotations

import inspect
from collections import namedtuple

# Compatibility shim for pymorphy2 on Python 3.11+
if not hasattr(inspect, "getargspec"):
    ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")

    def _getargspec(func):  # type: ignore[override]
        spec = inspect.getfullargspec(func)
        return ArgSpec(spec.args, spec.varargs, spec.varkw, spec.defaults)

    inspect.getargspec = _getargspec  # type: ignore[attr-defined]

import asyncio
import logging
import os
import random
import re
import sqlite3
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from telegram import (
    Update,
    MessageEntity,
    BotCommand,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from wordfreq import top_n_list
import pymorphy2
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
)
from telegram.helpers import escape_markdown

from hangman.core import HangmanGame, is_letter, normalize_letter, render_gallows
from metrics import track_event

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
# Ожидание слова для ссылки: user_id -> True
WAITING_SHARED_WORD: Dict[int, bool] = {}
# Секреты для расшаренных ссылок: token -> secret
SHARED_WORDS: Dict[str, str] = {}
# Автор расшаренного слова: token -> user_id
SHARED_HOST: Dict[str, int] = {}
# Отгаданные расшаренные слова: token -> set(user_id)
SHARED_PLAYED: Dict[str, Set[int]] = {}
# Активная игра из расшаренной ссылки: chat_id -> token
GAME_SHARED_TOKEN: Dict[int, str] = {}
# Активная игра по слову дня: chat_id -> date_key
GAME_DAILY_DATE: Dict[int, str] = {}
# Ожидание оплаты дополнительной попытки: chat_id -> user_id
EXTRA_PAYMENT_PENDING: Dict[int, int] = {}
# Ожидание выбора после исчерпания попыток: chat_id -> user_id
LOSE_CHOICE_PENDING: Dict[int, int] = {}

EXTRA_ATTEMPT_PRICE = 5
EXTRA_ATTEMPT_CURRENCY = "XTR"
BUY_ATTEMPT_TEXT = "⭐ Купить попытку"
END_GAME_TEXT = "❌ Завершить"
NEW_GAME_TEXT = "🎮 Новая игра"
REPLAY_TEXT = "🎮 Сыграть еще раз"
BUY_ATTEMPT_CB = "buy_attempt"
END_GAME_CB = "end_game"
NEW_GAME_CB = "new_game"
REPLAY_CB = "replay_game"

FALLBACK_WORDS = [
    "абрикос",
    "автобус",
    "айсберг",
    "аквариум",
    "алмаз",
    "апельсин",
    "арбуз",
    "барабан",
    "билет",
    "бинокль",
    "блокнот",
    "букет",
    "вагон",
    "вертолёт",
    "вишня",
    "гитара",
    "глобус",
    "гриб",
    "дворец",
    "дневник",
    "дракон",
    "жираф",
    "журнал",
    "замок",
    "зонтик",
    "кабинет",
    "кактус",
    "карандаш",
    "карман",
    "карусель",
    "кафе",
    "кедр",
    "кит",
    "клавиша",
    "книга",
    "кнопка",
    "корабль",
    "космос",
    "котёл",
    "кровать",
    "крыльцо",
    "кукла",
    "лампа",
    "лес",
    "листва",
    "лифт",
    "медаль",
    "метеор",
    "молоко",
    "мост",
    "музей",
    "ножницы",
    "облако",
    "огурец",
    "окно",
    "олень",
    "пальто",
    "пароход",
    "паровоз",
    "пейзаж",
    "песок",
    "письмо",
    "планета",
    "платье",
    "плита",
    "площадь",
    "погода",
    "помидор",
    "потолок",
    "праздник",
    "пряник",
    "путешествие",
    "радио",
    "радуга",
    "ракушка",
    "редис",
    "рисунок",
    "робот",
    "ромашка",
    "самолёт",
    "сарай",
    "свеча",
    "сок",
    "солнце",
    "спорт",
    "стекло",
    "стул",
    "сумка",
    "суп",
    "сцена",
    "таблица",
    "телефон",
    "тетрадь",
    "трамвай",
    "труба",
    "тюльпан",
    "улитка",
    "фонарь",
    "хлеб",
    "хомяк",
    "холод",
    "цветок",
    "чайник",
    "шар",
    "шахматы",
    "школа",
    "шляпа",
    "шоколад",
    "щенок",
    "яблоко",
    "ягода",
]

PLAY_BUTTON_TEXT = "Play"
NOUN_WORDS: List[str] = []
MORPH = pymorphy2.MorphAnalyzer()
# Попытки пользователей в текущих играх: chat_id -> user_id -> attempts
ATTEMPTS: Dict[int, Dict[int, int]] = {}

DB_PATH = os.getenv("HANGMAN_DB_PATH", "hangman.db")

def _emit_event(
    event_name: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str | None = None,
    game_id: str | None = None,
    properties: dict | None = None,
    payment: dict | None = None,
) -> None:
    if not update.effective_user:
        return
    asyncio.create_task(
        track_event(
            event_name=event_name,
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            session_id=session_id,
            game_id=game_id,
            properties=properties,
            payment=payment,
        )
    )

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_words (
                day_key TEXT PRIMARY KEY,
                word TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_plays (
                day_key TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (day_key, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS play_history (
                word TEXT PRIMARY KEY,
                last_used_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS play_user_last (
                user_id INTEGER PRIMARY KEY,
                last_word TEXT NOT NULL
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

def _fetch_all_user_ids(conn: sqlite3.Connection) -> List[int]:
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    return [int(row["user_id"]) for row in rows if row["user_id"] is not None]

def _get_daily_word(conn: sqlite3.Connection, day_key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT word FROM daily_words WHERE day_key = ?",
        (day_key,),
    ).fetchone()
    return row["word"] if row else None

def _set_daily_word(conn: sqlite3.Connection, day_key: str, word_key: str) -> None:
    conn.execute(
        """
        INSERT INTO daily_words (day_key, word)
        VALUES (?, ?)
        ON CONFLICT(day_key) DO UPDATE SET word=excluded.word
        """,
        (day_key, word_key),
    )

def _fetch_daily_words(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT word FROM daily_words").fetchall()
    return [row["word"] for row in rows if row["word"]]

def _fetch_known_words(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT word FROM word_records").fetchall()
    return [row["word"] for row in rows if row["word"]]

def _fetch_user_guessed_words(conn: sqlite3.Connection, user_id: int) -> Set[str]:
    rows = conn.execute(
        "SELECT DISTINCT word FROM games WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return {_normalize_game_word(row["word"]) for row in rows if row["word"]}

def _day_key_from_date(value: datetime) -> str:
    return value.strftime("%Y%m%d")

def _choose_daily_word(conn: sqlite3.Connection, day_key: str) -> str:
    raw_words = _fetch_known_words(conn) + _fetch_daily_words(conn)
    known_words = list(dict.fromkeys(_normalize_game_word(word) for word in raw_words if word))
    pool = known_words if known_words else [_normalize_game_word(word) for word in FALLBACK_WORDS]
    if not pool:
        return "слово"
    used_daily = {_normalize_game_word(w) for w in _fetch_daily_words(conn)}
    candidates = [w for w in pool if w not in used_daily] or pool
    return random.choice(candidates)

def _choose_play_word(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    global NOUN_WORDS
    if not NOUN_WORDS:
        # Берем частотные слова и фильтруем по существительным
        candidates = top_n_list("ru", 5000)
        nouns: List[str] = []
        for word in candidates:
            if not word or len(word) < 3:
                continue
            if not all(is_letter(ch) for ch in word):
                continue
            parse = MORPH.parse(word)
            if parse and parse[0].tag.POS == "NOUN":
                nouns.append(_normalize_game_word(word))
        NOUN_WORDS = nouns or [_normalize_game_word(word) for word in FALLBACK_WORDS]

    raw_words = _fetch_known_words(conn) + _fetch_daily_words(conn)
    known_words = list(dict.fromkeys(_normalize_game_word(word) for word in raw_words if word))
    if not known_words and not NOUN_WORDS:
        return None
    history = _fetch_play_history(conn)
    user_guessed = _fetch_user_guessed_words(conn, user_id)
    last_word = _fetch_last_play_word(conn, user_id)

    def _pick_from(pool: List[str], allow_last_word: bool) -> Optional[str]:
        available = [word for word in pool if word not in user_guessed]
        if not available:
            return None
        if last_word:
            if len(available) == 1 and available[0] == last_word and not allow_last_word:
                return None
            if len(available) > 1:
                available = [word for word in available if word != last_word] or available
        unseen = [word for word in available if word not in history]
        if unseen:
            return random.choice(unseen)
        sorted_by_age = sorted(
            available,
            key=lambda word: history.get(word, ""),
        )
        oldest_timestamp = history.get(sorted_by_age[0], "")
        oldest_candidates = [word for word in sorted_by_age if history.get(word, "") == oldest_timestamp]
        return random.choice(oldest_candidates) if oldest_candidates else random.choice(available)

    # Приоритет: слова, которые уже загадывали другие игроки, затем словарь.
    # Если оба пула закончились — разрешаем повтор последнего слова.
    return (
        _pick_from(known_words, allow_last_word=False)
        or _pick_from(NOUN_WORDS, allow_last_word=False)
        or _pick_from(known_words, allow_last_word=True)
        or _pick_from(NOUN_WORDS, allow_last_word=True)
    )

def _has_daily_play(conn: sqlite3.Connection, day_key: str, user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM daily_plays WHERE day_key = ? AND user_id = ?",
        (day_key, user_id),
    ).fetchone()
    return row is not None

def _record_daily_play(conn: sqlite3.Connection, day_key: str, user_id: int) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO daily_plays (day_key, user_id)
        VALUES (?, ?)
        """,
        (day_key, user_id),
    )

def _fetch_play_history(conn: sqlite3.Connection) -> Dict[str, str]:
    rows = conn.execute("SELECT word, last_used_at FROM play_history").fetchall()
    return {row["word"]: row["last_used_at"] for row in rows if row["word"] and row["last_used_at"]}

def _record_play_word(conn: sqlite3.Connection, word: str) -> None:
    conn.execute(
        """
        INSERT INTO play_history (word, last_used_at)
        VALUES (?, ?)
        ON CONFLICT(word) DO UPDATE SET last_used_at=excluded.last_used_at
        """,
        (_normalize_game_word(word), datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )

def _fetch_last_play_word(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT last_word FROM play_user_last WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return row["last_word"] if row else None

def _record_last_play_word(conn: sqlite3.Connection, user_id: int, word: str) -> None:
    conn.execute(
        """
        INSERT INTO play_user_last (user_id, last_word)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_word=excluded.last_word
        """,
        (user_id, _normalize_game_word(word)),
    )

def _today_key() -> str:
    return _day_key_from_date(datetime.now(timezone.utc))

def _replay_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(REPLAY_TEXT, callback_data=REPLAY_CB)]]
    )

def _lose_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(BUY_ATTEMPT_TEXT, callback_data=BUY_ATTEMPT_CB)],
            [InlineKeyboardButton(END_GAME_TEXT, callback_data=END_GAME_CB)],
            [InlineKeyboardButton(NEW_GAME_TEXT, callback_data=NEW_GAME_CB)],
        ]
    )

async def _prompt_lose_choice(update: Update) -> None:
    await update.effective_message.reply_text(
        "Ты использовал(а) все попытки 😬\n\n"
        "Хочешь продолжить игру?\n\n"
        "⭐ +1 попытка — 5 Stars\n"
        "❌ Узнать слово\n"
        "🎮 Новая игра",
        reply_markup=_lose_choice_keyboard(),
    )

def _buy_attempt_link(bot_username: str, chat_id: int) -> str:
    return f"https://t.me/{bot_username}?start=buy_{chat_id}"

async def _prompt_group_lose_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    bot_username = context.bot.username
    buy_link = _buy_attempt_link(bot_username, chat_id)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(BUY_ATTEMPT_TEXT, url=buy_link)],
            [InlineKeyboardButton(END_GAME_TEXT, callback_data=END_GAME_CB)],
            [InlineKeyboardButton(NEW_GAME_TEXT, callback_data=NEW_GAME_CB)],
        ]
    )
    await update.effective_message.reply_text(
        "Ты использовал(а) все попытки 😬\n\n"
        "Хочешь продолжить игру?\n\n"
        "⭐ +1 попытка — 5 Stars\n"
        "❌ Завершить игру\n"
        "🎮 Новая игра",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

async def _offer_extra_attempt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    game_chat_id: int,
    user_id: int,
    invoice_chat_id: Optional[int] = None,
) -> bool:
    if EXTRA_PAYMENT_PENDING.get(game_chat_id) == user_id and invoice_chat_id is None:
        await _prompt_lose_choice(update)
        return True

    provider_token = os.getenv("TELEGRAM_PROVIDER_TOKEN")
    if provider_token is None:
        await update.message.reply_text("Платежи не настроены. Попытки закончились.")
        return False

    payload = f"extra_attempt:{game_chat_id}:{user_id}"
    prices = [LabeledPrice("Дополнительная попытка", EXTRA_ATTEMPT_PRICE)]
    EXTRA_PAYMENT_PENDING[game_chat_id] = user_id
    target_chat_id = invoice_chat_id or update.effective_chat.id
    try:
        await context.bot.send_invoice(
            chat_id=target_chat_id,
            title="Дополнительная попытка",
            description="Плюс 1 попытка в текущей игре",
            payload=payload,
            provider_token=provider_token,
            currency=EXTRA_ATTEMPT_CURRENCY,
            prices=prices,
        )
        _emit_event(
            "invoice_sent",
            update,
            context,
            session_id=str(game_chat_id),
            game_id=str(game_chat_id),
            payment={
                "type": "stars",
                "amount": EXTRA_ATTEMPT_PRICE,
                "currency": EXTRA_ATTEMPT_CURRENCY,
                "invoice_id": payload,
                "status": "sent",
            },
        )
    except Exception as exc:
        EXTRA_PAYMENT_PENDING.pop(game_chat_id, None)
        await update.effective_message.reply_text(
            "Не удалось создать счет на оплату.\n"
            "Проверьте, что Stars доступны для бота, и попробуйте еще раз."
        )
        log.exception("Не удалось создать счет Stars: %s", exc)
        return False
    return True

async def _finalize_loss(
    update: Update,
    chat_id: int,
    game: HangmanGame,
    show_replay: bool = True,
) -> None:
    word_key = _normalize_game_word(game.secret)
    participants = ATTEMPTS.get(chat_id, {})
    with _db_connect() as conn:
        _record_game_results(conn, chat_id, word_key, participants, None)
        updated_record = _update_word_record(conn, word_key, None, None, False)

    display_word = _to_nominative_phrase(game.secret)
    await update.effective_message.reply_text(
        f"{escape_markdown('💀 Поражение. Вы повешены.', 2)}\n"
        f"{escape_markdown('Секретное слово было:', 2)} `{display_word}`\n"
        f"```\n{render_gallows(game.max_attempts, game.max_attempts)}\n```",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await _post_word_stats(
        update=update,
        word_display=game.secret,
        updated_record=updated_record,
    )
    if show_replay:
        await update.effective_message.reply_text(
            "Сыграть еще раз?",
            reply_markup=_replay_keyboard(),
        )

    del GAMES[chat_id]
    ACTIVE_GAME.pop(chat_id, None)
    ATTEMPTS.pop(chat_id, None)
    GAME_SHARED_TOKEN.pop(chat_id, None)
    GAME_DAILY_DATE.pop(chat_id, None)
    EXTRA_PAYMENT_PENDING.pop(chat_id, None)
    LOSE_CHOICE_PENDING.pop(chat_id, None)

def _fetch_word_leaderboard(conn: sqlite3.Connection, word_key: str) -> List[Tuple[int, int]]:
    rows = conn.execute(
        """
        SELECT user_id, MIN(attempts) AS best_attempts
        FROM games
        WHERE word = ? AND result = 'win'
        GROUP BY user_id
        """,
        (word_key,),
    ).fetchall()
    return sorted(
        [(int(row["user_id"]), int(row["best_attempts"])) for row in rows if row["best_attempts"] is not None],
        key=lambda item: item[1],
    )

def _count_word_players(conn: sqlite3.Connection, word_key: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS cnt FROM games WHERE word = ?",
        (word_key,),
    ).fetchone()
    return int(row["cnt"]) if row and row["cnt"] is not None else 0

def _count_word_losers(conn: sqlite3.Connection, word_key: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS cnt FROM games WHERE word = ? AND result = 'lose'",
        (word_key,),
    ).fetchone()
    return int(row["cnt"]) if row and row["cnt"] is not None else 0

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

    word_label = word_display.upper()

    with _db_connect() as conn:
        best_user_id = updated_record.get("best_user_id")
        worst_user_id = updated_record.get("worst_user_id")
        best_attempts = updated_record.get("best_attempts")
        worst_attempts = updated_record.get("worst_attempts")
        total_games = updated_record.get("total_games")
        if total_games is None or total_games <= 1:
            return

        if best_user_id is None or worst_user_id is None or best_attempts is None or worst_attempts is None:
            return

        champion = _get_user_label(conn, int(best_user_id))
        outsider = _get_user_label(conn, int(worst_user_id))

    lines = [
        f"✅ Слово «{word_label}» угадано — {winner_attempts} {_format_attempts(winner_attempts)}",
        f"🏆 Чемпион: {champion} — {best_attempts} {_format_attempts(best_attempts)}",
    ]
    if best_user_id != worst_user_id:
        lines.append(f"🐌 Аутсайдер: {outsider} — {worst_attempts} {_format_attempts(worst_attempts)}")
    lines.append(f"📊 Всего игр с этим словом: {total_games}")
    text = "\n".join(lines)
    await update.message.reply_text(
        escape_markdown(text, 2),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def _post_word_stats(
    update: Update,
    word_display: str,
    updated_record: Dict[str, Optional[int]],
) -> None:
    total_games = updated_record.get("total_games")
    if total_games is None or total_games <= 0:
        return

    best_user_id = updated_record.get("best_user_id")
    worst_user_id = updated_record.get("worst_user_id")
    best_attempts = updated_record.get("best_attempts")
    worst_attempts = updated_record.get("worst_attempts")

    with _db_connect() as conn:
        champion = _get_user_label(conn, int(best_user_id)) if best_user_id is not None else "—"
        outsider = _get_user_label(conn, int(worst_user_id)) if worst_user_id is not None else "—"

    word_label = word_display.upper()
    lines = [f"📊 Статистика по слову «{word_label}»"]
    if best_attempts is not None and best_user_id is not None:
        lines.append(f"🏆 Чемпион: {champion} — {best_attempts} {_format_attempts(best_attempts)}")
    if worst_attempts is not None and worst_user_id is not None and worst_user_id != best_user_id:
        lines.append(f"🐌 Аутсайдер: {outsider} — {worst_attempts} {_format_attempts(worst_attempts)}")
    lines.append(f"📊 Всего игр с этим словом: {total_games}")
    await update.effective_message.reply_text(
        escape_markdown("\n".join(lines), 2),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def _send_daily_word(context: ContextTypes.DEFAULT_TYPE) -> None:
    day_key = _today_key()
    with _db_connect() as conn:
        word_key = _get_daily_word(conn, day_key)
        if not word_key:
            word_key = _choose_daily_word(conn, day_key)
            _set_daily_word(conn, day_key, word_key)
        user_ids = _fetch_all_user_ids(conn)

    bot_username = context.bot.username
    deep_link = f"https://t.me/{bot_username}?start=day_{day_key}"
    text = "\n".join(
        [
            "🍀 Слово дня!",
            "Нажмите, чтобы начать игру в личке с ботом:",
            deep_link,
        ]
    )
    for user_id in user_ids:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.warning("Не удалось отправить слово дня пользователю %s: %s", user_id, exc)

# --- Команды ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    user = update.effective_user
    _emit_event(
        "bot_open",
        update,
        context,
        session_id=str(update.effective_chat.id),
        properties={"args": args},
    )
    _emit_event(
        "command_start",
        update,
        context,
        session_id=str(update.effective_chat.id),
    )
    with _db_connect() as conn:
        _upsert_user(conn, user)
    if args and len(args) >= 1 and args[0].startswith("ask_"):
        try:
            chat_id = int(args[0].split("ask_", 1)[1])
        except ValueError:
            await update.message.reply_text("Некорректная ссылка. Попробуйте ещё раз из группы, просто упомянув бота.")
            return
        WAITING_WORD[user.id] = chat_id
        await update.message.reply_text(
            "Введите ваше слово (буквы рус/лат, можно пробелы и дефисы). "
            "Я не покажу его в группе — только маску."
        )
        return
    if args and len(args) >= 1 and args[0].startswith("play_"):
        token = args[0].split("play_", 1)[1]
        secret = SHARED_WORDS.get(token)
        if not secret:
            await update.message.reply_text("Ссылка не найдена или устарела.")
            return
        chat_id = update.effective_chat.id
        if SHARED_HOST.get(token) == user.id:
            await update.message.reply_text("Вы не можете отгадывать своё слово по этой ссылке.")
            return
        if user.id in SHARED_PLAYED.get(token, set()):
            await update.message.reply_text("✅ Вы уже отгадали это слово по этой ссылке.")
            return
        if ACTIVE_GAME.get(chat_id):
            await update.message.reply_text("У вас уже идёт игра. Сначала завершите её.")
            return
        _start_game(chat_id=chat_id, secret=secret, host_user_id=user.id)
        GAME_SHARED_TOKEN[chat_id] = token
        _emit_event(
            "game_start",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
            properties={"mode": "shared_link", "secret_length": len(secret)},
        )
        await update.message.reply_text(
            f"{escape_markdown('🧩 Игра началась!', 2)}\n{GAMES[chat_id].progress_message()}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    if args and len(args) >= 1 and args[0].startswith("day_"):
        day_key = args[0].split("day_", 1)[1]
        chat_id = update.effective_chat.id
        with _db_connect() as conn:
            word_key = _get_daily_word(conn, day_key)
            if not word_key:
                await update.message.reply_text("Слово дня недоступно.")
                return
            word_key = _normalize_game_word(word_key)
            if _has_daily_play(conn, day_key, user.id):
                await update.message.reply_text("✅ Вы уже отгадали слово дня.")
                return
        if ACTIVE_GAME.get(chat_id):
            await update.message.reply_text("У вас уже идёт игра. Сначала завершите её.")
            return
        _start_game(chat_id=chat_id, secret=word_key, host_user_id=user.id)
        GAME_DAILY_DATE[chat_id] = day_key
        _emit_event(
            "game_start",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
            properties={"mode": "daily_deeplink", "secret_length": len(word_key)},
        )
        await update.message.reply_text(
            f"{escape_markdown('🧩 Игра началась!', 2)}\n{GAMES[chat_id].progress_message()}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    if args and len(args) >= 1 and args[0] == "playbot":
        await _start_bot_game(update, context)
        return
    if args and len(args) >= 1 and args[0].startswith("buy_"):
        try:
            game_chat_id = int(args[0].split("buy_", 1)[1])
        except ValueError:
            await update.message.reply_text("Некорректная ссылка на оплату.")
            return
        game = GAMES.get(game_chat_id)
        if not game or not game.is_lose():
            await update.message.reply_text("Оплата больше не актуальна.")
            return
        participants = ATTEMPTS.get(game_chat_id, {})
        if user.id not in participants:
            await update.message.reply_text("Оплата доступна только участникам этой игры.")
            return
        await _offer_extra_attempt(
            update=update,
            context=context,
            game_chat_id=game_chat_id,
            user_id=user.id,
            invoice_chat_id=update.effective_chat.id,
        )
        return

    await update.message.reply_text(
        "🎮 Как играть в «Виселицу»\n\n"
"У бота есть 3 режима игры и дополнительные функции:\n\n"

"🧩 1. Загадать слово для других\n"
"Если хочешь, чтобы другие люди угадывали твое слово:\n\n"
"1) Напиши в боте: /share или «загадать»\n"
"2) Отправь слово\n"
"3) Получи ссылку\n"
"4) Отправь ссылку друзьям или в чат\n\n"
"👉 По этой ссылке люди откроют бота и начнут угадывать слово.\n\n"

"💬 2. Загадать слово прямо в чате\n"
"Если игра идет в группе / чате:\n\n"
"1) Упомяни бота: @vicilitsa_bot\n"
"2) Бот пришлет ссылку для ввода слова\n"
"3) Введи слово\n"
"4) Отправь полученную ссылку в этот же чат\n\n"

"🤖 3. Играть с самим ботом\n"
"Если хочешь просто поиграть:\n"
"/play или кнопка Play\n\n"

"🌟 Дополнительно\n\n"
"Слово дня:\n"
"/daily — одно общее слово для всех на сегодня.\n\n"
"Статистика по слову:\n"
"/stats <слово> — кто угадывал, за сколько попыток, рекорды.",
        reply_markup=ReplyKeyboardRemove(),
    )



async def cmd_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        chat_id = update.effective_chat.id
        bot_username = context.bot.username
        if chat_id in GAMES:
            await update.message.reply_text(
                f"{escape_markdown('Игра уже идёт. Текущий прогресс ниже.', 2)}\n{GAMES[chat_id].progress_message()}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        deep_link = f"https://t.me/{bot_username}?start=ask_{chat_id}"
        await update.message.reply_text(
            f"Кто загадывает слово — перейдите по ссылке в личку бота:\n{deep_link}\n"
            "Там введите секретное слово.",
            disable_web_page_preview=True,
        )
        return
    with _db_connect() as conn:
        _upsert_user(conn, update.effective_user)
    WAITING_SHARED_WORD[update.effective_user.id] = True
    await update.message.reply_text(
        "Введите секретное слово для ссылки (буквы рус/лат, можно пробелы и дефисы)."
    )

async def _start_bot_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=playbot"
        await update.effective_message.reply_text(
            "Играть с ботом можно в личке. Откройте чат по ссылке:\n"
            f"{deep_link}",
            disable_web_page_preview=True,
        )
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    if LOSE_CHOICE_PENDING.get(chat_id) == user.id:
        game = GAMES.get(chat_id)
        if game:
            await _finalize_loss(update, chat_id, game, show_replay=False)
    with _db_connect() as conn:
        _upsert_user(conn, user)
        word_key = _choose_play_word(conn, user.id)
        word_key = _normalize_game_word(word_key) if word_key else None
        if not word_key:
            await update.effective_message.reply_text("Сейчас нет доступных слов. Попробуйте позже.")
            return
        _record_play_word(conn, word_key)
        _record_last_play_word(conn, user.id, word_key)
    if ACTIVE_GAME.get(chat_id):
        await update.effective_message.reply_text("У вас уже идёт игра. Сначала завершите её.")
        return
    _start_game(chat_id=chat_id, secret=word_key, host_user_id=user.id)
    _emit_event(
        "game_start",
        update,
        context,
        session_id=str(chat_id),
        game_id=str(chat_id),
        properties={"mode": "private_play", "secret_length": len(word_key)},
    )
    await update.effective_message.reply_text(
        f"{escape_markdown('🧩 Игра началась!', 2)}\n{GAMES[chat_id].progress_message()}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def _start_group_play_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("Команда доступна только в групповых чатах.")
        return
    chat_id = update.effective_chat.id
    if ACTIVE_GAME.get(chat_id):
        await update.effective_message.reply_text("Игра уже идёт в этом чате.")
        return
    user = update.effective_user
    with _db_connect() as conn:
        _upsert_user(conn, user)
        word_key = _choose_play_word(conn, user.id)
        word_key = _normalize_game_word(word_key) if word_key else None
        if not word_key:
            await update.effective_message.reply_text("Сейчас нет доступных слов. Попробуйте позже.")
            return
        _record_play_word(conn, word_key)
        _record_last_play_word(conn, user.id, word_key)
    _start_game(chat_id=chat_id, secret=word_key, host_user_id=user.id)
    _emit_event(
        "game_start",
        update,
        context,
        session_id=str(chat_id),
        game_id=str(chat_id),
        properties={"mode": "group_play", "secret_length": len(word_key)},
    )
    await update.effective_message.reply_text(
        f"{escape_markdown('🧩 Игра началась!', 2)}\n{GAMES[chat_id].progress_message()}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=ReplyKeyboardRemove(),
    )

async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _emit_event(
        "command_play",
        update,
        context,
        session_id=str(update.effective_chat.id),
    )
    if update.effective_chat.type == ChatType.PRIVATE:
        await _start_bot_game(update, context)
        return
    await _start_group_play_game(update, context)

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Команда доступна только в личных сообщениях с ботом.")
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    with _db_connect() as conn:
        _upsert_user(conn, user)
        day_key = _today_key()
        word_key = _get_daily_word(conn, day_key)
        if not word_key:
            word_key = _choose_daily_word(conn, day_key)
            _set_daily_word(conn, day_key, word_key)
        word_key = _normalize_game_word(word_key)
        if _has_daily_play(conn, day_key, user.id):
            await update.message.reply_text("✅ Вы уже отгадали слово дня.")
            return
    if ACTIVE_GAME.get(chat_id):
        await update.message.reply_text("У вас уже идёт игра. Сначала завершите её.")
        return
    _start_game(chat_id=chat_id, secret=word_key, host_user_id=user.id)
    GAME_DAILY_DATE[chat_id] = day_key
    _emit_event(
        "game_start",
        update,
        context,
        session_id=str(chat_id),
        game_id=str(chat_id),
        properties={"mode": "daily", "secret_length": len(word_key)},
    )
    await update.message.reply_text(
        f"{escape_markdown('🧩 Игра началась!', 2)}\n{GAMES[chat_id].progress_message()}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _emit_event(
        "command_stats",
        update,
        context,
        session_id=str(update.effective_chat.id),
    )
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /stats <слово>")
        return
    with _db_connect() as conn:
        _upsert_user(conn, update.effective_user)
    raw_word = " ".join(args).strip()
    word = _sanitize_secret(raw_word)
    if not word:
        await update.message.reply_text("Нужно ввести слово (разрешены буквы, пробелы и дефисы).")
        return

    word_key = _normalize_phrase(word)
    with _db_connect() as conn:
        record = _fetch_word_record(conn, word_key)
        if record is None:
            await update.message.reply_text("Статистика по этому слову ещё не собиралась.")
            return
        best_attempts = record["best_attempts"]
        best_user_id = record["best_user_id"]
        total_games = record["total_games"]
        total_players = _count_word_players(conn, word_key)
        total_losers = _count_word_losers(conn, word_key)
        best_label = _get_user_label(conn, int(best_user_id)) if best_user_id is not None else "—"

    text = "\n".join(
        [
            f"Статистика по слову «{word}»:",
            f"🏆 Рекорд: {best_attempts} {_format_attempts(best_attempts)} ({best_label})" if best_attempts is not None else "🏆 Рекорд: —",
            f"💀 Не справились: {total_losers}",
            f"👥 Игроков: {total_players}",
        ]
    )
    await update.message.reply_text(
        escape_markdown(text, 2),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if update.effective_chat:
        _emit_event(
            "invoice_opened",
            update,
            context,
            session_id=str(update.effective_chat.id),
            properties={"invoice_id": query.invoice_payload},
        )
    if query.invoice_payload.startswith("extra_attempt:"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Некорректный платеж.")

async def on_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    if not payload.startswith("extra_attempt:"):
        return
    _, chat_id_str, user_id_str = payload.split(":", 2)
    chat_id = int(chat_id_str)
    user_id = int(user_id_str)
    if update.effective_user.id != user_id:
        return

    _emit_event(
        "payment_success",
        update,
        context,
        session_id=str(chat_id),
        game_id=str(chat_id),
        payment={
            "type": "stars",
            "amount": payment.total_amount,
            "currency": payment.currency,
            "invoice_id": payload,
            "status": "success",
        },
    )
    _emit_event(
        "extra_attempt_bought",
        update,
        context,
        session_id=str(chat_id),
        game_id=str(chat_id),
    )

    game = GAMES.get(chat_id)
    EXTRA_PAYMENT_PENDING.pop(chat_id, None)
    LOSE_CHOICE_PENDING.pop(chat_id, None)
    if not game:
        await update.message.reply_text("Игра не найдена.")
        return

    game.max_attempts += 1
    await update.message.reply_text(
        f"{escape_markdown('✅ Добавлена 1 попытка. Продолжаем!', 2)}\n{game.progress_message()}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    if update.effective_chat.id != chat_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{escape_markdown('✅ Добавлена 1 попытка. Продолжаем!', 2)}\n{game.progress_message()}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

async def on_lose_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    game = GAMES.get(chat_id)
    if not game:
        await query.message.reply_text("Игра не найдена.")
        return

    if update.effective_chat.type == ChatType.PRIVATE:
        if LOSE_CHOICE_PENDING.get(chat_id) != user_id:
            await query.message.reply_text("Игра уже завершена или не найдена.")
            return

    if query.data == BUY_ATTEMPT_CB:
        await _offer_extra_attempt(update, context, chat_id, user_id)
        return
    if query.data == END_GAME_CB:
        await _finalize_loss(update, chat_id, game)
        return
    if query.data == NEW_GAME_CB:
        _emit_event(
            "new_game_clicked",
            update,
            context,
            session_id=str(chat_id),
        )
        await _finalize_loss(update, chat_id, game, show_replay=False)
        if update.effective_chat.type == ChatType.PRIVATE:
            await _start_bot_game(update, context)
        else:
            await _start_group_play_game(update, context)
        return

async def on_replay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _emit_event(
        "new_game_clicked",
        update,
        context,
        session_id=str(update.effective_chat.id),
    )
    if update.effective_chat.type == ChatType.PRIVATE:
        await _start_bot_game(update, context)
        return
    await _start_group_play_game(update, context)

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

def _format_attempts(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "промахов"
    last = count % 10
    if last == 1:
        return "промах"
    if 2 <= last <= 4:
        return "промаха"
    return "промахов"

def _to_nominative_phrase(phrase: str) -> str:
    tokens = re.split(r"(\s+|-)", phrase)
    normalized: List[str] = []
    for token in tokens:
        if not token or token.isspace() or token == "-":
            normalized.append(token)
            continue
        if all(is_letter(ch) for ch in token):
            parsed = MORPH.parse(token)
            noun = next((p for p in parsed if p.tag.POS == "NOUN"), None)
            normalized.append(noun.normal_form if noun else parsed[0].normal_form)
        else:
            normalized.append(token)
    return "".join(normalized)

def _normalize_game_word(word: str) -> str:
    return _normalize_phrase(_to_nominative_phrase(word))

def _extract_single_letter(text: str) -> Optional[str]:
    cleaned = text.strip()
    if len(cleaned) != 1:
        return None
    if not is_letter(cleaned):
        return None
    return normalize_letter(cleaned)

def _letter_script(ch: str) -> Optional[str]:
    if re.fullmatch(r"[A-Za-z]", ch):
        return "latin"
    if re.fullmatch(r"[А-Яа-яЁё]", ch):
        return "cyrillic"
    return None

def _secret_script(secret: str) -> Optional[str]:
    has_cyrillic = any(re.fullmatch(r"[А-Яа-яЁё]", ch) for ch in secret)
    has_latin = any(re.fullmatch(r"[A-Za-z]", ch) for ch in secret)
    if has_cyrillic and not has_latin:
        return "cyrillic"
    if has_latin and not has_cyrillic:
        return "latin"
    return None

def _start_game(chat_id: int, secret: str, host_user_id: int) -> HangmanGame:
    game = HangmanGame(chat_id=chat_id, secret=secret, host_user_id=host_user_id, max_attempts=6)
    GAMES[chat_id] = game
    ACTIVE_GAME[chat_id] = True
    ATTEMPTS[chat_id] = {}
    return game

async def _process_guess(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, guess_text: str) -> None:
    game = GAMES.get(chat_id)
    if game is None:
        ACTIVE_GAME.pop(chat_id, None)
        return

    if (
        update.effective_chat.type == ChatType.PRIVATE
        and game.is_lose()
        and EXTRA_PAYMENT_PENDING.get(chat_id) == user.id
    ):
        await update.message.reply_text("Попытки закончились. Оплатите +1 попытку за 5⭐️.")
        return

    if (
        update.effective_chat.type != ChatType.PRIVATE
        and game.is_lose()
        and EXTRA_PAYMENT_PENDING.get(chat_id)
    ):
        await _prompt_group_lose_choice(update, context, chat_id)
        return

    if (
        update.effective_chat.type == ChatType.PRIVATE
        and LOSE_CHOICE_PENDING.get(chat_id) == user.id
    ):
        await _prompt_lose_choice(update)
        return

    letters = [normalize_letter(ch) for ch in guess_text if is_letter(ch)]
    if len(letters) == 1 and len(guess_text) == 1:
        letter = letters[0]
        secret_script = _secret_script(game.secret)
        guessed_script = _letter_script(letter)
        if secret_script and guessed_script and secret_script != guessed_script:
            await update.message.reply_text("Неверная раскладка. Попытка не засчитана.")
            return
        if game.already_tried(letter):
            await update.message.reply_text(
                f"{escape_markdown(f'Буква «{letter}» уже называлась.', 2)}\n{game.progress_message()}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        _increment_attempt(chat_id, user.id)
        is_correct, is_win, is_lose = game.guess(letter)
        _emit_event(
            "letter_guess",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
            properties={"letter": letter, "is_correct": is_correct},
        )
    else:
        secret_script = _secret_script(game.secret)
        if secret_script:
            guessed_scripts = {_letter_script(ch) for ch in letters if _letter_script(ch)}
            if guessed_scripts and guessed_scripts != {secret_script}:
                await update.message.reply_text("Неверная раскладка. Попытка не засчитана.")
                return
        _increment_attempt(chat_id, user.id)
        is_win = _normalize_phrase(guess_text) == _normalize_phrase(game.secret)
        is_lose = not is_win
        is_correct = is_win
        _emit_event(
            "word_guessed",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
            properties={"guess": guess_text, "is_correct": is_correct},
        )

    if is_win:
        _emit_event(
            "game_win",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
        )
        _emit_event(
            "game_end",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
            properties={"result": "win"},
        )
        shared_token = GAME_SHARED_TOKEN.get(chat_id)
        if shared_token:
            SHARED_PLAYED.setdefault(shared_token, set()).add(user.id)
        daily_key = GAME_DAILY_DATE.get(chat_id)
        if daily_key:
            with _db_connect() as conn:
                _record_daily_play(conn, daily_key, user.id)
        word_key = _normalize_game_word(game.secret)
        participants = ATTEMPTS.get(chat_id, {})
        winner_attempts = len(game.wrong)
        with _db_connect() as conn:
            previous_record = _fetch_word_record(conn, word_key)
            _record_game_results(conn, chat_id, word_key, participants, user.id)
            updated_record = _update_word_record(conn, word_key, user.id, winner_attempts, True)
            leaderboard = _fetch_word_leaderboard(conn, word_key)
            total_players = len(leaderboard)
            record_line = ""
            place_line = ""
            is_new_record = (
                previous_record is not None
                and previous_record["best_attempts"] is not None
                and winner_attempts < previous_record["best_attempts"]
            )
            if is_new_record:
                record_line = "🏆 Новый рекорд! Вы — чемпион!"
            elif previous_record and previous_record["best_attempts"] is not None and previous_record["best_user_id"] is not None:
                record_label = _get_user_label(conn, int(previous_record["best_user_id"]))
                record_line = f"🏆 Рекорд: {previous_record['best_attempts']} {_format_attempts(previous_record['best_attempts'])} ({record_label})"
            if total_players > 1:
                place = next(
                    (idx + 1 for idx, (uid, _) in enumerate(leaderboard) if uid == user.id),
                    total_players,
                )
                place_line = f"Вы на {place} месте из {total_players}"

        display_word = _to_nominative_phrase(game.secret)
        lines = [f"🎉 Вы угадали слово «{display_word}» — {winner_attempts} {_format_attempts(winner_attempts)}"]
        if record_line:
            lines.append(record_line)
        if place_line:
            lines.append(place_line)
        await update.message.reply_text(
            escape_markdown("\n".join(lines), 2),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await update.message.reply_text(
            "Сыграть еще раз?",
            reply_markup=_replay_keyboard(),
        )
        if not daily_key:
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
        GAME_SHARED_TOKEN.pop(chat_id, None)
        GAME_DAILY_DATE.pop(chat_id, None)
        EXTRA_PAYMENT_PENDING.pop(chat_id, None)
        LOSE_CHOICE_PENDING.pop(chat_id, None)
        return

    if is_lose:
        _emit_event(
            "game_lose",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
        )
        _emit_event(
            "game_end",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
            properties={"result": "lose"},
        )
        if update.effective_chat.type == ChatType.PRIVATE:
            LOSE_CHOICE_PENDING[chat_id] = user.id
            _emit_event(
                "extra_attempt_offer_shown",
                update,
                context,
                session_id=str(chat_id),
                game_id=str(chat_id),
            )
            await _prompt_lose_choice(update)
            return
        await _prompt_group_lose_choice(update, context, chat_id)
        EXTRA_PAYMENT_PENDING[chat_id] = user.id
        _emit_event(
            "extra_attempt_offer_shown",
            update,
            context,
            session_id=str(chat_id),
            game_id=str(chat_id),
        )
        return

    # Промежуточный прогресс
    if is_correct:
        await update.message.reply_text(
            f"{escape_markdown('Есть такая буква!', 2)}\n{game.progress_message()}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await update.message.reply_text(
            f"{escape_markdown('Мимо.', 2)}\n{game.progress_message()}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    bot_username = context.bot.username
    text = (update.message.text or "").strip()
    if not text:
        return
    if user_id not in WAITING_WORD:
        if LOSE_CHOICE_PENDING.get(chat_id) == user_id:
            normalized = text.strip().lower()
            if normalized == BUY_ATTEMPT_TEXT.lower():
                await _offer_extra_attempt(update, context, chat_id, user_id)
                return
            if normalized == END_GAME_TEXT.lower():
                game = GAMES.get(chat_id)
                if game:
                    await _finalize_loss(update, chat_id, game)
                return
            if normalized == NEW_GAME_TEXT.lower():
                game = GAMES.get(chat_id)
                if game:
                    await _finalize_loss(update, chat_id, game, show_replay=False)
                await _start_bot_game(update, context)
                return
            await _prompt_lose_choice(update)
            return
        if text.strip().lower() == PLAY_BUTTON_TEXT.lower():
            await _start_bot_game(update, context)
            return
        if user_id in WAITING_SHARED_WORD:
            secret = _sanitize_secret(text)
            if not secret:
                await update.message.reply_text(
                    "Нужно ввести хотя бы одну букву (разрешены буквы, пробелы и дефисы). Попробуйте снова."
                )
                return
            token = uuid.uuid4().hex
            SHARED_WORDS[token] = secret
            SHARED_HOST[token] = user_id
            del WAITING_SHARED_WORD[user_id]
            deep_link = f"https://t.me/{bot_username}?start=play_{token}"
            await update.message.reply_text(
                "Готово! Отправьте эту ссылку любому человеку или в чат, "
                "и игра начнётся у него в личке с ботом:\n"
                f"{deep_link}"
            )
            return

        if ACTIVE_GAME.get(chat_id):
            guess_text = _sanitize_guess(text)
            if not guess_text:
                await update.message.reply_text(
                    "Отправьте одну букву или слово целиком (разрешены буквы, пробелы и дефисы)."
                )
                return
            user = update.effective_user
            with _db_connect() as conn:
                _upsert_user(conn, user)
            await _process_guess(update, context, chat_id, user, guess_text)
            return

        if re.fullmatch(r"(?:/share|загадать)", text, flags=re.IGNORECASE):
            WAITING_SHARED_WORD[user_id] = True
            await update.message.reply_text(
                "Введите секретное слово для ссылки (буквы рус/лат, можно пробелы и дефисы)."
            )
            return

        await update.message.reply_text(
            "Чтобы загадать слово в группе, просто упомяните бота в группе.\n"
            "Чтобы создать ссылку на игру в личке — отправьте «загадать» или /share."
        )
        return

    chat_id = WAITING_WORD[user_id]
    secret = _sanitize_secret(text)
    if not secret:
        await update.message.reply_text("Нужно ввести хотя бы одну букву (разрешены буквы, пробелы и дефисы). Попробуйте снова.")
        return

    # Заводим игру
    game = _start_game(chat_id=chat_id, secret=secret, host_user_id=user_id)
    del WAITING_WORD[user_id]
    _emit_event(
        "game_start",
        update,
        context,
        session_id=str(chat_id),
        game_id=str(chat_id),
        properties={"mode": "group", "secret_length": len(secret)},
    )

    # Сообщение в группу
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{escape_markdown('🧩 Слово загадано!', 2)}\n{game.progress_message()}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=ReplyKeyboardRemove(),
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

def _is_share_command(text: str, bot_username: str) -> bool:
    match = re.match(r"^/share(?:@(\w+))?\b", text.strip(), flags=re.IGNORECASE)
    if not match:
        return False
    target = match.group(1)
    return target is not None and target.lower() == bot_username.lower()

def _is_play_command(text: str, bot_username: str) -> bool:
    match = re.match(r"^/play(?:@(\w+))?\b", text.strip(), flags=re.IGNORECASE)
    if not match:
        return False
    target = match.group(1)
    return target is None or target.lower() == bot_username.lower()

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик добавления бота в чат — убираем старые reply-клавиатуры"""
    chat_member = update.my_chat_member
    if not chat_member:
        return
    new_status = chat_member.new_chat_member.status
    old_status = chat_member.old_chat_member.status
    # Бот был добавлен в чат (member или administrator)
    if old_status in ["left", "kicked"] and new_status in ["member", "administrator"]:
        chat_id = update.effective_chat.id
        if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Привет! Я бот для игры в «Виселицу». Просто упомяните меня, чтобы начать игру.",
                    reply_markup=ReplyKeyboardRemove(),
                )
            except Exception:
                pass

async def on_group_mention(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    msg = update.message
    text = (msg.text or "").strip()

    chat_id = update.effective_chat.id
    bot_username = context.bot.username
    if _is_play_command(text, bot_username):
        await _start_group_play_game(update, context)
        return
    if _is_share_command(text, bot_username):
        if chat_id in GAMES:
            await msg.reply_text(
                f"{escape_markdown('Игра уже идёт. Текущий прогресс ниже.', 2)}\n{GAMES[chat_id].progress_message()}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        deep_link = f"https://t.me/{bot_username}?start=ask_{chat_id}"
        await msg.reply_text(
            f"Кто загадывает слово — перейдите по ссылке в личку бота:\n{deep_link}\n"
            "Там введите секретное слово.",
            disable_web_page_preview=True,
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    # Ветка старта: достаточно просто упомянуть бота
    if _mentioned_this_bot(update, context) and not ACTIVE_GAME.get(chat_id):
        if chat_id in GAMES:
            await msg.reply_text(
                f"{escape_markdown('Игра уже идёт. Текущий прогресс ниже.', 2)}\n{GAMES[chat_id].progress_message()}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        deep_link = f"https://t.me/{bot_username}?start=ask_{chat_id}"
        await msg.reply_text(
            f"Кто загадывает слово — перейдите по ссылке в личку бота:\n{deep_link}\n"
            "Там введите секретное слово.",
            disable_web_page_preview=True,
            reply_markup=ReplyKeyboardRemove(),
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
            await _process_guess(update, context, chat_id, user, guess_text)
            return

        # Без упоминания принимаем только одну букву
        letter = _extract_single_letter(text)
        if not letter:
            return
        await _process_guess(update, context, chat_id, user, letter)
        return

    if _mentioned_this_bot(update, context):
        await msg.reply_text("Сначала начните игру: просто упомяните бота.")
        return

# --- Запуск ---

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не найден в окружении. Создайте .env и задайте токен.")

    _init_db()
    app: Application = ApplicationBuilder().token(token).build()

    async def _post_init(application: Application) -> None:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Памятка и быстрый старт"),
                BotCommand("play", "Играть с ботом"),
                BotCommand("share", "Создать ссылку на игру"),
                BotCommand("daily", "Слово дня"),
                BotCommand("stats", "Глобальная статистика по слову"),
            ]
        )

    app.post_init = _post_init

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("share", cmd_share))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(CallbackQueryHandler(on_lose_choice_callback, pattern=f"^{BUY_ATTEMPT_CB}$|^{END_GAME_CB}$|^{NEW_GAME_CB}$"))
    app.add_handler(CallbackQueryHandler(on_replay_callback, pattern=f"^{REPLAY_CB}$"))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
    
    # Обработчик добавления бота в чат
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Приватные сообщения — ввод секретного слова
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, on_private_text))

    # Группы — любые тексты, но мы внутри проверим упоминание и логику
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_mention))

    # Слово дня
    if app.job_queue is None:
        log.warning("JobQueue не доступен. Установите 'python-telegram-bot[job-queue]' для ежедневной рассылки.")
    else:
        app.job_queue.run_daily(
            _send_daily_word,
            time=time(hour=9, minute=0, tzinfo=timezone.utc),
        )

    log.info("Starting bot...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

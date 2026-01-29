from __future__ import annotations
from dataclasses import dataclass, field
from typing import Set, Tuple
import re

# Разрешаем кириллицу и латиницу; пробелы и дефисы отображаем как есть.
ALLOWED_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")

STAGES = [
    # 0 ошибок
    "┏━━━━━┓\n"
    "┃     \n"
    "┃     \n"
    "┃     \n"
    "┃     \n"
    "┻━━━━━━",
    # 1
    "┏━━━━━┓\n"
    "┃     │\n"
    "┃     ◯\n"
    "┃     \n"
    "┃     \n"
    "┻━━━━━━",
    # 2
    "┏━━━━━┓\n"
    "┃     │\n"
    "┃     ◯\n"
    "┃     │\n"
    "┃     \n"
    "┻━━━━━━",
    # 3
    "┏━━━━━┓\n"
    "┃     │\n"
    "┃     ◯\n"
    "┃    ╱│\n"
    "┃     \n"
    "┻━━━━━━",
    # 4
    "┏━━━━━┓\n"
    "┃     │\n"
    "┃     ◯\n"
    "┃    ╱│╲\n"
    "┃     \n"
    "┻━━━━━━",
    # 5
    "┏━━━━━┓\n"
    "┃     │\n"
    "┃     ◯\n"
    "┃    ╱│╲\n"
    "┃    ╱ \n"
    "┻━━━━━━",
    # 6 — финал
    "┏━━━━━┓\n"
    "┃     │\n"
    "┃     ◯\n"
    "┃    ╱│╲\n"
    "┃    ╱ ╲\n"
    "┻━━━━━━",
]

def normalize_letter(ch: str) -> str:
    # Нормализуем регистр; 'Ё' и 'Е' считаем разными явно (можно объединить при желании)
    return ch.lower()

def is_letter(ch: str) -> bool:
    return bool(ALLOWED_LETTER_RE.fullmatch(ch))

def render_masked(secret: str, guessed: Set[str]) -> str:
    """
    Рисует маску: для букв — '—' если ещё не угадано, иначе буква (в нижнем регистре),
    для пробела показывает '/', дефисы и прочие символы — как есть.
    Между символами — пробел для наглядности.
    """
    tokens = []
    g = {normalize_letter(c) for c in guessed}
    for ch in secret:
        if ch == " ":
            tokens.append("/")
        elif ch == "\t":
            tokens.append("/")
        elif ch == "-":
            tokens.append("-")
        elif is_letter(ch):
            tokens.append(normalize_letter(ch) if normalize_letter(ch) in g else "—")
        else:
            # Прочие знаки препинания оставим как есть
            tokens.append(ch)
    return " ".join(tokens)

def render_gallows(wrong_count: int, max_attempts: int = 6) -> str:
    """
    Возвращает ASCII-виселицу по количеству ошибок.
    max_attempts по умолчанию соответствует последнему индексу STAGES.
    """
    if max_attempts != 6:
        # линеаризуем к диапазону STAGES
        wrong_count = int(round(wrong_count * 6 / max(1, max_attempts)))
    idx = max(0, min(6, wrong_count))
    return STAGES[idx]

@dataclass
class HangmanGame:
    chat_id: int
    secret: str
    host_user_id: int
    max_attempts: int = 6
    guessed: Set[str] = field(default_factory=set)
    wrong: Set[str] = field(default_factory=set)

    def _unique_letters(self) -> Set[str]:
        return {normalize_letter(c) for c in self.secret if is_letter(c)}

    def masked(self) -> str:
        return render_masked(self.secret, self.guessed)

    def gallows(self) -> str:
        return render_gallows(len(self.wrong), self.max_attempts)

    def already_tried(self, letter: str) -> bool:
        l = normalize_letter(letter)
        return l in self.guessed or l in self.wrong

    def guess(self, letter: str) -> Tuple[bool, bool, bool]:
        """
        Делает ход.
        Возвращает кортеж: (is_correct, is_win, is_lose).
        """
        l = normalize_letter(letter)
        if not is_letter(l) or len(l) != 1:
            return (False, False, False)
        if self.already_tried(l):
            # Без изменения состояния, но сигнализируем как "неизменившийся" ход
            return (l in self._unique_letters(), self.is_win(), self.is_lose())

        if l in self._unique_letters():
            self.guessed.add(l)
            return (True, self.is_win(), self.is_lose())
        else:
            self.wrong.add(l)
            return (False, self.is_win(), self.is_lose())

    def is_win(self) -> bool:
        return self._unique_letters().issubset(self.guessed)

    def is_lose(self) -> bool:
        return len(self.wrong) >= self.max_attempts

    # hangman/core.py
    def progress_message(self) -> str:
        guessed_sorted = " ".join(sorted(self.guessed)) if self.guessed else "—"
        wrong_sorted = " ".join(sorted(self.wrong)) if self.wrong else "—"
        return (
            f"```\n{self.gallows()}\n```\n"
            f"Слово: `{self.masked()}`\n"
            f"Верные буквы: `{guessed_sorted}`\n"
            f"Ошибки `({len(self.wrong)}/{self.max_attempts})`: `{wrong_sorted}`"
        )


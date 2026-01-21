import pytest
from hangman.core import render_masked, render_gallows, HangmanGame

# -------- Тесты рендера виселицы --------

@pytest.mark.parametrize(
    "wrong, expected",
    [
        (0,
         "┏━━━━━┓\n"
         "┃     \n"
         "┃     \n"
         "┃     \n"
         "┃     \n"
         "┻━━━━━━"
        ),
        (6,
         "┏━━━━━┓\n"
         "┃     │\n"
         "┃     ◯\n"
         "┃    ╱│╲\n"
         "┃    ╱ ╲\n"
         "┻━━━━━━"
        ),
    ]
)
def test_render_gallows_full_match(wrong, expected):
    assert render_gallows(wrong) == expected

@pytest.mark.parametrize("wrong", [1, 2, 3, 4, 5])
def test_render_gallows_contains_head_after_first_error(wrong):
    # Начиная с 1 ошибки должна появиться голова "◯"
    stage = render_gallows(wrong)
    assert "◯" in stage

# -------- Тесты рендера маски --------

@pytest.mark.parametrize(
    "secret, guessed, expected",
    [
        ("кот", set(), "— — —"),
        ("дом", {"о"}, "— о —"),
        ("река", {"р", "а"}, "р — — а"),
        # было: ("мой-дом", {"о"}, "м — й - д о м".replace("м", "—"))
        ("мой-дом", {"о"}, "— о — - — о —"),
        # было: ("два слова", set(), "— — а / — — о — а".replace("а", "—"))
        ("два слова", set(), "— — — / — — — — —"),
    ]
)
def test_render_masked(secret, guessed, expected):
    assert render_masked(secret, guessed) == expected


def test_game_progress_message_stable():
    g = HangmanGame(chat_id=1, secret="мир", host_user_id=42)
    # Ноль букв
    txt = g.progress_message()
    assert "Слово: `— — —`" in txt
    # Угадали "и"
    g.guess("и")
    txt2 = g.progress_message()
    assert "Слово: `— и —`" in txt2

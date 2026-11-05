"""Правила CS2: как определяется победитель карты и всей игры.

Игра (серия) состоит из карт. Чтобы выиграть игру, нужно взять нужное число карт:
Bo1 — одну, Bo3 — две, Bo5 — три. Карта идёт до 13 раундов: побеждает тот, кто
первым набрал 13. При 12:12 начинается овертайм — 6 дополнительных раундов, в них
надо забрать 4. Если и там никто не выиграл (15:15), играется ещё один такой блок.

Всё это — чистые функции без базы и сети: и синк, и админские команды, и ручные
результаты считают карты и серии одинаково.
"""

from __future__ import annotations

# Карта идёт до 13 раундов.
ROUNDS_TO_WIN = 13
# Овертайм: блок из 6 раундов, для победы в нём нужно взять 4.
OVERTIME_ROUNDS = 6
OVERTIME_TO_WIN = OVERTIME_ROUNDS // 2 + 1
# Каждый следующий блок овертайма сдвигает счёт победителя на 3: 16 → 19 → 22.
OVERTIME_STEP = 3
# Сколько карт нужно взять для победы в серии.
MAPS_TO_WIN = {"bo1": 1, "bo3": 2, "bo5": 3}


def required_maps(tournament: str) -> int:
    """Сколько карт нужно взять, чтобы выиграть серию (по названию формата)."""
    name = (tournament or "").lower()
    for fmt, need in MAPS_TO_WIN.items():
        if fmt in name:
            return need
    return 2  # формат в названии не указан — считаем Bo3


def format_label(tournament: str) -> str:
    """«Bo3» для подписи на карточке."""
    return f"Bo{required_maps(tournament)}"


def map_finished(score_a: int | None, score_b: int | None) -> bool:
    """Карта доиграна до победы одной из команд."""
    return map_winner(score_a, score_b) is not None


def map_winner(score_a: int | None, score_b: int | None) -> str | None:
    """Победитель карты по раундам: `a`, `b` или None, если карта ещё идёт.

    Карта идёт до 13 раундов. Ничья 12:12 отправляет её в овертайм: блок из шести
    раундов, для победы в блоке нужно взять четыре. Если блок закончился 15:15,
    играется следующий такой же блок — пока не выявится победитель.

    Отсюда корректные финальные счёта:
    * 13:N, где N ≤ 11 (основное время);
    * 16:N, 19:N, 22:N … где счёт победителя — 13 + 3·k, а отрыв — 2, 3 или 4
      (выигранный блок овертайма: 16:12–16:14, 19:15–19:17 и так далее).
    Всё остальное — идущая карта: 13:12, 14:12, 12:12, 15:15, 15:12.
    """
    if score_a is None or score_b is None or score_a == score_b:
        return None
    high, low = max(score_a, score_b), min(score_a, score_b)
    diff = high - low

    regulation = high == ROUNDS_TO_WIN and low <= ROUNDS_TO_WIN - 2
    overtime = (
        high >= ROUNDS_TO_WIN + OVERTIME_STEP
        and (high - ROUNDS_TO_WIN) % OVERTIME_STEP == 0
        and 2 <= diff <= OVERTIME_TO_WIN
    )
    if not (regulation or overtime):
        return None
    return "a" if score_a > score_b else "b"


def map_score_text(score_a: int | None, score_b: int | None) -> str | None:
    if score_a is None or score_b is None:
        return None
    return f"{score_a}:{score_b}"


def series_winner(wins_a: int, wins_b: int, tournament: str) -> str | None:
    """Победитель серии по выигранным картам."""
    need = required_maps(tournament)
    if wins_a >= need:
        return "a"
    if wins_b >= need:
        return "b"
    return None


def series_finished(tournament: str, wins_a: int, wins_b: int) -> bool:
    return series_winner(wins_a, wins_b, tournament) is not None


def series_score_from_maps(tournament: str) -> int:
    """Сколько всего карт может быть в серии (Bo3 — до трёх)."""
    return required_maps(tournament) * 2 - 1


__all__ = [
    "MAPS_TO_WIN",
    "OVERTIME_ROUNDS",
    "OVERTIME_STEP",
    "OVERTIME_TO_WIN",
    "ROUNDS_TO_WIN",
    "format_label",
    "map_finished",
    "map_score_text",
    "map_winner",
    "required_maps",
    "series_finished",
    "series_score_from_maps",
    "series_winner",
]

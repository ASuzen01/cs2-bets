"""Азартные игры мини-аппа: рулетка и кости.

Правила и экономика
-------------------
**Рулетка** — европейская, 37 лунок (0…36, один ноль). Ставки классические:

===============  ======================  =========  ===========
ставка           выигрывает на            кэф        шанс
===============  ======================  =========  ===========
красное / чёрное 18 чисел нужного цвета   ×2         48.6 %
чётное / нечётное 18 чисел                 ×2         48.6 %
малое (1–18) / большое (19–36)             ×2         48.6 %
дюжина (1–12, 13–24, 25–36)                ×3         32.4 %
колонка (по 12 чисел)                      ×3         32.4 %
точное число, включая зеро                 ×36        2.7 %
===============  ======================  =========  ===========

Кэфы выплачиваются как в казино «настоящими» (36 к 1 за число, 2 к 1 за дюжину):
преимущество дома — 2.7 %, ровно как у европейского колеса. Никакой дополнительной
маржи не добавляем, чтобы игрок не чувствовал себя обманутым.

**Кости** — два кубика 1–6, сумма 2…12. Ставки:

* **меньше** (сумма 2–6) и **больше** (8–12) — по 15 из 36 исходов;
* **точная сумма** — от 1 исхода (2 и 12) до 6 (7).

Кэф считаем от числа благоприятных исходов с наценкой 4.5 %:
`кэф = 36 / исходов × 0.955`. То есть «больше/меньше» — ×2.29 (вместо честных 2.40),
«ровно 7» — ×5.73 (вместо 6.00), «ровно 2» — ×34.38 (вместо 36.00). Преимущество
дома 4.5 % — чуть выше рулетки, потому что кубики бросает бот и угадать «больше»
проще, чем поставить на одно число.

Где считать
-----------
Всё считается на сервере: клиент присылает только выбор и сумму, а исход
(число на колесе или грани кубиков) приходит в ответе. Клиент его **анимирует** —
крутит колесо и роняет кубики на нужный результат. Так игрок не может подсмотреть
или подкрутить исход: анимация — только картинка.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------- рулетка
# Красные числа европейского колеса; всё остальное (кроме нуля) — чёрное.
ROULETTE_RED = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})
ROULETTE_SLOTS = 37                      # 0…36, один ноль
ROULETTE_STRAIGHT = 36.0                 # 35 к 1 плюс ставка

DICE_EDGE = 0.955                        # сколько от честной выплаты оставляем игроку
DICE_WAYS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 5, 9: 4, 10: 3, 11: 2, 12: 1}


@dataclass(frozen=True, slots=True)
class GameBet:
    """Ставка внутри игры: код выбора, подпись и кэф."""

    code: str
    label: str
    odds: float
    group: str = ""


# ------------------------------------------------------------------ рулетка
def _roulette_options() -> tuple[GameBet, ...]:
    options = [
        GameBet("red", "Красное", 2.0, "Цвет"),
        GameBet("black", "Чёрное", 2.0, "Цвет"),
        GameBet("even", "Чёт", 2.0, "Чёт/нечет"),
        GameBet("odd", "Нечет", 2.0, "Чёт/нечет"),
        GameBet("low", "1–18", 2.0, "Половина"),
        GameBet("high", "19–36", 2.0, "Половина"),
        GameBet("dozen:1", "1–12", 3.0, "Дюжина"),
        GameBet("dozen:2", "13–24", 3.0, "Дюжина"),
        GameBet("dozen:3", "25–36", 3.0, "Дюжина"),
        GameBet("column:1", "1-я", 3.0, "Колонка"),
        GameBet("column:2", "2-я", 3.0, "Колонка"),
        GameBet("column:3", "3-я", 3.0, "Колонка"),
        GameBet("zero", "Зеро", ROULETTE_STRAIGHT, "Зеро"),
    ]
    # точные числа: интерфейс рисует их отдельной сеткой, но кэфы и подписи
    # должны приходить с сервера — одним списком, чтобы не дублировать правила
    options += [
        GameBet(f"straight:{number}", f"Число {number}", ROULETTE_STRAIGHT, "Числа")
        for number in range(ROULETTE_SLOTS)
    ]
    return tuple(options)


ROULETTE_OPTIONS = _roulette_options()
ROULETTE_BY_CODE = {option.code: option for option in ROULETTE_OPTIONS}


def roulette_colour(number: int) -> str:
    """'red' / 'black' / 'green' — цвет лунки."""
    if number == 0:
        return "green"
    return "red" if number in ROULETTE_RED else "black"


def roulette_label(selection: str) -> str:
    """Подпись ставки для истории и уведомлений."""
    if selection.startswith("straight:"):
        return f"Число {selection.split(':', 1)[1]}"
    option = ROULETTE_BY_CODE.get(selection)
    return option.label if option else selection


def roulette_odds(selection: str) -> float:
    if selection.startswith("straight:"):
        _check_straight(selection)
        return ROULETTE_STRAIGHT
    option = ROULETTE_BY_CODE.get(selection)
    if option is None:
        raise ValueError("Такой ставки в рулетке нет")
    return option.odds


def _check_straight(selection: str) -> int:
    raw = selection.split(":", 1)[1]
    if not raw.isdigit():
        raise ValueError("Номер в ставке должен быть числом")
    number = int(raw)
    if not 0 <= number <= 36:
        raise ValueError("На колесе нет такого числа")
    return number


def spin_roulette() -> int:
    """Крутит колесо: 0…36. `secrets` — чтобы исход нельзя было предсказать."""
    return secrets.randbelow(ROULETTE_SLOTS)


def roulette_wins(selection: str, number: int) -> bool:
    """Выиграла ли ставка на выпавшее число."""
    if selection.startswith("straight:"):
        return _check_straight(selection) == number
    if selection == "zero":
        return number == 0
    if number == 0:
        return False            # зеро проигрывает всем равным ставкам
    if selection == "red":
        return roulette_colour(number) == "red"
    if selection == "black":
        return roulette_colour(number) == "black"
    if selection == "even":
        return number % 2 == 0
    if selection == "odd":
        return number % 2 == 1
    if selection == "low":
        return 1 <= number <= 18
    if selection == "high":
        return 19 <= number <= 36
    if selection.startswith("dozen:"):
        index = int(selection.split(":", 1)[1])
        if index not in (1, 2, 3):
            raise ValueError("Дюжин всего три")
        return (number - 1) // 12 + 1 == index
    if selection.startswith("column:"):
        index = int(selection.split(":", 1)[1])
        if index not in (1, 2, 3):
            raise ValueError("Колонок всего три")
        return (number - 1) % 3 + 1 == index
    raise ValueError("Такой ставки в рулетке нет")


# -------------------------------------------------------------------- кости
def _dice_odds(ways: int) -> float:
    return round(36 / ways * DICE_EDGE, 2)


def _dice_options() -> tuple[GameBet, ...]:
    options = [
        GameBet("under", "Меньше (2–6)", _dice_odds(15), "Больше/меньше"),
        GameBet("over", "Больше (8–12)", _dice_odds(15), "Больше/меньше"),
    ]
    options += [
        GameBet(f"exact:{total}", f"Ровно {total}", _dice_odds(ways), "Точная сумма")
        for total, ways in DICE_WAYS.items()
    ]
    return tuple(options)


DICE_OPTIONS = _dice_options()
DICE_BY_CODE = {option.code: option for option in DICE_OPTIONS}


def dice_label(selection: str) -> str:
    option = DICE_BY_CODE.get(selection)
    return option.label if option else selection


def dice_odds(selection: str) -> float:
    option = DICE_BY_CODE.get(selection)
    if option is None:
        raise ValueError("Такой ставки в костях нет")
    return option.odds


def roll_dice() -> tuple[int, int]:
    """Бросает два кубика: (1…6, 1…6)."""
    return secrets.randbelow(6) + 1, secrets.randbelow(6) + 1


def dice_wins(selection: str, first: int, second: int) -> bool:
    total = first + second
    if selection == "under":
        return total < 7
    if selection == "over":
        return total > 7
    if selection.startswith("exact:"):
        raw = selection.split(":", 1)[1]
        if not raw.isdigit() or not 2 <= int(raw) <= 12:
            raise ValueError("Сумма двух кубиков — от 2 до 12")
        return total == int(raw)
    raise ValueError("Такой ставки в костях нет")


# ------------------------------------------------------------------ общее
def game_options(game: str) -> list[dict[str, Any]]:
    """Роспись игры для интерфейса: что можно выбрать и по какому кэфу."""
    source = {"roulette": ROULETTE_OPTIONS, "dice": DICE_OPTIONS}.get(game)
    if source is None:
        raise ValueError("Такой игры нет")
    return [
        {"code": option.code, "label": option.label, "odds": option.odds, "group": option.group}
        for option in source
    ]


def play(game: str, selection: str, amount: int) -> dict[str, Any]:
    """Проводит раунд: проверяет ставку, бросает исход и считает выплату."""
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")
    if game == "roulette":
        odds = roulette_odds(selection)
        label = roulette_label(selection)
        number = spin_roulette()
        won = roulette_wins(selection, number)
        outcome = str(number)
        detail = {"number": number, "colour": roulette_colour(number)}
    elif game == "dice":
        odds = dice_odds(selection)
        label = dice_label(selection)
        first, second = roll_dice()
        won = dice_wins(selection, first, second)
        outcome = str(first + second)
        detail = {"dice": [first, second], "total": first + second}
    else:
        raise ValueError("Такой игры нет")

    payout = round(amount * odds) if won else 0
    return {
        "game": game,
        "selection": selection,
        "label": label,
        "odds": odds,
        "amount": amount,
        "outcome": outcome,
        "win": won,
        "payout": payout,
        **detail,
    }


__all__ = [
    "DICE_BY_CODE",
    "DICE_OPTIONS",
    "ROULETTE_BY_CODE",
    "ROULETTE_OPTIONS",
    "ROULETTE_RED",
    "ROULETTE_SLOTS",
    "dice_label",
    "dice_odds",
    "dice_wins",
    "game_options",
    "play",
    "roll_dice",
    "roulette_colour",
    "roulette_label",
    "roulette_odds",
    "roulette_wins",
    "spin_roulette",
]

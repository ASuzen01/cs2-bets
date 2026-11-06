"""Игры мини-аппа: рулетка и кости (задача 53).

Две вещи, которые тут проверяются:

* **экономика** — кэфы такие, что дом забирает свои 2.7 % на рулетке и 4.5 % на костях,
  а не «на глазок»: считаем ожидание по всем исходам колеса и всех 36 комбинаций кубиков;
* **деньги** — раунд идёт одной транзакцией: ставка списывается, выигрыш зачисляется,
  в истории остаётся строка, а при нехватке монет не происходит вообще ничего.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import Database, InsufficientFunds
from app.services import games
from app.services.games import (
    DICE_OPTIONS,
    ROULETTE_RED,
    dice_odds,
    game_options,
    play,
    roulette_colour,
    roulette_odds,
    roulette_wins,
)

# ------------------------------------------------------------------ экономика
ROULETTE_SELECTIONS = [
    "red",
    "black",
    "even",
    "odd",
    "low",
    "high",
    "dozen:1",
    "dozen:2",
    "dozen:3",
    "column:1",
    "column:2",
    "column:3",
    "zero",
    "straight:0",
    "straight:1",
    "straight:17",
    "straight:36",
]


async def test_legacy_casino_table_gets_result_json_migration(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE casino_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                selection TEXT NOT NULL,
                label TEXT NOT NULL,
                amount INTEGER NOT NULL,
                odds REAL NOT NULL,
                outcome TEXT NOT NULL,
                win INTEGER NOT NULL,
                payout INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )

    db = Database(path)
    await db.connect()
    try:
        columns = await db.conn.table_columns("casino_bets")
        assert "result_json" in columns
    finally:
        await db.close()


def _roulette_expectation(selection: str) -> float:
    """Средний возврат на 1 монету по всем 37 лункам."""
    odds = roulette_odds(selection)
    return sum(odds if roulette_wins(selection, number) else 0 for number in range(37)) / 37


@pytest.mark.parametrize("selection", ROULETTE_SELECTIONS)
def test_roulette_house_edge_is_one_slot(selection) -> None:
    """Европейское колесо: дом забирает ровно 1/37 = 2.7 %, ни больше ни меньше."""
    assert _roulette_expectation(selection) == pytest.approx(36 / 37, abs=1e-9)


def test_roulette_red_black_split_evenly() -> None:
    """18 красных и 18 чёрных: суммы 1…36 без пересечений."""
    assert len(ROULETTE_RED) == 18
    black = [n for n in range(1, 37) if roulette_colour(n) == "black"]
    assert len(black) == 18
    assert not ROULETTE_RED & set(black)
    assert roulette_colour(0) == "green"


@pytest.mark.parametrize("selection", [option.code for option in DICE_OPTIONS])
def test_dice_house_edge(selection) -> None:
    """Кости: дом забирает 4.5 % на любой ставке — проверяем по всем 36 броскам."""
    odds = dice_odds(selection)
    returned = sum(
        odds if games.dice_wins(selection, first, second) else 0
        for first in range(1, 7)
        for second in range(1, 7)
    )
    assert returned / 36 == pytest.approx(0.955, abs=0.006)


def test_dice_odds_are_ordered_by_probability() -> None:
    """Чем вероятнее сумма, тем ниже кэф: 7 — самая частая, 2 и 12 — самые редкие."""
    seven = dice_odds("exact:7")
    assert seven < dice_odds("exact:6") < dice_odds("exact:5") < dice_odds("exact:4")
    assert dice_odds("exact:2") == dice_odds("exact:12")
    assert dice_odds("under") < seven
    assert dice_odds("over") == dice_odds("under")


def test_game_options_cover_everything_the_ui_shows() -> None:
    roulette = game_options("roulette")
    dice = game_options("dice")
    assert {"red", "black", "even", "odd", "zero"} <= {item["code"] for item in roulette}
    assert {"under", "over"} <= {item["code"] for item in dice}
    assert all(item["odds"] > 1 for item in roulette + dice)
    with pytest.raises(ValueError):
        game_options("poker")


# -------------------------------------------------------------------- правила
def test_zero_beats_even_money_bets() -> None:
    """Зеро проигрывает красному, чётному и «малому» — но выигрывает у своей ставки."""
    for selection in ("red", "black", "even", "odd", "low", "high", "dozen:1", "column:2"):
        assert roulette_wins(selection, 0) is False
    assert roulette_wins("zero", 0) is True
    assert roulette_wins("straight:0", 0) is True


def test_dozen_and_column_land_where_expected() -> None:
    assert roulette_wins("dozen:1", 12) and not roulette_wins("dozen:1", 13)
    assert roulette_wins("dozen:3", 36) and roulette_wins("dozen:2", 13)
    # колонки идут сверху вниз по таблице: 1, 4, 7 … — первая
    assert roulette_wins("column:1", 1) and roulette_wins("column:1", 34)
    assert roulette_wins("column:2", 2) and roulette_wins("column:3", 3)


def test_bad_selections_are_rejected() -> None:
    for selection in ("straight:37", "straight:minus", "dozen:4", "column:0", "blue"):
        with pytest.raises(ValueError):
            roulette_odds(selection)
    for selection in ("exact:1", "exact:13", "exact:x", "triple"):
        with pytest.raises(ValueError):
            dice_odds(selection)
    with pytest.raises(ValueError):
        play("roulette", "red", 0)
    with pytest.raises(ValueError):
        play("poker", "red", 10)


def test_play_returns_outcome_and_payout() -> None:
    """Раунд всегда отдаёт исход: число на колесе или обе грани кубиков."""
    spin = play("roulette", "red", 100)
    assert 0 <= spin["number"] <= 36
    assert spin["outcome"] == str(spin["number"])
    assert spin["colour"] in ("red", "black", "green")
    assert spin["payout"] == (200 if spin["win"] else 0)

    roll = play("dice", "exact:7", 100)
    first, second = roll["dice"]
    assert (1 <= first <= 6) and (1 <= second <= 6)
    assert roll["total"] == first + second == int(roll["outcome"])
    assert roll["win"] is (roll["total"] == 7)
    assert roll["payout"] == (573 if roll["win"] else 0)


def test_odds_are_rounded_to_kopecks() -> None:
    """Кэфы с двумя знаками — чтобы в интерфейсе не мигали длинные хвосты."""
    for option in games.ROULETTE_OPTIONS + games.DICE_OPTIONS:
        assert round(option.odds, 2) == option.odds


# --------------------------------------------------------------------- деньги
@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "casino.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7, "lucky", "Lucky Seven")
    yield database
    await database.close()


async def test_win_is_credited_and_logged(db: Database) -> None:
    balance, round_id = await db.play_casino(
        7,
        game="roulette",
        selection="red",
        label="Красное",
        amount=100,
        odds=2.0,
        outcome="17",
        won=True,
        payout=200,
    )
    assert balance == 1100
    assert round_id > 0

    history = await db.casino_history(7)
    assert len(history) == 1
    assert history[0]["label"] == "Красное"
    assert history[0]["payout"] == 200
    assert history[0]["win"] == 1

    summary = await db.casino_summary(7)
    assert (summary["rounds"], summary["staked"], summary["paid"]) == (1, 100, 200)
    assert summary["net"] == 100


async def test_loss_only_takes_the_stake(db: Database) -> None:
    balance, _ = await db.play_casino(
        7,
        game="dice",
        selection="exact:2",
        label="Ровно 2",
        amount=250,
        odds=34.38,
        outcome="9",
        won=False,
        payout=0,
    )
    assert balance == 750
    summary = await db.casino_summary(7)
    assert summary["net"] == -250
    assert summary["wins"] == 0


async def test_no_money_no_round(db: Database) -> None:
    """Ставка больше баланса: ни списания, ни строки в истории."""
    with pytest.raises(InsufficientFunds):
        await db.play_casino(
            7,
            game="roulette",
            selection="black",
            label="Чёрное",
            amount=5000,
            odds=2.0,
            outcome="2",
            won=True,
            payout=10000,
        )
    assert await db.get_balance(7) == 1000
    assert await db.casino_history(7) == []


async def test_ledger_keeps_both_sides_of_a_round(db: Database) -> None:
    """В бухгалтерии видно и ставку, и выплату — баланс сходится с историей."""
    await db.play_casino(
        7,
        game="roulette",
        selection="straight:17",
        label="Число 17",
        amount=50,
        odds=36.0,
        outcome="17",
        won=True,
        payout=1800,
    )
    rows = await db.history(7, 10)
    deltas = sorted(int(row["delta"]) for row in rows)
    assert deltas == [-50, 1800]
    assert await db.get_balance(7) == 2750


async def test_history_is_newest_first_and_limited(db: Database) -> None:
    for index in range(5):
        await db.play_casino(
            7,
            game="dice",
            selection="under",
            label="Меньше 7",
            amount=10 + index,
            odds=2.29,
            outcome="5",
            won=True,
            payout=round((10 + index) * 2.29),
        )
    rows = await db.casino_history(7, 3)
    assert len(rows) == 3
    assert [int(row["amount"]) for row in rows] == [14, 13, 12]

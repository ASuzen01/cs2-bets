"""Регрессии из сквозного дебага (задача 50).

Каждая проверка — про то, что игрок реально видит: подпись турнира, иконку спорта,
текст возврата и запись в истории операций. На деньги эти правки не влияют,
но именно здесь легче всего «поехать» незаметно.
"""

from __future__ import annotations

import time

import pytest

from app import texts
from app.db import Database
from app.web.payloads import short_tournament as web_short_tournament


# --------------------------------------------------------------- подпись турнира
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Counter-Strike. IEM Пекин. Групповой этап. Bo3", "IEM Пекин · Bo3"),
        ("Counter-Strike. BLAST Premier. Финал. Bo5", "BLAST Premier · Bo5"),
        ("Лига Чемпионов УЕФА. Общий этап. 4-й тур", "Лига Чемпионов УЕФА · Общий этап"),
    ],
)
def test_bot_tournament_keeps_series_format(raw: str, expected: str) -> None:
    """Формат серии (Bo3/Bo5) нельзя терять: от него зависит, сколько карт в матче."""
    assert texts.short_tournament(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Counter-Strike. IEM Пекин. Групповой этап. Bo3", "IEM Пекин · Bo3"),
        ("Англия. Премьер-Лига. Сезон 26/27", "Премьер-Лига"),
        # вторая часть названия — не лига, а стадия: показываем лигу
        ("Премьер-Лига. 12-й тур", "Премьер-Лига"),
        ("Лига Чемпионов УЕФА. Общий этап", "ЛЧ · Общий этап"),
    ],
)
def test_web_tournament_label(raw: str, expected: str) -> None:
    assert web_short_tournament(raw) == expected


# ------------------------------------------------------------------ карточка матча
def _match(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 7,
        "team_a": "Арсенал",
        "team_b": "Челси",
        "tournament": "Англия. Премьер-Лига. Сезон 26/27",
        "start_time": 1762340400,
        "odds_a": 2.1,
        "odds_b": 3.55,
        "status": "live",
        "winner": None,
        "score_a": 1,
        "score_b": 2,
        "sport": "football",
    }
    base.update(over)
    return base


def test_football_card_has_ball_icon_and_goals_label() -> None:
    card = texts.match_card(_match())
    assert "⚽️" in card
    assert "🎮" not in card
    assert "Счёт: <b>1:2</b>" in card       # не «счёт по картам»


def test_cs2_card_counts_maps() -> None:
    card = texts.match_card(_match(sport="cs2", tournament="Counter-Strike. IEM Пекин. Bo3"))
    assert "🎮" in card and "IEM Пекин · Bo3" in card
    assert "Счёт по картам: <b>1:2</b>" in card


# ------------------------------------------------------------------ тексты возврата
def test_map_refund_does_not_say_match_cancelled() -> None:
    """Карту в серии не играли — матч при этом состоялся, так и пишем."""
    text = texts.bet_result(
        won=False, void=True, team_a="MOUZ", team_b="PARIVISION", picked="MOUZ",
        amount=100, odds_value=1.9, payout=100, balance=1000, market_label="Карта 3",
    )
    assert "Карту не играли" in text
    assert "Матч отменён" not in text


def test_match_refund_says_match_cancelled() -> None:
    text = texts.bet_result(
        won=False, void=True, team_a="Брага", team_b="Тун", picked="Тун",
        amount=60, odds_value=4.0, payout=60, balance=1000,
    )
    assert "Матч отменён" in text


# ------------------------------------------------------- история операций (леджер)
@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "debug.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


async def _match_id(db: Database, fonbet_id: int) -> int:
    row, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="PARIVISION",
        team_b="Natus Vincere",
        start_time=int(time.time()) + 3600,
        odds_a=2.35,
        odds_b=1.58,
    )
    return int(row["id"])


async def test_ledger_shows_teams_instead_of_match_number(db: Database) -> None:
    """В истории операций игрок должен видеть матч, а не «матч #5»."""
    match_id = await _match_id(db, 9101)
    await db.place_bet(match_id, 1, None, "a", 100)

    row = await db._fetchone("SELECT reason FROM ledger WHERE user_id = 1 ORDER BY id DESC")
    assert row is not None
    reason = str(row["reason"])
    assert "PARIVISION — Natus Vincere" in reason
    assert "#" not in reason


async def test_ledger_payout_and_map_bet_are_readable(db: Database) -> None:
    match_id = await _match_id(db, 9102)
    card, _ = await db.upsert_map(
        match_id=match_id, fonbet_id=91_002, map_number=2, odds_a=1.9, odds_b=1.9
    )
    await db.place_map_bet(int(card["id"]), 1, None, "a", 100)
    await db.settle_map(int(card["id"]), "a", source="test")

    reasons = [
        str(r["reason"])
        for r in await db._fetchall("SELECT reason FROM ledger WHERE user_id = 1 ORDER BY id")
    ]
    assert "ставка · карта 2 · PARIVISION — Natus Vincere" in reasons
    assert "выплата · карта 2 · PARIVISION — Natus Vincere" in reasons

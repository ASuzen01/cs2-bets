"""Перепроверка расчётов за последние часы: футбол по фиду, CS2 по картам."""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.recheck import recheck_recent, report_text
from app.services.results import FootballResult


class FakeResults:
    def __init__(self, result: FootballResult | None = None) -> None:
        self.result = result
        self.calls = 0

    async def find(self, *args, **kwargs) -> FootballResult | None:
        self.calls += 1
        return self.result

    async def day(self, *args, **kwargs):
        return []

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.recheck.notify_bettors", _noop)
    monkeypatch.setattr("app.services.maps.notify_map_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "recheck.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


def _ft(score: tuple[int, int], *, status: str = "FT") -> FootballResult:
    return FootballResult(
        home="AEK Athens",
        away="Real Madrid",
        score_home=score[0],
        score_away=score[1],
        final_home=score[0],
        final_away=score[1],
        status=status,
        start_ts=int(time.time()) - 3 * 3600,
    )


async def _football(db: Database) -> int:
    row, _ = await db.upsert_match(
        fonbet_id=770001,
        tournament="Лига Чемпионов УЕФА. Общий этап",
        team_a="АЕК Афины",
        team_b="Реал Мадрид",
        start_time=int(time.time()) + 1800,
        odds_a=6.4,
        odds_x=4.2,
        odds_b=1.24,
        sport="football",
    )
    match_id = int(row["id"])
    await db.place_football_bet(match_id, 1, None, "result", "2", 100)
    await db.conn.execute(
        "UPDATE matches SET start_time = ? WHERE id = ?", (int(time.time()) - 3 * 3600, match_id)
    )
    await db.conn.commit()
    return match_id


async def test_football_wrong_result_is_fixed(db: Database) -> None:
    """Матч посчитали 1:0 по live-счёту, а по факту 2:3 — перепроверка исправляет."""
    match_id = await _football(db)
    await db.settle_football(match_id, 1, 0, source="fonbet")
    assert await db.get_balance(1) == 900          # ставка на Реал проиграла

    report = await recheck_recent(db, None, FakeResults(_ft((2, 3))), hours=48)

    assert len(report["football"]) == 1
    match = await db.get_match(match_id)
    assert (int(match["score_a"]), int(match["score_b"])) == (2, 3)
    assert str(match["winner"]) == "b"
    assert await db.get_balance(1) == 900 + round(100 * 1.24)


async def test_football_correct_result_is_not_touched(db: Database) -> None:
    """Счёт совпадает с фактом — ничего не пересчитываем и не дёргаем игроков."""
    match_id = await _football(db)
    await db.settle_football(match_id, 2, 3, source="fonbet")
    balance = await db.get_balance(1)

    report = await recheck_recent(db, None, FakeResults(_ft((2, 3))), hours=48)

    assert report["football"] == []
    assert await db.get_balance(1) == balance


async def test_football_postponed_match_is_refunded(db: Database) -> None:
    """Фид говорит «перенесён» — возвращаем ставки (матч реально не состоялся)."""
    match_id = await _football(db)
    await db.set_status(match_id, "awaiting")

    report = await recheck_recent(db, None, FakeResults(_ft((0, 0), status="Postp.")), hours=48)

    assert len(report["football"]) == 1
    assert str((await db.get_match(match_id))["status"]) == "cancelled"
    assert await db.get_balance(1) == 1000


async def test_cs2_series_is_fixed_by_maps(db: Database) -> None:
    """Серию закрыли не тем победителем — перепроверка считает по картам."""
    row, _ = await db.upsert_match(
        fonbet_id=770002,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Team Liquid",
        team_b="TYLOO",
        start_time=int(time.time()) - 4 * 3600,
        odds_a=1.6,
        odds_b=2.3,
    )
    match_id = int(row["id"])
    await db.set_status(match_id, "live")
    await db.place_bet(match_id, 1, None, "b", 100)      # ставили на TYLOO
    await db.settle_match(match_id, "a", source="fonbet")
    assert await db.get_balance(1) == 900

    # по картам: TYLOO взял первую и вторую (13:9, 16:14) — серия 0:2
    for number, score in enumerate([(9, 13), (14, 16)], start=1):
        await db.upsert_map(
            match_id=match_id,
            fonbet_id=880000 + number,
            map_number=number,
            odds_a=1.6,
            odds_b=2.3,
            score_a=score[0],
            score_b=score[1],
        )

    report = await recheck_recent(db, None, FakeResults(None), hours=48)

    assert len(report["cs2"]) == 1
    match = await db.get_match(match_id)
    assert str(match["winner"]) == "b"
    assert await db.get_balance(1) == 900 + round(100 * 2.3)


async def test_cs2_series_without_full_maps_is_left_alone(db: Database) -> None:
    """Карт недостаточно, чтобы закрыть серию, — не гадаем."""
    row, _ = await db.upsert_match(
        fonbet_id=770003,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) - 2 * 3600,
        odds_a=1.8,
        odds_b=2.0,
    )
    match_id = int(row["id"])
    await db.upsert_map(
        match_id=match_id,
        fonbet_id=880010,
        map_number=1,
        odds_a=1.8,
        odds_b=2.0,
        score_a=13,
        score_b=9,
    )

    report = await recheck_recent(db, None, FakeResults(None), hours=48)

    assert report["cs2"] == []
    assert str((await db.get_match(match_id))["status"]) == "open"


def test_report_text_lists_corrections() -> None:
    text = report_text(
        {
            "hours": 48,
            "checked": 12,
            "football": ["АЕК Афины — Реал Мадрид: было 1:0 (finished), по факту 2:3"],
            "cs2": [],
            "maps_repaired": 0,
            "maps_settled": 2,
        }
    )
    assert "Перепроверка за 48 ч" in text
    assert "АЕК" in text and "Карт рассчитано: 2" in text

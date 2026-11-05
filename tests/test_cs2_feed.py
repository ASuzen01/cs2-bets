"""CS2: результат серии и карт из внешнего фида (bo3.gg)."""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.cs2_results import (
    Cs2Game,
    Cs2Match,
    Cs2ResultsClient,
    canonical,
    same_cs2_team,
)
from app.services.cs2_settle import settle_from_feed
from app.services.recheck import recheck_recent

NOW = int(time.time())


class FakeCs2Results:
    """Фид, который знает ровно одну серию."""

    def __init__(self, match: Cs2Match | None) -> None:
        self.match = match
        self.calls = 0

    async def find(self, *args, **kwargs) -> Cs2Match | None:
        self.calls += 1
        return self.match

    async def finished(self, *args, **kwargs):
        return [self.match] if self.match else []

    async def games_of(self, *args, **kwargs):
        return self.match.games if self.match else []

    async def close(self) -> None:
        return None


class FakeFootball:
    """Футбольный фид в этих тестах ничего не знает."""

    async def find(self, *args, **kwargs):
        return None

    async def day(self, *args, **kwargs):
        return []

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.recheck.notify_bettors", _noop)
    monkeypatch.setattr("app.services.cs2_settle.notify_bettors", _noop)
    monkeypatch.setattr("app.services.cs2_settle.notify_map_bettors", _noop)
    monkeypatch.setattr("app.services.maps.notify_map_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "cs2feed.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


def _navi_falcons(start_ts: int | None = None) -> Cs2Match:
    """NaVi 2:1 Falcons: карты 13:11 (Falcons), 13:4 и 13:3 (NaVi)."""
    return Cs2Match(
        id=133042,
        team_a="Natus Vincere",
        team_b="Falcons",
        score_a=2,
        score_b=1,
        start_ts=start_ts or NOW - 5 * 3600,
        games=[
            Cs2Game(number=1, winner="Team Falcons", loser="Natus Vincere", winner_score=13, loser_score=11),
            Cs2Game(number=2, winner="Natus Vincere", loser="Team Falcons", winner_score=13, loser_score=4),
            Cs2Game(number=3, winner="Natus Vincere", loser="Team Falcons", winner_score=13, loser_score=3),
        ],
    )


async def _series(db: Database, *, status: str = "awaiting", still_open: bool = False) -> int:
    """Матч, который уже сыгран, но у нас висит без результата."""
    row, _ = await db.upsert_match(
        fonbet_id=900001,
        tournament="Counter-Strike. IEM Пекин. Плей-офф. Bo3",
        team_a="Natus Vincere",
        team_b="Team Falcons",
        start_time=NOW + 3600,
        odds_a=1.9,
        odds_b=1.85,
    )
    match_id = int(row["id"])
    for number in (1, 2, 3):
        await db.upsert_map(
            match_id=match_id,
            fonbet_id=990000 + number,
            map_number=number,
            odds_a=1.85 if number == 1 else 2.12,
            odds_b=1.95 if number == 1 else 1.72,
        )
    if not still_open:
        await _started(db, match_id, status)
    return match_id


async def _book(db: Database) -> int:
    """Матч со ставками ещё в линии (ставки принимаются только до старта)."""
    return await _series(db, still_open=True)


async def _started(db: Database, match_id: int, status: str = "awaiting") -> None:
    """Ставки сделаны — теперь матч стартовал (и, по фиду, уже сыгран)."""
    await db.conn.execute(
        "UPDATE matches SET start_time = ? WHERE id = ?", (NOW - 5 * 3600, match_id)
    )
    await db.conn.commit()
    await db.set_status(match_id, status)


# ------------------------------------------------------------------ названия
def test_ru_and_en_team_names_are_the_same_team() -> None:
    assert canonical("BB Team") == canonical("BetBoom") == "betboom"
    assert same_cs2_team("NAVI", "Natus Vincere")
    assert same_cs2_team("Team Falcons", "Falcons")
    assert same_cs2_team("G2 Esports", "G2")
    assert not same_cs2_team("MOUZ", "Aurora")


def test_client_finds_series_by_names_and_time(monkeypatch) -> None:
    client = Cs2ResultsClient()
    match = _navi_falcons()

    async def _finished(*args, **kwargs):
        return [match]

    async def _games(*args, **kwargs):
        return match.games

    monkeypatch.setattr(client, "finished", _finished)
    monkeypatch.setattr(client, "games_of", _games)

    import asyncio

    found = asyncio.run(client.find("Natus Vincere", "Team Falcons", match.start_ts))
    assert found is not None and found.score_text == "2:1"
    assert [game.number for game in found.games] == [1, 2, 3]

    # чужая серия: другие команды и время
    assert asyncio.run(client.find("MOUZ", "PARIVISION", match.start_ts)) is None


# --------------------------------------------------------------- расчёт серии
async def test_series_and_maps_are_settled_from_feed(db: Database) -> None:
    """Серию и карты считает фид: ставки ждали результата — самое время их закрыть."""
    match_id = await _book(db)
    await db.place_bet(match_id, 1, None, "a", 100)               # NaVi на матч
    cards = await db.match_maps(match_id)
    await db.place_map_bet(int(cards[0]["id"]), 1, None, "b", 50)  # Falcons на карту 1
    await db.place_map_bet(int(cards[2]["id"]), 1, None, "a", 60)  # NaVi на карту 3
    await _started(db, match_id)

    result = await settle_from_feed(db, None, await db.get_match(match_id), FakeCs2Results(_navi_falcons()))

    assert result is not None and result["settled"]
    match = await db.get_match(match_id)
    assert str(match["status"]) == "finished"
    assert str(match["winner"]) == "a"
    assert (int(match["score_a"]), int(match["score_b"])) == (2, 1)

    cards = await db.match_maps(match_id)
    assert [str(card["status"]) for card in cards] == ["finished", "finished", "finished"]
    assert [str(card["winner"]) for card in cards] == ["b", "a", "a"]
    assert [(int(c["score_a"]), int(c["score_b"])) for c in cards] == [(11, 13), (13, 4), (13, 3)]

    # 100 @1.90 (NaVi, матч) + 50 @1.95 (Falcons, карта 1) + 60 @2.12 (NaVi, карта 3)
    assert await db.get_balance(1) == 1000 - 210 + round(100 * 1.90) + round(50 * 1.95) + round(60 * 2.12)


async def test_unknown_series_is_left_alone(db: Database) -> None:
    match_id = await _series(db)
    before = await db.get_match(match_id)

    assert await settle_from_feed(db, None, before, FakeCs2Results(None)) is None
    after = await db.get_match(match_id)
    assert str(after["status"]) == "awaiting"
    assert await db.get_balance(1) == 1000


async def test_recheck_uses_feed_for_stuck_series(db: Database) -> None:
    """Матч висел в «ждём результат» — перепроверка закрывает его по фиду."""
    match_id = await _book(db)
    await db.place_bet(match_id, 1, None, "b", 100)               # Falcons на матч
    await _started(db, match_id)

    report = await recheck_recent(
        db, None, FakeFootball(), hours=48, cs2_results=FakeCs2Results(_navi_falcons())
    )

    assert len(report["cs2"]) == 1
    assert report["maps_settled"] == 3
    assert str((await db.get_match(match_id))["status"]) == "finished"
    assert await db.get_balance(1) == 900                          # Falcons проиграли

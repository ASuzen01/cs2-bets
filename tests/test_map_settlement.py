"""Ставки на карты: сыгранная карта считается, неначатая возвращается.

Раньше карта, у которой из линии пропали данные, уходила в «ждём результат» и в
итоге получала возврат «матч не сыгран» вместе с закрытием серии — даже если все
раунды на ней были сыграны.
"""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.fonbet import FonbetMap
from app.services.maps import finish_maps_of_match
from app.services.sync import MatchSyncer


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.maps.notify_map_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "mapsettle.db", start_balance=1000)
    await database.connect()
    for user_id in (1, 2):
        await database.upsert_user(user_id, f"user{user_id}", f"User {user_id}")
    yield database
    await database.close()


async def _match(db: Database, *, status: str = "live") -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=900,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) - 30 * 60,
        odds_a=1.65,
        odds_b=2.25,
    )
    if status != "open":
        await db.set_status(int(row["id"]), status)
    return row


async def _map(
    db: Database,
    match_id: int,
    *,
    number: int = 1,
    score: tuple[int | None, int | None] = (None, None),
    status: str = "live",
) -> dict:
    row, _ = await db.upsert_map(
        match_id=match_id,
        fonbet_id=100000 + number,
        map_number=number,
        odds_a=1.70,
        odds_b=2.10,
        score_a=score[0],
        score_b=score[1],
    )
    if status != "open":
        await db.set_map_status(int(row["id"]), status)
    return row


async def test_played_map_is_settled_not_refunded(db: Database) -> None:
    """Карта сыграна 13:7 — ставка на неё рассчитывается, а не возвращается."""
    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(13, 7))
    await db.place_map_bet(int(card["id"]), 1, None, "a", 100)

    done = await finish_maps_of_match(db, None, int(match["id"]))

    assert done == {"settled": 1, "refunded": 0, "waiting": 0}
    settled = await db.get_map(int(card["id"]))
    assert settled["status"] == "finished" and settled["winner"] == "a"
    assert await db.get_balance(1) == 1070          # 1000 - 100 + 170


async def test_map_with_overtime_is_settled(db: Database) -> None:
    """Овертайм 16:14 — карта доиграна, ставка играет."""
    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(14, 16))
    await db.place_map_bet(int(card["id"]), 1, None, "b", 100)

    await finish_maps_of_match(db, None, int(match["id"]))

    settled = await db.get_map(int(card["id"]))
    assert settled["winner"] == "b"
    assert await db.get_balance(1) == 1110


async def test_unplayed_map_is_refunded(db: Database) -> None:
    """Серия кончилась 2:0 — карту, на которой не было ни одного раунда, возвращаем."""
    match = await _match(db)
    card = await _map(db, int(match["id"]), number=3, status="open")
    await db.place_map_bet(int(card["id"]), 1, None, "a", 100)

    done = await finish_maps_of_match(db, None, int(match["id"]))

    assert done["refunded"] == 1
    assert str((await db.get_map(int(card["id"])))["status"]) == "cancelled"
    assert await db.get_balance(1) == 1000


async def test_map_with_partial_score_waits_for_result(db: Database) -> None:
    """Счёт 9:3 есть, победителя не видно — возврата нет, карта ждёт /maps."""
    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(9, 3))
    await db.place_map_bet(int(card["id"]), 1, None, "a", 100)

    done = await finish_maps_of_match(db, None, int(match["id"]))

    assert done["waiting"] == 1
    assert str((await db.get_map(int(card["id"])))["status"]) == "awaiting"
    assert await db.get_balance(1) == 900


async def test_live_map_bet_allowed_when_match_awaits_result(db: Database) -> None:
    """Серия в «ждём результат», а карта идёт — ставить на карту можно."""
    match = await _match(db, status="awaiting")
    card = await _map(db, int(match["id"]), number=2, status="live")

    balance, odds = await db.place_map_bet(int(card["id"]), 1, None, "a", 100)

    assert balance == 900 and odds == 1.70


async def test_sync_settles_map_by_round_score(db: Database) -> None:
    """Счёт 13:6 из линии — уже победа, флаг finished от Fonbet не нужен."""
    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(13, 6))
    await db.place_map_bet(int(card["id"]), 2, None, "a", 100)

    feed = {
        int(match["fonbet_id"]): [
            FonbetMap(
                event_id=100000 + 1,
                parent_id=int(match["fonbet_id"]),
                map_number=1,
                odds_a=1.70,
                odds_b=2.10,
                score_a=13,
                score_b=6,
                finished=False,
            )
        ]
    }
    stats = {"maps_settled": 0}
    await MatchSyncer(bot=None, db=db)._settle_maps(feed, stats)

    assert stats["maps_settled"] == 1
    assert str((await db.get_map(int(card["id"])))["status"]) == "finished"
    assert await db.get_balance(2) == 1070


async def test_sync_keeps_map_open_for_live_betting_when_feed_lost_it(db: Database) -> None:
    """Карта пропала из линии, но не доиграна — статус остаётся live, приём открыт."""
    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(6, 4))

    stats = {"maps_settled": 0}
    await MatchSyncer(bot=None, db=db)._settle_maps({}, stats)

    assert str((await db.get_map(int(card["id"])))["status"]) == "live"
    balance, _ = await db.place_map_bet(int(card["id"]), 1, None, "b", 50)
    assert balance == 950

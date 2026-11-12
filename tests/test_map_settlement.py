"""Ставки на карты: сыгранная карта считается, неначатая возвращается.

Раньше карта, у которой из линии пропали данные, уходила в «ждём результат» и в
итоге получала возврат «матч не сыгран» вместе с закрытием серии — даже если все
раунды на ней были сыграны.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.db import Database
from app.services.bulk_sync import bulk_upsert_maps
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


async def test_admin_can_manually_settle_a_live_map_while_series_continues(
    db: Database, monkeypatch
) -> None:
    """Админ может вручную закрыть закончившуюся карту, не завершая серию."""
    from app.handlers import admin

    match = await _match(db, status="live")
    card = await _map(db, int(match["id"]), score=(12, 10))
    await db.place_map_bet(int(card["id"]), 1, None, "a", 100)
    monkeypatch.setattr(admin, "settings", SimpleNamespace(admin_ids_set={42}))

    class FakeMessage:
        def __init__(self):
            self.answers = []
            self.edited = None

        async def answer(self, text, **kwargs):
            self.answers.append((text, kwargs))

        async def edit_text(self, text, **kwargs):
            self.edited = text

    message = FakeMessage()
    await admin.cmd_maps(message, db)
    card_message = next(item for item in message.answers if item[1].get("reply_markup"))
    markup = card_message[1]["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"ams:{card['id']}:a"

    callback = SimpleNamespace(
        data=f"ams:{card['id']}:a",
        from_user=SimpleNamespace(id=42),
        bot=object(),
        message=message,
        answer=lambda *args, **kwargs: None,
    )

    async def _answer(*args, **kwargs):
        return None

    callback.answer = _answer
    monkeypatch.setattr(admin, "notify_map_bettors", _answer)
    await admin.on_map_confirm(callback, db)

    settled = await db.get_map(int(card["id"]))
    still_live = await db.get_match(int(match["id"]))
    assert settled["status"] == "finished" and settled["winner"] == "a"
    assert still_live["status"] == "live" and still_live["winner"] is None
    assert await db.get_balance(1) == 1070
    assert "рассчитана" in message.edited


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


async def test_final_map_score_settles_while_series_is_still_live(db: Database) -> None:
    """Финальный счёт карты сам по себе рассчитывает её, не закрывая серию."""
    match = await _match(db, status="live")
    card = await _map(db, int(match["id"]), score=(12, 12))
    await db.place_map_bet(int(card["id"]), 2, None, "b", 100)

    # Никакого отдельного bulk-upsert перед расчётом: проверяем fallback-путь,
    # где финальный счёт впервые приходит прямо в _settle_maps.
    feed_card = FonbetMap(
        event_id=100001,
        parent_id=int(match["fonbet_id"]),
        map_number=1,
        odds_a=1.70,
        odds_b=2.10,
        score_a=14,
        score_b=16,
        finished=False,
    )
    stats = {"maps_settled": 0}
    await MatchSyncer(bot=None, db=db)._settle_maps(
        {int(match["fonbet_id"]): [feed_card]}, stats
    )

    settled = await db.get_map(int(card["id"]))
    still_live = await db.get_match(int(match["id"]))
    assert stats["maps_settled"] == 1
    assert settled["status"] == "finished" and settled["winner"] == "b"
    assert (settled["score_a"], settled["score_b"]) == (14, 16)
    assert still_live["status"] == "live" and still_live["winner"] is None
    assert await db.get_balance(2) == 1110


async def test_awaiting_map_settles_when_later_feed_has_final_score(db: Database) -> None:
    """Ожидающая карта получает финальный счёт позже и автоматически рассчитывается."""
    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(12, 12))
    await db.place_map_bet(int(card["id"]), 2, None, "b", 100)
    await db.set_map_status(int(card["id"]), "awaiting")

    feed_card = FonbetMap(
        event_id=100001,
        parent_id=int(match["fonbet_id"]),
        map_number=1,
        odds_a=9.9,
        odds_b=9.8,
        score_a=14,
        score_b=16,
        finished=False,
    )
    await bulk_upsert_maps(
        db,
        [{
            "match_id": int(match["id"]),
            "fonbet_id": feed_card.event_id,
            "map_number": feed_card.map_number,
            "odds_a": feed_card.odds_a,
            "odds_b": feed_card.odds_b,
            "score_a": feed_card.score_a,
            "score_b": feed_card.score_b,
        }],
    )

    stats = {"maps_settled": 0}
    await MatchSyncer(bot=None, db=db)._settle_maps(
        {int(match["fonbet_id"]): [feed_card]}, stats
    )

    settled = await db.get_map(int(card["id"]))
    assert stats["maps_settled"] == 1
    assert settled["status"] == "finished" and settled["winner"] == "b"
    assert (settled["score_a"], settled["score_b"]) == (14, 16)
    assert (float(settled["odds_a"]), float(settled["odds_b"])) == (1.70, 2.10)
    assert await db.get_balance(2) == 1110


async def test_sync_keeps_map_open_for_live_betting_when_feed_lost_it(db: Database) -> None:
    """Карта пропала из линии, но не доиграна — статус остаётся live, приём открыт."""
    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(6, 4))

    stats = {"maps_settled": 0}
    await MatchSyncer(bot=None, db=db)._settle_maps({}, stats)

    assert str((await db.get_map(int(card["id"])))["status"]) == "live"
    balance, _ = await db.place_map_bet(int(card["id"]), 1, None, "b", 50)
    assert balance == 950


async def test_refunded_map_with_score_is_recounted(db: Database) -> None:
    """Карту вернули как «не сыгранную», а счёт 13:9 есть — пересчитываем по факту."""
    from app.services.maps import repair_refunded_maps

    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(13, 9))
    await db.place_map_bet(int(card["id"]), 1, None, "a", 100)
    await db.cancel_map(int(card["id"]))                       # ошибочный возврат
    assert await db.get_balance(1) == 1000

    done = await repair_refunded_maps(db, None)

    assert done["settled"] == 1 and done["paid"] == 170
    settled = await db.get_map(int(card["id"]))
    assert settled["status"] == "finished" and settled["winner"] == "a"
    assert await db.get_balance(1) == 1070


async def test_refunded_map_without_decided_score_is_left_alone(db: Database) -> None:
    """Счёт 9:3 победителя не даёт — возврат не трогаем, карту добивает админ."""
    from app.services.maps import repair_refunded_maps

    match = await _match(db)
    card = await _map(db, int(match["id"]), score=(9, 3))
    await db.place_map_bet(int(card["id"]), 1, None, "a", 100)
    await db.cancel_map(int(card["id"]))

    done = await repair_refunded_maps(db, None)

    assert done["settled"] == 0
    assert str((await db.get_map(int(card["id"])))["status"]) == "cancelled"
    assert await db.get_balance(1) == 1000

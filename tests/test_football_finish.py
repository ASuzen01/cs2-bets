"""Расчёт футбольных матчей, которые Fonbet убрал из линии.

Завершённые события Fonbet выбрасывает из /events/listBase, а /events/event по ним
возвращает пустоту — финальный счёт взять негде. Поэтому финалом считаем последний
счёт, увиденный в live (он лежит в базе), но только когда событие пропало и его нет
пять минут: за это время счёт уже не изменится.
"""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.football import FootballMatch
from app.services.sync import FOOTBALL_FINISH_MINUTES, MatchSyncer


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    """В тестах бота нет: уведомления игрокам не отправляем."""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.sync.notify_bettors", _noop)
    monkeypatch.setattr("app.services.sync.notify_map_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "football_settle.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


async def _finished_match(db: Database, *, minutes_ago: int = 150) -> dict:
    """Матч, который мы видели в live со счётом 2:1 и который ушёл из линии."""
    feilds = {
        "fonbet_id": 424242,
        "tournament": "Лига Чемпионов УЕФА. Общий этап",
        "team_a": "Барселона",
        "team_b": "Астон Вилла",
        "odds_a": 1.8,
        "odds_x": 3.6,
        "odds_b": 4.2,
        "sport": "football",
    }
    # пока матч в линии (не начался) — на него можно ставить
    row, _ = await db.upsert_match(start_time=int(time.time()) + 3600, **feilds)
    await db.place_football_bet(int(row["id"]), 1, None, "result", "1", 100)

    # матч прошёл: счёт 2:1, он ушёл из линии
    row, _ = await db.upsert_match(
        start_time=int(time.time()) - minutes_ago * 60, score_a=2, score_b=1, **feilds
    )
    await db.set_status(int(row["id"]), "live")
    return row


def _syncer(db: Database) -> MatchSyncer:
    return MatchSyncer(bot=None, db=db)


async def test_gone_match_settles_after_quiet_period(db: Database) -> None:
    row = await _finished_match(db)
    assert await db.get_balance(1) == 900

    syncer = _syncer(db)
    stats = {"settled": 0, "awaiting": 0}

    # первый тик: матча в линии нет — только помечаем и ждём
    await syncer._settle_football([], stats)
    assert stats["settled"] == 0
    assert (await db.match_by_fonbet(424242))["status"] == "live"

    # «пять минут тишины» прошло — считаем последний счёт финальным
    await db.mark_result_check(int(row["id"]), int(time.time()) - 600)
    await syncer._settle_football([], stats)
    assert stats["settled"] == 1

    match = await db.match_by_fonbet(424242)
    assert str(match["status"]) == "finished"
    assert match["winner"] == "a"
    bet = (await db.match_bets(int(row["id"])))[0]
    assert await db.get_balance(1) == 900 + int(100 * float(bet["odds"]))


async def test_young_match_waits_for_result(db: Database) -> None:
    """Матч стартовал 30 минут назад: он просто ещё не в линии, расчитывать нечего."""
    row = await _finished_match(db, minutes_ago=30)
    await db.mark_result_check(int(row["id"]), int(time.time()) - 3600)

    stats = {"settled": 0, "awaiting": 0}
    await _syncer(db)._settle_football([], stats)

    assert stats["settled"] == 0
    assert (await db.match_by_fonbet(424242))["status"] == "live"
    assert FOOTBALL_FINISH_MINUTES == 100


async def test_match_without_score_is_not_settled(db: Database) -> None:
    """Счёт не видели (матч не был live) — считать не по чему, уходит в ожидание."""
    row, _ = await db.upsert_match(
        fonbet_id=424243,
        tournament="Лига Европы УЕФА. Общий этап",
        team_a="Рома",
        team_b="Лилль",
        start_time=int(time.time()) - 4 * 3600,
        odds_a=2.0,
        odds_x=3.3,
        odds_b=3.7,
        sport="football",
    )
    await db.mark_result_check(int(row["id"]), int(time.time()) - 3600)

    stats = {"settled": 0, "awaiting": 0}
    await _syncer(db)._settle_football([], stats)

    assert stats["settled"] == 0
    assert stats["awaiting"] == 1
    assert (await db.match_by_fonbet(424243))["status"] == "awaiting"


async def test_marker_is_not_moved_every_tick(db: Database) -> None:
    """Отметку «матч пропал» нельзя двигать на каждом тике — иначе тишина не истечёт."""
    row = await _finished_match(db)
    syncer = _syncer(db)
    stats = {"settled": 0, "awaiting": 0}

    await syncer._settle_football([], stats)                     # тик 1: отметили
    first = int((await db.match_by_fonbet(424242))["last_result_check"])
    assert first > 0

    await syncer._settle_football([], stats)                     # тик 2: отметка та же
    assert int((await db.match_by_fonbet(424242))["last_result_check"]) == first
    assert stats["settled"] == 0


async def test_seen_match_resets_marker(db: Database) -> None:
    """Матч снова в линии (например, его вернули) — тишину считаем заново."""
    row = await _finished_match(db)
    await db.mark_result_check(int(row["id"]), int(time.time()) - 3600)

    seen = FootballMatch(
        event_id=424242,
        tournament="Лига Чемпионов УЕФА. Общий этап",
        team_a="Барселона",
        team_b="Астон Вилла",
        start_time=int(time.time()) - 150 * 60,
        odds_1=1.8,
        odds_x=3.6,
        odds_2=4.2,
        score_a=2,
        score_b=1,
        is_live=True,
    )
    stats = {"settled": 0, "awaiting": 0}
    await _syncer(db)._settle_football([seen], stats)

    assert stats["settled"] == 0
    assert int((await db.match_by_fonbet(424242))["last_result_check"]) == 0
    assert (await db.match_by_fonbet(424242))["status"] == "live"

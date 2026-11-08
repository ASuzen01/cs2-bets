"""Расчёт футбольных матчей, которые Fonbet убрал из линии.

Fonbet выбрасывает завершённые события из линии и финальный счёт после этого не
отдаёт. Раньше мы брали последний live-счёт — и это ломало расчёт: гол на 90+4 или
в добавленное время менял исход уже после того, как мы расплатились. Теперь
финальный счёт спрашиваем у фида результатов, а без него матч ждёт (`awaiting`).
"""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.football import FootballMatch
from app.services.results import FootballResult
from app.services.sync import FOOTBALL_FINISH_MINUTES, MatchSyncer


class FakeResults:
    """Заглушка фида результатов: без сети, отдаёт заданный итог."""

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
    """В тестах бота нет: уведомления игрокам не отправляем."""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.sync.notify_bettors", _noop)
    monkeypatch.setattr("app.services.maps.notify_map_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "football_settle.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


async def _finished_match(db: Database, *, minutes_ago: int = 150) -> dict:
    """Матч, который мы видели в live со счётом 2:1 и который ушёл из линии."""
    fields = {
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
    row, _ = await db.upsert_match(start_time=int(time.time()) + 3600, **fields)
    await db.place_football_bet(int(row["id"]), 1, None, "result", "1", 100)

    # матч прошёл: счёт 2:1, он ушёл из линии
    row, _ = await db.upsert_match(
        start_time=int(time.time()) - minutes_ago * 60, score_a=2, score_b=1, **fields
    )
    await db.set_status(int(row["id"]), "live")
    return row


def _syncer(db: Database, results: FakeResults | None = None) -> MatchSyncer:
    return MatchSyncer(bot=None, db=db, results=results or FakeResults())


def _result(score: tuple[int, int], *, status: str = "FT") -> FootballResult:
    return FootballResult(
        home="Barcelona",
        away="Aston Villa",
        score_home=score[0],
        score_away=score[1],
        final_home=score[0],
        final_away=score[1],
        status=status,
        start_ts=int(time.time()) - 150 * 60,
    )


async def test_settles_by_feed_score_not_by_live_snapshot(db: Database) -> None:
    """Счёт в базе 2:1, а по факту 1:2 — платим по факту, а не по последнему live."""
    await _finished_match(db)
    assert await db.get_balance(1) == 900

    # ставили на «1» (Барселона), а Барселона проиграла
    syncer = _syncer(db, FakeResults(_result((1, 2))))
    stats = {"settled": 0, "awaiting": 0}
    await syncer._settle_football([], stats)

    assert stats["settled"] == 1
    match = await db.match_by_fonbet(424242)
    assert str(match["status"]) == "finished"
    assert match["winner"] == "b"
    assert (int(match["score_a"]), int(match["score_b"])) == (1, 2)
    assert await db.get_balance(1) == 900          # ставка проиграла — и это верно


async def test_uses_flashscore_when_livescore_has_no_match(db: Database) -> None:
    row = await _finished_match(db)
    primary = FakeResults(None)
    fallback_result = _result((1, 2))
    fallback_result.source = "flashscore"
    fallback = FakeResults(fallback_result)
    syncer = MatchSyncer(
        bot=None,
        db=db,
        results=primary,
        flashscore_results=fallback,  # type: ignore[arg-type]
    )

    stats = {"settled": 0, "awaiting": 0}
    await syncer._settle_football([], stats)

    assert primary.calls == fallback.calls == 1
    assert stats["settled"] == 1
    match = await db.get_match(int(row["id"]))
    assert (int(match["score_a"]), int(match["score_b"])) == (1, 2)
    assert str(match["result_source"]) == "flashscore"


async def test_settles_winning_bet_by_feed_score(db: Database) -> None:
    """Ставка на победителя заходит по финальному счёту из фида."""
    row = await _finished_match(db)
    bet = (await db.match_bets(int(row["id"])))[0]

    syncer = _syncer(db, FakeResults(_result((3, 1))))
    stats = {"settled": 0, "awaiting": 0}
    await syncer._settle_football([], stats)

    assert stats["settled"] == 1
    assert await db.get_balance(1) == 900 + int(100 * float(bet["odds"]))


async def test_without_result_match_waits(db: Database) -> None:
    """Фид ничего не знает — матч уходит в «ждём результат», а не считается наугад."""
    await _finished_match(db)

    syncer = _syncer(db, FakeResults(None))
    stats = {"settled": 0, "awaiting": 0}
    await syncer._settle_football([], stats)

    assert stats["settled"] == 0
    assert stats["awaiting"] == 1
    assert (await db.match_by_fonbet(424242))["status"] == "awaiting"
    assert await db.get_balance(1) == 900           # деньги в игре


async def test_postponed_match_is_refunded(db: Database) -> None:
    """Матч не состоялся (перенесён) — вот здесь возврат уместен."""
    await _finished_match(db)

    syncer = _syncer(db, FakeResults(_result((0, 0), status="Postp.")))
    stats = {"settled": 0, "awaiting": 0, "cancelled": 0}
    await syncer._settle_football([], stats)

    assert stats["cancelled"] == 1
    assert (await db.match_by_fonbet(424242))["status"] == "cancelled"
    assert await db.get_balance(1) == 1000


async def test_young_match_waits_for_result(db: Database) -> None:
    """Матч стартовал 30 минут назад: он просто ещё не в линии, расчитывать нечего."""
    row = await _finished_match(db, minutes_ago=30)
    await db.mark_result_check(int(row["id"]), int(time.time()) - 3600)
    feed = FakeResults(_result((1, 2)))

    stats = {"settled": 0, "awaiting": 0}
    await _syncer(db, feed)._settle_football([], stats)

    assert stats["settled"] == 0
    assert feed.calls == 0                          # фид даже не спрашивали
    assert (await db.match_by_fonbet(424242))["status"] == "live"
    assert FOOTBALL_FINISH_MINUTES == 100


async def test_feed_is_not_asked_every_tick(db: Database) -> None:
    """Матч без результата: фид переспрашиваем не чаще раза в десять минут."""
    await _finished_match(db)
    feed = FakeResults(None)
    syncer = _syncer(db, feed)
    stats = {"settled": 0, "awaiting": 0}

    await syncer._settle_football([], stats)
    assert feed.calls == 1

    await syncer._settle_football([], stats)        # сразу же — уже не спрашиваем
    assert feed.calls == 1


async def test_seen_match_resets_marker(db: Database) -> None:
    """Матч снова в линии (например, его вернули) — отметку сбрасываем."""
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

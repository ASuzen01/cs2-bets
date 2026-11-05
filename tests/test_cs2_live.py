"""Live CS2: счёт и ставки, когда Fonbet убрал событие из линии.

Fonbet по ходу матча снимает кэфы (и тогда событие уходит из выдачи), а после
финального свистка убирает его совсем и результат не отдаёт. Из-за этого лайв
раньше «замерзал»: матч висел без счёта, а через 45 минут уходил в «ждём
результат» — и ставки в лайве переставали приниматься.
"""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.fonbet import FonbetClient, parse_maps
from app.services.sync import MatchSyncer, decisive_score, format_minutes


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.sync.notify_bettors", _noop)
    monkeypatch.setattr("app.services.maps.notify_map_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "live.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


def _client() -> FonbetClient:
    return FonbetClient("https://example.invalid", include=["IEM"], exclude=[])


def _packet(*, odds: bool, score: tuple[int, int], place: str = "live") -> dict:
    factors = (
        [{"e": 500, "factors": [{"f": 921, "v": 1.8}, {"f": 923, "v": 2.0}]}] if odds else []
    )
    return {
        "sports": [
            {"id": 1, "name": "Counter-Strike. IEM Пекин. Групповой этап. Bo3", "kind": "segment"},
            {"id": 9, "name": "Counter-Strike. IEM Пекин. Групповой этап. Bo3. Карты", "kind": "segment"},
        ],
        "events": [
            {
                "id": 500,
                "level": 1,
                "sportId": 1,
                "team1": "Aurora",
                "team2": "G2 Esports",
                "startTime": 1_800_000_000,
                "place": place,
            },
            {
                "id": 501,
                "level": 2,
                "parentId": 500,
                "sportId": 9,
                "name": "2-я карта",
                "startTime": 1_800_000_000,
                "place": place,
            },
        ],
        "customFactors": factors,
        "eventMiscs": [
            {"id": 500, "score1": score[0], "score2": score[1]},
            {"id": 501, "score1": 6, "score2": 8},
        ],
        "liveEventInfos": [{"eventId": 500}] if place == "live" else [],
    }


def test_live_match_without_odds_is_still_parsed() -> None:
    """Кэфы сняли — но счёт нужен: матч обязан остаться в разборе линии."""
    client = _client()
    matches = client.parse(_packet(odds=False, score=(1, 0)))

    assert len(matches) == 1
    assert matches[0].odds_a is None and matches[0].odds_b is None
    assert (matches[0].score_a, matches[0].score_b) == (1, 0)
    assert matches[0].is_live


def test_line_match_without_odds_is_skipped() -> None:
    client = _client()
    assert client.parse(_packet(odds=False, score=(0, 0), place="line")) == []


def test_map_without_odds_keeps_score() -> None:
    """У идущей карты кэфов нет, а счёт по раундам есть — показываем его."""
    client = _client()
    packet = _packet(odds=False, score=(1, 0))
    cards = parse_maps(packet, client)[500]

    assert len(cards) == 1
    assert cards[0].map_number == 2
    assert cards[0].score_text == "6:8"
    assert cards[0].odds_a is None


async def test_old_odds_survive_when_line_drops_them(db: Database) -> None:
    row, _ = await db.upsert_match(
        fonbet_id=500,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + 600,
        odds_a=1.8,
        odds_b=2.0,
    )
    await db.upsert_match(
        fonbet_id=500,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) - 60,
        odds_a=None,
        odds_b=None,
        score_a=1,
        score_b=0,
    )

    fresh = await db.match_by_fonbet(500)
    assert float(fresh["odds_a"]) == 1.8      # кэф остался прежним
    assert (fresh["score_a"], fresh["score_b"]) == (1, 0)
    del row


def test_formats() -> None:
    assert format_minutes("... Bo1") == 90
    assert format_minutes("... Bo3") == 170
    assert format_minutes("... Bo5") == 260
    assert decisive_score("... Bo3", 2, 0)
    assert decisive_score("... Bo3", 2, 1)
    assert not decisive_score("... Bo3", 1, 1)
    assert not decisive_score("... Bo1", 13, 9)   # у Bo1 в счёте раунды


async def _live_match(db: Database, *, minutes: int, score: tuple[int, int], bo: str = "Bo3") -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=500,
        tournament=f"Counter-Strike. IEM Пекин. Групповой этап. {bo}",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) - minutes * 60,
        odds_a=1.8,
        odds_b=2.0,
        score_a=score[0],
        score_b=score[1],
    )
    await db.set_status(int(row["id"]), "live")
    return row


async def test_gone_match_with_won_series_is_settled(db: Database) -> None:
    row = await _live_match(db, minutes=120, score=(2, 0))
    await db.place_bet(int(row["id"]), 1, None, "a", 100)
    syncer = MatchSyncer(bot=None, db=db)
    stats = {"settled": 0, "awaiting": 0}

    await syncer._settle_gone_cs2({}, stats)              # первый тик — только отметка
    assert stats["settled"] == 0

    await db.mark_result_check(int(row["id"]), int(time.time()) - 600)
    await syncer._settle_gone_cs2({}, stats)
    assert stats["settled"] == 1

    match = await db.match_by_fonbet(500)
    assert str(match["status"]) == "finished"
    assert match["winner"] == "a"
    assert await db.get_balance(1) == 900 + 180


async def test_unfinished_match_stays_live_and_bettable(db: Database) -> None:
    """1:1 в Bo3 — матч идёт, приём не закрываем и в «ждём результат» не уводим."""
    row = await _live_match(db, minutes=100, score=(1, 1))
    stats = {"settled": 0, "awaiting": 0}

    await MatchSyncer(bot=None, db=db)._settle_gone_cs2({}, stats)

    assert stats == {"settled": 0, "awaiting": 0}
    match = await db.match_by_fonbet(500)
    assert str(match["status"]) == "live"
    assert await db.place_bet(int(row["id"]), 1, None, "a", 50)   # ставка проходит


async def test_expired_match_goes_to_manual_result(db: Database) -> None:
    await _live_match(db, minutes=200, score=(1, 1))     # Bo3 давно должен был кончиться
    stats = {"settled": 0, "awaiting": 0}

    await MatchSyncer(bot=None, db=db)._settle_gone_cs2({}, stats)

    assert stats["awaiting"] == 1
    assert str((await db.match_by_fonbet(500))["status"]) == "awaiting"


async def test_match_without_score_keeps_bets_after_format(db: Database) -> None:
    """Счёта не видели, но матч игрался: ставки остаются в игре до результата."""
    row, _ = await db.upsert_match(
        fonbet_id=600,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Team Liquid",
        team_b="TYLOO",
        start_time=int(time.time()) - 220 * 60,
        odds_a=1.7,
        odds_b=2.1,
    )
    await db.set_status(int(row["id"]), "live")     # матч идёт: live-ставки разрешены
    await db.place_bet(int(row["id"]), 1, None, "a", 100)     # ставка сделана в игре
    await db.set_status(int(row["id"]), "awaiting")
    stats = {"settled": 0, "awaiting": 0, "cancelled": 0}

    await MatchSyncer(bot=None, db=db)._settle_gone_cs2({}, stats)

    assert stats["cancelled"] == 0
    assert str((await db.match_by_fonbet(600))["status"]) == "awaiting"
    assert await db.get_balance(1) == 900       # ставка не возвращена


async def test_stuck_awaiting_without_score_is_never_refunded(db: Database) -> None:
    """Сыгранный матч без счёта НЕ отменяем: держим awaiting, пока не придёт результат."""
    row, _ = await db.upsert_match(
        fonbet_id=601,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="BB Team",
        team_b="Aurora",
        start_time=int(time.time()) - 130 * 60,
        odds_a=1.6,
        odds_b=2.2,
    )
    await db.set_status(int(row["id"]), "live")
    await db.place_bet(int(row["id"]), 1, None, "b", 100)
    await db.set_status(int(row["id"]), "awaiting")
    stats = {"settled": 0, "awaiting": 0, "cancelled": 0}

    await MatchSyncer(bot=None, db=db)._settle_gone_cs2({}, stats)

    assert stats["cancelled"] == 0
    assert str((await db.match_by_fonbet(601))["status"]) == "awaiting"
    # ставка остаётся в игре, деньги не возвращены
    assert await db.get_balance(1) == 900


async def test_stale_match_with_bets_is_not_cancelled(db: Database) -> None:
    """Зависший матч со ставками не снимаем: возврат только там, где ставить было некому."""
    row, _ = await db.upsert_match(
        fonbet_id=602,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Team Liquid",
        team_b="TYLOO",
        start_time=int(time.time()) - 30 * 3600,
        odds_a=1.7,
        odds_b=2.1,
    )
    await db.set_status(int(row["id"]), "live")
    await db.place_bet(int(row["id"]), 1, None, "a", 100)
    stats = {"settled": 0, "awaiting": 0, "cancelled": 0}

    await MatchSyncer(bot=None, db=db)._close_stale(stats)

    assert stats.get("cancelled", 0) == 0
    assert str((await db.match_by_fonbet(602))["status"]) == "awaiting"
    assert await db.get_balance(1) == 900


async def test_stale_match_without_bets_is_removed(db: Database) -> None:
    """Матч без единой ставки снимать с линии можно — возвращать нечего."""
    row, _ = await db.upsert_match(
        fonbet_id=603,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="HEROIC",
        team_b="B8",
        start_time=int(time.time()) - 30 * 3600,
        odds_a=1.7,
        odds_b=2.1,
    )
    await db.set_status(int(row["id"]), "live")
    stats = {"settled": 0, "awaiting": 0, "cancelled": 0}

    await MatchSyncer(bot=None, db=db)._close_stale(stats)

    assert stats["cancelled"] == 1
    assert str((await db.match_by_fonbet(603))["status"]) == "cancelled"

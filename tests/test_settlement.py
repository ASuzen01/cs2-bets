"""Автоматический расчёт: серия по картам, возврат несыгранных карт, экспресс без потолка."""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.sync import MatchSyncer


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "settle.db", start_balance=1000)
    await database.connect()
    for user_id in (1, 2):
        await database.upsert_user(user_id, f"user{user_id}", f"User {user_id}")
    yield database
    await database.close()


async def _match(db: Database, *, tournament: str = "Counter-Strike. IEM. Групповой этап. Bo3") -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=555,
        tournament=tournament,
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) - 600,
        odds_a=1.65,
        odds_b=2.25,
    )
    await db.set_status(int(row["id"]), "live")
    return row


async def _map(db: Database, match_id: int, number: int, odds_a: float = 1.7) -> dict:
    row, _ = await db.upsert_map(
        match_id=match_id,
        fonbet_id=9000 + number,
        map_number=number,
        odds_a=odds_a,
        odds_b=2.1,
    )
    return row


def _syncer(db: Database) -> MatchSyncer:
    return MatchSyncer(bot=None, db=db)  # bot не нужен: проверяем только расчёт


async def test_large_match_settlement_batches_payouts_without_changing_totals(db: Database) -> None:
    match = await _match(db)
    now = int(time.time())
    async with db.transaction() as conn:
        for index in range(250):
            user_id = 10_000 + index
            await conn.execute(
                "INSERT INTO users (user_id, balance, created_at, last_seen_at) VALUES (?, 1000, ?, ?)",
                (user_id, now, now),
            )
            await conn.execute(
                "INSERT INTO bets (match_id, user_id, side, amount, odds, created_at) "
                "VALUES (?, ?, 'a', 10, 2.0, ?)",
                (int(match["id"]), user_id, now),
            )

    result = await db.settle_match(int(match["id"]), "a")
    balances = await db._fetchall(
        "SELECT balance FROM users WHERE user_id >= 10000 ORDER BY user_id"
    )
    ledger = await db._fetchall(
        "SELECT delta FROM ledger WHERE user_id >= 10000 ORDER BY user_id"
    )

    assert result["bets"] == 250
    assert result["paid"] == 5_000
    assert len(result["payouts"]) == 250
    assert len(ledger) == 250 and all(int(row["delta"]) == 20 for row in ledger)
    assert len(balances) == 250 and all(int(row["balance"]) == 1_020 for row in balances)


def test_required_maps_by_format() -> None:
    assert MatchSyncer._required_maps("Counter-Strike. IEM. Bo1") == 1
    assert MatchSyncer._required_maps("Counter-Strike. IEM. Bo3") == 2
    assert MatchSyncer._required_maps("Counter-Strike. Major. Bo5") == 3
    assert MatchSyncer._required_maps("Counter-Strike. IEM") == 2  # по умолчанию Bo3


async def test_series_winner_from_maps(db: Database) -> None:
    match = await _match(db)
    first = await _map(db, int(match["id"]), 1)
    second = await _map(db, int(match["id"]), 2)

    syncer = _syncer(db)
    assert await syncer._series_from_maps(match) == (None, None)  # карты ещё не сыграны

    await db.settle_map(int(first["id"]), "a")
    assert await syncer._series_from_maps(match) == (None, None)  # 1:0 — мало для Bo3

    await db.settle_map(int(second["id"]), "a")
    assert await syncer._series_from_maps(match) == ("a", "2:0")


async def test_series_winner_from_maps_bo1(db: Database) -> None:
    match = await _match(db, tournament="Counter-Strike. IEM. Bo1")
    card = await _map(db, int(match["id"]), 1)
    await db.settle_map(int(card["id"]), "b")

    winner, score = await _syncer(db)._series_from_maps(match)
    assert (winner, score) == ("b", "0:1")


async def test_unplayed_map_returns_bets(db: Database) -> None:
    match = await _match(db)
    card = await _map(db, int(match["id"]), 3)          # третья карта не игралась
    await db.place_map_bet(int(card["id"]), 1, None, "a", 200)
    assert await db.get_balance(1) == 800

    result = await db.cancel_map(int(card["id"]))
    assert result["refunded"] == 200
    assert await db.get_balance(1) == 1000

    row = await db.get_map(int(card["id"]))
    assert row["status"] == "cancelled"
    bet = (await db.match_bets(int(match["id"])))[0]
    assert bet["payout"] == 200                          # возврат зафиксирован


async def test_express_has_no_odds_cap(db: Database) -> None:
    """Потолка 40 больше нет: экспресс с кэфом выше собирается."""
    legs = []
    for index in range(1, 4):
        row, _ = await db.upsert_match(
            fonbet_id=7000 + index,
            tournament="Counter-Strike. IEM. Bo3",
            team_a=f"A{index}",
            team_b=f"B{index}",
            start_time=int(time.time()) + 3600,
            odds_a=5.0,
            odds_b=1.1,
        )
        legs.append({"match_id": int(row["id"]), "side": "a"})

    express_id, odds, balance = await db.place_express(1, None, 100, legs, max_odds=0)
    assert express_id > 0
    assert odds == pytest.approx(125.0)                  # 5 × 5 × 5 — раньше упиралось в 40
    assert balance == 900


# ---------------------------------------------------------------- сквозной прогон
class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((int(chat_id), text))

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.photos.append((int(chat_id), caption or ""))

    async def edit_message_text(self, **kwargs):
        return None


class FakeClient:
    """Отдаёт заранее подготовленный пакет линии."""

    def __init__(self, packets: list[dict]) -> None:
        self.packets = packets

    async def fetch_packet(self) -> dict:
        return self.packets.pop(0) if len(self.packets) > 1 else self.packets[0]

    def parse(self, packet: dict):
        from app.services.fonbet import FonbetClient

        return FonbetClient("http://x", include=["IEM"], exclude=[]).parse(packet)

    async def close(self) -> None:
        return None


def _packet(*, finished: bool, rounds: tuple[int, int] = (13, 9), start_offset: int = 1200) -> dict:
    start = int(time.time()) - start_offset
    livе_info = []
    miscs = []
    if finished:
        likve_finished = True
        livе_info = [
            {"eventId": 555, "finished": likve_finished},
            {"eventId": 601, "finished": likve_finished},
            {"eventId": 602, "finished": likve_finished},
        ]
        miscs = [
            {"id": 601, "score1": rounds[0], "score2": rounds[1]},
            {"id": 602, "score1": rounds[0], "score2": rounds[1]},
        ]

    return {
        "sports": [{"id": 1, "name": "Counter-Strike. IEM Пекин. Групповой этап. Bo3"}],
        "events": [
            {"id": 555, "sportId": 1, "level": 1, "team1": "Aurora", "team2": "G2 Esports",
             "startTime": start},
            {"id": 601, "sportId": 1, "level": 2, "parentId": 555, "name": "1-я карта"},
            {"id": 602, "sportId": 1, "level": 2, "parentId": 555, "name": "2-я карта"},
        ],
        "customFactors": [
            {"e": 555, "factors": [{"f": 921, "v": 1.65}, {"f": 923, "v": 2.25}]},
            {"e": 601, "factors": [{"f": 921, "v": 1.70}, {"f": 923, "v": 2.10}]},
            {"e": 602, "factors": [{"f": 921, "v": 1.90}, {"f": 923, "v": 1.90}]},
        ],
        "eventMiscs": miscs,
        "liveEventInfos": livе_info,
    }


async def test_whole_flow_settles_match_by_maps(db: Database) -> None:
    """Главный баг: матч сам уходит из Live и считается по картам — без админа."""
    bot = FakeBot()
    syncer = MatchSyncer(bot, db)
    syncer.client = FakeClient([_packet(finished=False), _packet(finished=True)])

    await syncer.tick()                      # линия: матч + две карты
    match = (await db.matches_by_status(["open", "live"]))[0]
    assert match["status"] == "live"         # приём ставок при этом открыт
    await db.place_bet(int(match["id"]), 1, None, "a", 100)

    maps = await db.match_maps(int(match["id"]))
    await db.place_map_bet(int(maps[0]["id"]), 2, None, "a", 100)

    stats = await syncer.tick()              # карты доиграны → расчёт без человека
    assert stats["maps_settled"] == 2
    assert stats["settled"] == 1

    settled = await db.get_match(int(match["id"]))
    assert settled["status"] == "finished"
    assert settled["winner"] == "a"

    # ставки рассчитаны: списали 100 и вернули выплату
    assert await db.get_balance(1) == 1000 - 100 + round(100 * 1.65)
    assert await db.get_balance(2) == 1000 - 100 + round(100 * 1.70)

    # уведомления ушли в личку обоим
    winners = dict(bot.messages + bot.photos)
    assert 1 in winners and 2 in winners
    assert "сыграла" in winners[1].lower()


async def test_stale_match_with_bets_waits_for_result(db: Database) -> None:
    """Матч сутки висел без результата, но ставки есть — возврата не будет, ждём исход."""
    row, _ = await db.upsert_match(
        fonbet_id=888,
        tournament="Counter-Strike. IEM. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) - 26 * 3600,
        odds_a=1.65,
        odds_b=2.25,
    )
    await db.set_status(int(row["id"]), "live")
    await db.place_bet(int(row["id"]), 1, None, "a", 300)
    await db.set_status(int(row["id"]), "awaiting")

    bot = FakeBot()
    syncer = MatchSyncer(bot, db)
    stats: dict = {}
    await syncer._close_stale(stats)

    assert stats.get("cancelled", 0) == 0
    settled = await db.get_match(int(row["id"]))
    assert settled["status"] == "awaiting"                      # ждём фактический исход
    assert await db.get_balance(1) == 700                       # ставка осталась в игре
    assert not any("возвращена" in text.lower() for _, text in bot.messages)


async def test_express_without_leg_limit(db: Database) -> None:
    """Лимита в 6 исходов больше нет — собираем экспресс из восьми."""
    legs = []
    for index in range(1, 9):
        row, _ = await db.upsert_match(
            fonbet_id=8000 + index,
            tournament="Counter-Strike. IEM. Bo3",
            team_a=f"A{index}",
            team_b=f"B{index}",
            start_time=int(time.time()) + 3600,
            odds_a=1.5,
            odds_b=2.5,
        )
        legs.append({"match_id": int(row["id"]), "side": "a"})

    express_id, odds, balance = await db.place_express(1, None, 100, legs, max_odds=0)
    assert express_id > 0
    assert odds == pytest.approx(1.5 ** 8, rel=1e-3)
    assert balance == 900


async def test_map_bet_stays_open_after_match_finished(db: Database) -> None:
    """Матч посчитан, карта ещё нет — ставка на карту не должна считаться проигранной."""
    from app.web.payloads import bets_history

    match = await _match(db)
    card = await _map(db, int(match["id"]), 2)
    await db.place_map_bet(int(card["id"]), 1, None, "a", 100)
    await db.settle_match(int(match["id"]), "a")      # матч рассчитан, карта ещё ждёт

    data = await bets_history(db, 1)
    open_items = data["open"]
    assert len(open_items) == 1
    item = open_items[0]
    assert item["market"] == "map"
    assert item["market_label"] == "Карта 2"
    assert item["status"] == "open"
    assert data["settled"] == []                      # и в проигранные не попала

    active = await db.user_active_bets(1)
    assert [int(row["map_number"]) for row in active] == [2]


async def test_recount_cancelled_match_by_real_result(db: Database) -> None:
    """Отменили по ошибке — считаем заново по факту: возвраты откатываются, победитель получает выплату."""
    match = await _match(db)
    await db.place_bet(int(match["id"]), 1, None, "a", 100)
    await db.place_bet(int(match["id"]), 2, None, "b", 100)
    await db.cancel_match(int(match["id"]))
    assert await db.get_balance(1) == 1000
    assert await db.get_balance(2) == 1000

    result = await db.recount_match(int(match["id"]), "a")

    assert result["team"] == "Aurora"
    assert result["reversed"] == 200              # откатили оба возврата
    assert await db.get_balance(1) == 1000 - 100 + round(100 * 1.65)
    assert await db.get_balance(2) == 900         # возврат снят, ставка проиграла

    settled = await db.get_match(int(match["id"]))
    assert settled["status"] == "finished"
    assert settled["winner"] == "a"


async def test_recount_rejects_finished_match(db: Database) -> None:
    from app.db.database import BettingError

    match = await _match(db)
    await db.settle_match(int(match["id"]), "a")
    with pytest.raises(BettingError):
        await db.recount_match(int(match["id"]), "b")


async def test_stale_match_is_settled_not_cancelled_when_maps_known(db: Database) -> None:
    """Старый матч с известными картами считается по факту, а не отменяется."""
    row, _ = await db.upsert_match(
        fonbet_id=999,
        tournament="Counter-Strike. IEM. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) - 13 * 3600,
        odds_a=1.65,
        odds_b=2.25,
    )
    await db.set_status(int(row["id"]), "live")
    await db.place_bet(int(row["id"]), 1, None, "a", 100)
    for number in (1, 2):
        card = await _map(db, int(row["id"]), number)
        await db.settle_map(int(card["id"]), "a")

    bot = FakeBot()
    syncer = MatchSyncer(bot, db)
    stats: dict = {}
    await syncer._close_stale(stats)

    assert stats.get("settled") == 1
    assert stats.get("cancelled") in (None, 0)
    settled = await db.get_match(int(row["id"]))
    assert settled["status"] == "finished" and settled["winner"] == "a"
    assert await db.get_balance(1) == 1000 - 100 + round(100 * 1.65)


async def test_old_tracked_match_keeps_updating(db: Database) -> None:
    """Матч, который идёт дольше 6 часов, больше не выпадает из синка."""
    row, _ = await db.upsert_match(
        fonbet_id=555,
        tournament="Counter-Strike. IEM. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) - 8 * 3600,
        odds_a=1.10,
        odds_b=6.00,
    )
    await db.set_status(int(row["id"]), "live")

    syncer = MatchSyncer(FakeBot(), db)
    syncer.client = FakeClient([_packet(finished=False, start_offset=8 * 3600)])
    await syncer.tick()

    refreshed = await db.get_match(int(row["id"]))
    assert float(refreshed["odds_a"]) == pytest.approx(1.65)   # линия продолжает обновляться
    assert refreshed["status"] == "live"

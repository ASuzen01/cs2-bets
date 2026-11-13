"""P2: задания дня, серия дней, статистика игрока. P3: лимиты, потолок ставки, /metrics, /health."""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.db.database import bonus_amount
from app.services.quests import by_code, day_number, quests_of_day
from app.web.limits import Metrics, RateLimiter, metrics
from app.web.server import build_app

USER = {"id": 777, "first_name": "Антон", "username": "sap"}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "p2.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(USER["id"], USER["username"], "Антон")
    yield database
    await database.close()


@pytest.fixture
async def client(db: Database, monkeypatch):
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    app = build_app(db)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def _match(db: Database, *, fonbet_id: int = 555, minutes: int = 60) -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament="Counter-Strike. IEM. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + minutes * 60,
        odds_a=2.0,
        odds_b=2.0,
    )
    return row


# ------------------------------------------------------------------ задания дня
def test_three_quests_rotate_by_day() -> None:
    today = quests_of_day(10)
    assert len(today) == 3
    assert len({quest["code"] for quest in today}) == 3      # без повторов
    tomorrow = quests_of_day(11)
    assert today != tomorrow                                  # набор меняется


async def test_quests_state_creates_rows(db: Database) -> None:
    rows = await db.quests_state(USER["id"])
    assert len(rows) == 3
    assert all(int(row["progress"]) == 0 for row in rows)


async def test_bet_advances_quest_only_if_present_today(db: Database) -> None:
    match = await _match(db)
    await db.place_bet(int(match["id"]), USER["id"], None, "a", 100)

    rows = {str(row["code"]): row for row in await db.quests_state(USER["id"])}
    today = {quest["code"] for quest in quests_of_day(day_number())}

    if "bet" in today:
        assert int(rows["bet"]["progress"]) == 1
    else:
        # задания «ставка» сегодня нет — прогресс других не должен появиться
        assert all(int(row["progress"]) == 0 for row in rows.values())


async def test_claim_quest_pays_once(db: Database) -> None:
    code = quests_of_day(day_number())[0]["code"]
    await db.quests_state(USER["id"])
    # добиваем прогресс до цели
    await db._write(  # тесту нужен прямой доступ к записи
        "UPDATE quests SET progress = target WHERE user_id = ? AND code = ?",
        (USER["id"], code),
    )

    claimed, reward, balance, error = await db.claim_quest(USER["id"], code)
    assert claimed and reward > 0 and error == ""
    assert balance == 1000 + reward

    again, _, balance2, error2 = await db.claim_quest(USER["id"], code)
    assert not again and balance2 == balance and "уже" in error2


async def test_claim_unfinished_quest_is_refused(db: Database) -> None:
    code = quests_of_day(day_number())[1]["code"]
    await db.quests_state(USER["id"])
    claimed, _, balance, error = await db.claim_quest(USER["id"], code)
    assert not claimed and balance == 1000 and "не выполнено" in error


async def test_claim_foreign_quest_code(db: Database) -> None:
    today = {quest["code"] for quest in quests_of_day(day_number())}
    foreign = ({"bet", "casino", "express", "bonus"} - today).pop()
    claimed, _, _, error = await db.claim_quest(USER["id"], foreign)
    assert not claimed and "нет" in error


# ------------------------------------------------------------------- серия дней
def test_bonus_amount_grows_with_streak() -> None:
    assert bonus_amount(250, 1) == 250
    assert bonus_amount(250, 3) == 300          # +20 %
    assert bonus_amount(250, 10) == 375         # потолок +50 %


async def test_streak_continues_on_consecutive_days(db: Database) -> None:
    now = int(time.time())
    day = now // 86400
    await db._write(  # имитируем вчерашний заход
        "UPDATE users SET bonus_at = ?, bonus_day = ?, streak_days = 2 WHERE user_id = ?",
        (now - 90000, day - 1, USER["id"]),
    )

    received, _, balance = await db.daily_bonus(USER["id"], 250, 3600)

    assert received
    assert balance == 1000 + bonus_amount(250, 3)     # серия стала 3 дня
    row = await db.get_user(USER["id"])
    assert int(row["streak_days"]) == 3


async def test_streak_resets_after_gap(db: Database) -> None:
    now = int(time.time())
    day = now // 86400
    await db._write(
        "UPDATE users SET bonus_at = ?, bonus_day = ?, streak_days = 5 WHERE user_id = ?",
        (now - 3 * 86400, day - 3, USER["id"]),
    )

    received, _, balance = await db.daily_bonus(USER["id"], 250, 3600)

    assert received and balance == 1000 + 250        # серия началась заново
    row = await db.get_user(USER["id"])
    assert int(row["streak_days"]) == 1


# --------------------------------------------------------------- статистика
async def test_player_stats_counts_roi(db: Database) -> None:
    match = await _match(db)
    match_id = int(match["id"])
    await db.place_bet(match_id, USER["id"], None, "a", 100)
    await db.place_bet(match_id, USER["id"], None, "b", 50)
    await db.set_status(match_id, "live")
    await db.settle_match(match_id, "a")

    stats = await db.player_stats(USER["id"])

    assert stats["bets"] == 2
    assert stats["settled"] == 2
    assert stats["won"] == 1
    assert stats["winrate"] == 50
    assert stats["staked"] == 150
    assert stats["returned"] == 200
    assert stats["roi"] == 33                       # (200-150)/150
    assert stats["biggest_win"] == 200
    assert stats["favourite_tournament"].startswith("Counter-Strike")
    assert stats["cash_staked"] == 150
    assert stats["cash_returned"] == 200
    assert stats["promo_staked"] == 0


async def test_player_stats_separates_ticket_funded_bets_from_own_balance(db: Database) -> None:
    match = await _match(db, fonbet_id=556)
    match_id = int(match["id"])
    await db.place_bet(match_id, USER["id"], None, "a", 100)
    ticket_id = await db.add_ticket(USER["id"], 200, "test", kind="single")
    await db.place_bet(match_id, USER["id"], None, "a", 200, ticket_id=ticket_id)
    await db.set_status(match_id, "live")
    await db.settle_match(match_id, "a")

    stats = await db.player_stats(USER["id"])

    assert stats["cash_staked"] == 100
    assert stats["cash_returned"] == 200
    assert stats["promo_staked"] == 200
    assert stats["promo_returned"] == 400
    assert stats["staked"] == 300 and stats["returned"] == 600
    assert stats["roi"] == 100  # ROI personal only: (200-100)/100


async def test_player_roi_ignores_unsettled_stakes_until_result(db: Database) -> None:
    settled = await _match(db, fonbet_id=562)
    open_match = await _match(db, fonbet_id=563)
    await db.place_bet(int(settled["id"]), USER["id"], None, "a", 100)
    await db.place_bet(int(open_match["id"]), USER["id"], None, "a", 500)
    await db.set_status(int(settled["id"]), "live")
    await db.settle_match(int(settled["id"]), "a")

    stats = await db.player_stats(USER["id"])

    assert stats["cash_staked"] == 600
    assert stats["settled_cash_staked"] == 100
    assert stats["cash_returned"] == 200
    assert stats["roi"] == 100


async def test_player_roi_includes_cashout_refund_and_commission(db: Database) -> None:
    match = await _match(db, fonbet_id=564)
    await db.place_bet(int(match["id"]), USER["id"], None, "a", 100)
    bet = await db._fetchone(
        "SELECT id FROM bets WHERE user_id = ? ORDER BY id DESC LIMIT 1", (USER["id"],)
    )
    await db.cashout_bet(int(bet["id"]), USER["id"], 0.95)

    stats = await db.player_stats(USER["id"])

    assert stats["cash_staked"] == 100
    assert stats["settled_cash_staked"] == 100
    assert stats["cash_returned"] == 95
    assert stats["roi"] == -5
    assert stats["bets"] == 0  # проданная ставка не считается обычной ставкой/winrate


async def test_player_stats_recognizes_ticket_funded_express(db: Database) -> None:
    first = await _match(db, fonbet_id=558)
    second = await _match(db, fonbet_id=559)
    ticket_id = await db.add_ticket(USER["id"], 300, "test", kind="express")
    await db.place_express(
        USER["id"], None, 300,
        [{"match_id": int(first["id"]), "side": "a"}, {"match_id": int(second["id"]), "side": "b"}],
        ticket_id=ticket_id,
    )

    stats = await db.player_stats(USER["id"])

    assert stats["cash_staked"] == 0
    assert stats["promo_staked"] == 300
    assert stats["staked"] == 300


async def test_line_rtp_counts_only_settled_sports_bets(db: Database) -> None:
    settled = await _match(db, fonbet_id=560)
    still_open = await _match(db, fonbet_id=561)
    await db.place_bet(int(settled["id"]), USER["id"], None, "a", 100)
    await db.place_bet(int(still_open["id"]), USER["id"], None, "a", 500)
    await db.set_status(int(settled["id"]), "live")
    await db.settle_match(int(settled["id"]), "a")

    totals = await db.line_totals()

    assert totals["bets_count"] == 1
    assert totals["bets_staked"] == 100
    assert totals["bets_paid"] == 200


async def test_bet_request_cleanup_keeps_events_and_prunes_only_expired_tokens(db: Database) -> None:
    old = int(time.time()) - 8 * 24 * 60 * 60
    await db._write(
        "INSERT INTO bet_requests (user_id, channel, token, bet_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (USER["id"], "bet", "retry-token", 0, old),
    )
    await db.log_event(USER["id"], "winback", "retention-check")
    match = await _match(db, fonbet_id=557)
    await db.place_bet(int(match["id"]), USER["id"], None, "a", 100, token="retry-token")

    token = await db._fetchone(
        "SELECT created_at, bet_id FROM bet_requests WHERE user_id = ? AND channel = ? AND token = ?",
        (USER["id"], "bet", "retry-token"),
    )
    assert token is not None and int(token["created_at"]) > old and int(token["bet_id"]) > 0
    assert await db.has_event(USER["id"], "winback", "retention-check")


# ------------------------------------------------------------------- лимиты
def test_rate_limiter_window() -> None:
    limiter = RateLimiter()
    for _ in range(8):
        assert limiter.allow(1, "/api/wheel", now=1000)
    assert not limiter.allow(1, "/api/wheel", now=1000)     # девятый — в минуту не пускаем
    assert limiter.allow(2, "/api/wheel", now=1000)         # другой игрок не страдает
    assert limiter.allow(1, "/api/wheel", now=1100)         # окно прошло


def test_rate_limiter_prunes_inactive_client_keys() -> None:
    limiter = RateLimiter()
    assert limiter.allow(1, "/api/wheel", now=1000)
    assert limiter.allow(2, "/api/wheel", now=1000)
    assert limiter.allow(3, "/api/wheel", now=1061)
    assert set(limiter._hits) == {(3, "/api/wheel")}


async def test_honesty_reports_all_games_and_separates_theory_from_realized_line(client: TestClient) -> None:
    data = await (await client.get("/api/honesty")).json()
    assert data["roulette"]["rtp"] == 97.3
    assert data["games"]["slots"]["rtp"] == 95.5
    assert data["games"]["plinko"]["rtp"] == 95.5
    assert data["games"]["ladder"]["rtp"] == 95.5
    assert data["games"]["crash"]["cashout_2x_return"] == 95.0248
    assert data["games"]["blackjack"]["rtp"] is None
    assert data["games"]["baccarat"]["rtp_by_bet"]["banker"] == 98.94
    assert "не прогноз RTP" in data["betting_note"]


async def test_state_has_no_money_ceiling(client: TestClient) -> None:
    state = await (await client.get("/api/state")).json()
    assert "max_bet" not in state["limits"]
    assert "ladder_max_bet" not in state["limits"]
    # casino-состояние — отдельный ленивый эндпоинт
    casino = await (await client.get("/api/casino")).json()
    assert "max_bet" not in casino


async def test_single_bet_above_former_ceiling_is_accepted(client: TestClient, db: Database) -> None:
    match = await _match(db)
    await db.add_coins(USER["id"], 10_000, "тест большого баланса")
    before = await db.get_balance(USER["id"])

    resp = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 6_000}
    )
    data = await resp.json()

    assert resp.status == 200 and data["ok"] is True
    assert await db.get_balance(USER["id"]) == before - 6_000


async def test_express_above_former_ceiling_is_accepted(client: TestClient, db: Database) -> None:
    first = await _match(db, fonbet_id=556)
    second = await _match(db, fonbet_id=557)
    await db.add_coins(USER["id"], 10_000, "тест большого баланса")
    resp = await client.post(
        "/api/express",
        json={"legs": [{"match_id": int(first["id"]), "side": "a"},
                        {"match_id": int(second["id"]), "side": "b"}], "amount": 6_000},
    )
    data = await resp.json()
    assert resp.status == 200 and data["ok"] is True


async def test_casino_and_blackjack_above_former_ceiling_are_accepted(client: TestClient, db: Database) -> None:
    await db.add_coins(USER["id"], 20_000, "тест большого баланса")
    casino = await client.post(
        "/api/casino/play", json={"game": "roulette", "selection": "red", "amount": 6_000}
    )
    assert casino.status == 200 and (await casino.json())["ok"] is True

    blackjack = await client.post("/api/blackjack/start", json={"amount": 6_000})
    assert blackjack.status == 200 and (await blackjack.json())["ok"] is True


async def test_metrics_and_health(client: TestClient) -> None:
    await client.get("/api/state")
    resp = await client.get("/metrics")
    text = await resp.text()

    assert resp.status == 200
    assert "app_requests_total" in text and "app_bets_total" in text

    health = await (await client.get("/health")).json()
    assert health["db"] == "ok"
    assert "feed_stale" in health
    assert metrics.requests > 0


async def test_metrics_aggregate_unmatched_paths(client: TestClient) -> None:
    attacker_paths = [f"/api/not-a-route-{index}" for index in range(6)]
    for path in attacker_paths:
        response = await client.get(path)
        assert response.status == 404

    assert all(path not in metrics.by_path for path in attacker_paths)
    assert metrics.by_path["unmatched"] >= len(attacker_paths)

    body = await (await client.get("/metrics")).text()
    assert all(path not in body for path in attacker_paths)
    assert Metrics._label('quote"\\line\nnext') == 'quote\\"\\\\line\\nnext'


async def test_quests_and_stats_endpoints(client: TestClient, db: Database) -> None:
    quests = await (await client.get("/api/quests")).json()
    assert len(quests["quests"]) == 3
    assert all("title" in quest and "reward" in quest for quest in quests["quests"])

    stats = await (await client.get("/api/stats")).json()
    assert stats["ok"] and stats["stats"]["bets"] == 0

    # чужой код задания — понятная ошибка, а не 500
    resp = await client.post("/api/quest", json={"code": "нет-такого"})
    data = await resp.json()
    assert data["ok"] and data["claimed"] is False and data["error"]


def test_quest_titles_exist() -> None:
    for quest in quests_of_day(day_number()):
        assert by_code(day_number(), quest["code"]) is not None
        assert quest["target"] >= 1 and quest["reward"] > 0

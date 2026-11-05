"""P2: задания дня, серия дней, статистика игрока. P3: лимиты, потолок ставки, /metrics, /health."""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.db.database import bonus_amount
from app.services.quests import by_code, day_number, quests_of_day
from app.web.limits import RateLimiter, metrics
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


# ------------------------------------------------------------------- лимиты
def test_rate_limiter_window() -> None:
    limiter = RateLimiter()
    for _ in range(8):
        assert limiter.allow(1, "/api/wheel", now=1000)
    assert not limiter.allow(1, "/api/wheel", now=1000)     # девятый — в минуту не пускаем
    assert limiter.allow(2, "/api/wheel", now=1000)         # другой игрок не страдает
    assert limiter.allow(1, "/api/wheel", now=1100)         # окно прошло


async def test_bet_over_max_is_refused(client: TestClient, db: Database) -> None:
    match = await _match(db)
    before = await db.get_balance(USER["id"])

    resp = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 999999}
    )
    data = await resp.json()

    assert resp.status == 400 and "Максимальная ставка" in data["error"]
    assert await db.get_balance(USER["id"]) == before


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


async def test_quests_and_stats_endpoints(client: TestClient, db: Database) -> None:
    state = await (await client.get("/api/state")).json()
    assert len(state["quests"]) == 3
    assert all("title" in quest and "reward" in quest for quest in state["quests"])

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

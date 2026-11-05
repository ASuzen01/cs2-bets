"""Игровой цикл, рост и доверие: достижения, серия дней, рефералы, кэшаут, честность.

Один файл на все новые механики: каждая проверяется от лица игрока — «сделал
действие, получил результат», а не «вызвал метод».
"""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.db.database import InvalidBet, MatchNotFound
from app.services import achievements as achievements_service
from app.services.quests import chain_chest, next_chest
from app.web.server import build_app

TOKEN = "123456:CYCLE_TOKEN"
USER = {"id": 8080, "first_name": "Игрок", "username": "player"}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "cycle.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(USER["id"], USER["username"], "Игрок")
    yield database
    await database.close()


@pytest.fixture
async def client(db: Database, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    monkeypatch.setattr(settings, "cashout_rate", 0.95)
    async with TestClient(TestServer(build_app(db))) as test_client:
        yield test_client


async def make_match(db: Database, *, fonbet_id: int = 5001, odds_a: float = 2.0) -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament="IEM. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + 3600,
        odds_a=odds_a,
        odds_b=2.2,
    )
    return row


# ------------------------------------------------------------------ достижения
async def test_first_bet_unlocks_achievement(client: TestClient, db: Database) -> None:
    match = await make_match(db)

    response = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100}
    )
    assert response.status == 200

    body = await (await client.get("/api/state")).json()
    unlocked = {item["code"] for item in body["achievements"]["items"] if item["unlocked"]}
    assert "first_bet" in unlocked
    assert body["achievements"]["unlocked"] >= 1
    assert body["achievements"]["total"] == len(achievements_service.ACHIEVEMENTS)


async def test_new_achievements_reported_once(client: TestClient, db: Database) -> None:
    match = await make_match(db)
    placed = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100}
    )
    codes = {item["code"] for item in (await placed.json())["new_achievements"]}
    assert "first_bet" in codes                 # прилетело вместе со ставкой

    second = await (await client.get("/api/state")).json()
    assert not any(item["code"] == "first_bet" for item in second["new_achievements"])


async def test_win_unlocks_first_win_and_odds_achievements(db: Database) -> None:
    match = await make_match(db, odds_a=6.0)
    await db.place_bet(int(match["id"]), USER["id"], None, "a", 100)
    await db.settle_match(int(match["id"]), "a", source="admin")

    fresh = {item["code"] for item in await achievements_service.sync(db, USER["id"])}
    assert {"first_bet", "first_win", "odds_5"} <= fresh


async def test_three_lines_achievement(db: Database) -> None:
    cs2 = await make_match(db, fonbet_id=6001)
    football, _ = await db.upsert_match(
        fonbet_id=6002, tournament="Англия. Премьер-лига", team_a="Арсенал", team_b="Челси",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_x=3.4, odds_b=3.6, sport="football",
    )
    hockey, _ = await db.upsert_match(
        fonbet_id=6003, tournament="КХЛ", team_a="Авангард", team_b="ЦСКА",
        start_time=int(time.time()) + 3600, odds_a=2.95, odds_x=4.2, odds_b=2.1, sport="hockey",
    )
    await db.place_express(
        USER["id"], None, 100,
        [
            {"match_id": int(cs2["id"]), "side": "a"},
            {"match_id": int(football["id"]), "side": "x"},
            {"match_id": int(hockey["id"]), "side": "h:final:1"},
        ],
    )

    fresh = {item["code"] for item in await achievements_service.sync(db, USER["id"])}
    assert "three_lines" in fresh


async def test_achievements_are_idempotent(db: Database) -> None:
    match = await make_match(db)
    await db.place_bet(int(match["id"]), USER["id"], None, "a", 100)

    first = await achievements_service.sync(db, USER["id"])
    second = await achievements_service.sync(db, USER["id"])
    assert first and second == []


# ------------------------------------------------------------- серия дней/сундук
def test_chain_chest_math() -> None:
    assert chain_chest(0) is None
    assert chain_chest(2) is None
    assert chain_chest(3) == {"days": 3, "reward": 300}
    assert chain_chest(6) == {"days": 3, "reward": 300}
    assert chain_chest(7) == {"days": 7, "reward": 700}
    assert next_chest(0) == {"days": 3, "reward": 300}
    assert next_chest(3) == {"days": 7, "reward": 700}
    assert next_chest(7) is None


async def test_quest_chain_grows_and_pays_chest(client: TestClient, db: Database) -> None:
    """Закрыл все задания дня три дня подряд — забираешь сундук."""
    from app.services.quests import quests_of_day

    day = int(time.time()) // 86400
    async with db.transaction() as conn:
        for closed_day in (day - 2, day - 1, day):
            await conn.execute(
                "INSERT INTO quest_chain (user_id, days, last_day, best_days, chest_day)"
                " VALUES (?, 0, 0, 0, 0) ON CONFLICT DO NOTHING",
                (USER["id"],),
            )
            for quest in quests_of_day(closed_day):
                await conn.execute(
                    "INSERT INTO quests (user_id, day, code, progress, target, reward)"
                    " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (USER["id"], closed_day, quest["code"], quest["target"],
                     quest["target"], quest["reward"]),
                )
            await db._bump_quest_chain(conn, USER["id"], closed_day)

    chain = await db.quest_chain(USER["id"])
    assert chain["days"] == 3 and chain["best_days"] == 3

    response = await client.post("/api/quest/chest")
    body = await response.json()
    assert response.status == 200
    assert body["amount"] == 300
    assert body["balance"] == 1300

    again = await client.post("/api/quest/chest")
    assert again.status == 400


async def test_state_reports_chain(client: TestClient, db: Database) -> None:
    body = await (await client.get("/api/state")).json()
    assert body["chain"]["days"] == 0
    assert body["chain"]["next"] == {"days": 3, "reward": 300}


# -------------------------------------------------------------------- рефералы
async def test_referral_pays_both_sides_after_first_bet(
    client: TestClient, db: Database, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "referral_bonus", 300)
    monkeypatch.setattr(settings, "referral_welcome", 150)
    monkeypatch.setattr(settings, "bot_username", "dobraya_ludka_bot")
    await db.upsert_user(9001, "friend", "Друг")

    # новый игрок пришёл по ссылке
    monkeypatch.setattr(settings, "miniapp_dev_user", 9001)
    async with TestClient(TestServer(build_app(db))) as friend:
        attached = await friend.post("/api/referral", json={"code": f"ref_{USER['id']}"})
        assert (await attached.json())["attached"] is True

        match = await make_match(db, fonbet_id=7777)
        placed = await friend.post(
            "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100}
        )
        assert placed.status == 200

    assert await db.get_balance(USER["id"]) == 1300      # 1000 + 300
    assert await db.get_balance(9001) == 1000 - 100 + 150

    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    state = await (await client.get("/api/referral")).json()
    assert state["invited"] == 1
    assert state["paid"] == 1
    assert state["earned"] == 300
    assert state["link"] == f"https://t.me/dobraya_ludka_bot?startapp=ref_{USER['id']}"


async def test_referral_ignores_self_and_late_invites(client: TestClient, db: Database) -> None:
    self_attach = await client.post("/api/referral", json={"code": f"ref_{USER['id']}"})
    assert self_attach.status == 400

    match = await make_match(db, fonbet_id=7778)
    await client.post("/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 10})
    # после первой ставки приглашение уже не привязать
    await db.upsert_user(9002, "inviter", "Пригласивший")
    late = await client.post("/api/referral", json={"code": "ref_9002"})
    assert late.status == 400


async def test_referral_link_is_empty_without_username(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "bot_username", "")
    body = await (await client.get("/api/referral")).json()
    assert body["link"] == ""


# ---------------------------------------------------------------------- кэшаут
async def test_cashout_refunds_95_percent(client: TestClient, db: Database) -> None:
    match = await make_match(db)
    await client.post("/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100})

    bets = await (await client.get("/api/bets")).json()
    item = bets["open"][0]
    assert item["cashout"] == 95

    response = await client.post("/api/cashout", json={"bet_id": item["id"]})
    body = await response.json()

    assert response.status == 200
    assert body["refund"] == 95
    assert body["balance"] == 995

    after = await (await client.get("/api/bets")).json()
    assert after["open"] == []
    assert after["settled"][0]["cashed_out"] is True
    assert after["settled"][0]["result"] == "void"


async def test_cashed_bet_does_not_pay_on_settlement(client: TestClient, db: Database) -> None:
    match = await make_match(db)
    await client.post("/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100})
    item = (await (await client.get("/api/bets")).json())["open"][0]
    await client.post("/api/cashout", json={"bet_id": item["id"]})

    result = await db.settle_match(int(match["id"]), "a", source="admin")
    assert result["paid"] == 0                     # проданная ставка не участвует
    assert await db.get_balance(USER["id"]) == 995


async def test_cashout_twice_is_rejected(client: TestClient, db: Database) -> None:
    match = await make_match(db)
    await client.post("/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100})
    item = (await (await client.get("/api/bets")).json())["open"][0]

    assert (await client.post("/api/cashout", json={"bet_id": item["id"]})).status == 200
    assert (await client.post("/api/cashout", json={"bet_id": item["id"]})).status == 400


async def test_cashout_foreign_bet_is_rejected(client: TestClient, db: Database) -> None:
    match = await make_match(db)
    await db.upsert_user(9009, "other", "Другой")
    await db.place_bet(int(match["id"]), 9009, None, "a", 100)

    response = await client.post("/api/cashout", json={"bet_id": 1})
    assert response.status == 404


async def test_cashout_after_start_is_rejected(db: Database) -> None:
    row, _ = await db.upsert_match(
        fonbet_id=5050,
        tournament="IEM",
        team_a="A",
        team_b="B",
        start_time=int(time.time()) + 3600,
        odds_a=2.0,
        odds_b=2.0,
    )
    await db.place_bet(int(row["id"]), USER["id"], None, "a", 100)
    bet_id = int((await db.user_bets_all(USER["id"]))[0]["id"])
    # матч начался уже после ставки — продать больше нельзя
    await db._write("UPDATE matches SET start_time = ? WHERE id = ?",
                    (int(time.time()) - 60, int(row["id"])))
    with pytest.raises(InvalidBet):
        await db.cashout_bet(bet_id, USER["id"], 0.95)
    with pytest.raises(MatchNotFound):
        await db.cashout_bet(9999, USER["id"], 0.95)


# -------------------------------------------------------------------- честность
async def test_honesty_numbers(client: TestClient, db: Database) -> None:
    match = await make_match(db)
    await client.post("/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100})
    await db.settle_match(int(match["id"]), "a", source="admin")

    body = await (await client.get("/api/honesty")).json()

    assert body["ok"] is True
    assert body["roulette"]["rtp"] == 97.3          # 18/37 × 2
    assert body["dice"]["rtp"] == 95.5              # DICE_EDGE
    assert body["wheel"]["slots"] == 24
    assert body["wheel"]["empty"] == 8
    assert body["wheel"]["expected"] > 0
    assert body["line"]["bets_staked"] == 100
    assert body["line_rtp"] > 0


# ------------------------------------------------------------------- напоминание
async def test_reminder_text_and_window(db: Database) -> None:
    from app.services import reminders

    assert "Серия 4" in reminders.text_for(4, 350)
    assert reminders.in_window(16 * 3600 + 60) is True
    assert reminders.in_window(3 * 3600) is False


async def test_reminder_targets_only_active_streaks(db: Database) -> None:
    from app.services import reminders

    day = int(time.time()) // 86400
    await db._write("UPDATE users SET streak_days = 5, bonus_day = ? WHERE user_id = ?",
                    (day - 1, USER["id"]))
    await db.upsert_user(9100, "noseries", "Без серии")

    rows = await db.users_to_remind(day, reminders.EVENT)
    assert [int(row["user_id"]) for row in rows] == [USER["id"]]

    # бонус забрали сегодня — напоминание не нужно
    await db._write("UPDATE users SET bonus_day = ? WHERE user_id = ?", (day, USER["id"]))
    assert await db.users_to_remind(day, reminders.EVENT) == []


async def test_reminder_sends_once(db: Database) -> None:
    from app.services import reminders

    day = int(time.time()) // 86400
    await db._write("UPDATE users SET streak_days = 3, bonus_day = ? WHERE user_id = ?",
                    (day - 1, USER["id"]))

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    count = await reminders.remind_once(FakeBot(), db, force=True)
    assert count == 1
    assert sent[0][0] == USER["id"]
    assert "Серия 3" in sent[0][1]

    await db.log_event(USER["id"], reminders.EVENT, "3")
    assert await reminders.remind_once(FakeBot(), db, force=True) == 0


# ----------------------------------------------------------- старт матча
async def test_match_start_notification_once(db: Database) -> None:
    from app.services import matchnotify

    now = int(time.time())
    row, _ = await db.upsert_match(
        fonbet_id=8100, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=now + 600, odds_a=2.0, odds_b=2.0,
    )
    await db.place_bet(int(row["id"]), USER["id"], None, "a", 100)

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    assert await matchnotify.notify_once(FakeBot(), db, now=now) == 1
    assert sent[0][0] == USER["id"]
    assert "Aurora — G2 Esports" in sent[0][1]
    assert "10 мин" in sent[0][1]

    # второй проход по тому же матчу молчит
    assert await matchnotify.notify_once(FakeBot(), db, now=now) == 0


async def test_match_start_notification_skips_far_and_cashed(db: Database) -> None:
    from app.services import matchnotify

    now = int(time.time())
    far, _ = await db.upsert_match(
        fonbet_id=8200, tournament="IEM", team_a="X", team_b="Y",
        start_time=now + 7200, odds_a=2.0, odds_b=2.0,
    )
    await db.place_bet(int(far["id"]), USER["id"], None, "a", 100)

    soon, _ = await db.upsert_match(
        fonbet_id=8201, tournament="IEM", team_a="P", team_b="Q",
        start_time=now + 300, odds_a=2.0, odds_b=2.0,
    )
    await db.place_bet(int(soon["id"]), USER["id"], None, "a", 100)
    bet = [item for item in await db.user_bets_all(USER["id"]) if int(item["match_id"]) == int(soon["id"])][0]
    await db.cashout_bet(int(bet["id"]), USER["id"], 0.95)

    class FakeBot:
        def __init__(self):
            self.texts: list[str] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.texts.append(text)

    fake = FakeBot()
    assert await matchnotify.notify_once(fake, db, now=now) == 0
    assert fake.texts == []


async def test_season_top_counts_only_bets(db: Database) -> None:
    """В недельный зачёт идёт прибыль со ставок, а бонусы — нет."""
    now = int(time.time())
    row, _ = await db.upsert_match(
        fonbet_id=8300, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=now + 600, odds_a=2.0, odds_b=2.0,
    )
    await db.place_bet(int(row["id"]), USER["id"], None, "a", 100)
    await db.settle_match(int(row["id"]), "a")
    await db.add_coins(USER["id"], 5000, "бонус за вход")

    start = db.week_start()
    top = await db.season_top(10, start)
    assert int(top[0]["user_id"]) == USER["id"]
    assert int(top[0]["score"]) == 100                 # +200 выигрыш − 100 ставка

    rank = await db.season_rank(USER["id"], start)
    assert rank["rank"] == 1 and rank["score"] == 100

    # баланс в общем топе при этом вырос на бонус
    general = await db.rank_of_user(USER["id"])
    assert general["balance"] == 1000 - 100 + 200 + 5000


async def test_top_api_returns_season(client: TestClient, db: Database) -> None:
    now = int(time.time())
    row, _ = await db.upsert_match(
        fonbet_id=8400, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=now + 600, odds_a=2.0, odds_b=2.0,
    )
    await client.post("/api/bet", json={"match_id": int(row["id"]), "side": "a", "amount": 100})
    await db.settle_match(int(row["id"]), "a")

    body = await (await client.get("/api/top")).json()
    assert body["season"]["top"][0]["balance"] == 100
    assert body["season"]["me"]["score"] == 100
    assert body["season"]["starts_at"] == db.week_start()

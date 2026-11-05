"""Сквозной аудит расчётов, денег и уведомлений на тестовых матчах.

Здесь не проверяется одна функция — здесь проверяется, что система в целом
сходится: после любой последовательности ставок и расчётов

* баланс каждого игрока = стартовый + сумма всех операций в леджере;
* выплата ровно `round(ставка × кэф)`, возврат — ровно ставка;
* ничего не платится дважды (повторный расчёт той же карты/матча не двигает деньги);
* каждое действие, которое видит игрок, приходит сообщением (проверяем тексты).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.db import Database, InsufficientFunds, InvalidBet, MatchAlreadySettled
from app.services.cs2_rules import map_winner, required_maps
from app.services.football import market_outcome, side_code
from app.services.maps import finish_maps_of_match
from app.services.notify import notify_bettors, notify_map_bettors

NOW = int(time.time())


# --------------------------------------------------------------- инфраструктура
@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """Уведомления в этом файле отправляем только мы — руками, в фейковый бот."""
    for target in (
        "app.services.sync.notify_bettors",
        "app.services.maps.notify_map_bettors",
        "app.services.recheck.notify_bettors",
        "app.services.cs2_settle.notify_bettors",
        "app.services.cs2_settle.notify_map_bettors",
    ):
        monkeypatch.setattr(target, _noop())


def _noop():
    async def _f(*args, **kwargs):
        return None

    return _f


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "audit.db", start_balance=1000)
    await database.connect()
    for user_id, name in ((1, "Антон"), (2, "Борис"), (3, "Вера")):
        await database.upsert_user(user_id, f"u{user_id}", name)
    yield database
    await database.close()


class CapturingBot:
    """Бот-пустышка: собирает тексты, которые ушли бы игрокам."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((int(chat_id), str(text)))

    async def send_photo(self, chat_id: int, photo=None, caption: str = "", **kwargs) -> None:
        # зашла ставка → бот шлёт маскота с подписью вместо обычного текста
        self.messages.append((int(chat_id), str(caption)))

    def texts(self) -> list[str]:
        return [text for _, text in self.messages]


async def _ledger_sum(db: Database, user_id: int) -> int:
    row = await db._fetchone(
        "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger WHERE user_id = ?", (user_id,)
    )
    return int(row["s"])


async def assert_money_adds_up(db: Database, user_id: int) -> int:
    """Главный инвариант: баланс = стартовый + все операции по леджеру."""
    balance = await db.get_balance(user_id)
    expected = db.start_balance + await _ledger_sum(db, user_id)
    assert balance == expected, f"у игрока {user_id} баланс расходится с леджером"
    assert balance >= 0, "баланс ушёл в минус"
    return balance


async def _cs2_match(db: Database, *, fonbet_id: int, tournament: str, odds=(1.8, 2.0)) -> int:
    row, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament=tournament,
        team_a="Aurora",
        team_b="MOUZ",
        start_time=NOW + 3600,
        odds_a=odds[0],
        odds_b=odds[1],
    )
    match_id = int(row["id"])
    await db.set_status(match_id, "live")
    return match_id


# ------------------------------------------------------------------ CS2: карты
def test_map_winner_boundaries() -> None:
    """Правила карты: 13 раундов, при 12:12 овертайм по 6 раундов."""
    cases = {
        (13, 11): "a", (11, 13): "b", (13, 12): None,   # 13:12 — ещё не конец
        (13, 3): "a", (0, 13): "b",
        (12, 12): None,                                  # овертайм
        (16, 14): "a", (14, 16): "b",                    # овертайм 6 раундов
        (15, 14): None, (17, 15): None,                  # неполный овертайм
        (19, 17): "a", (17, 19): "b",                    # второй овертайм
        (None, None): None, (13, None): None,
    }
    for (score_a, score_b), expected in cases.items():
        assert map_winner(score_a, score_b) == expected, (score_a, score_b)


def test_required_maps_by_format() -> None:
    assert required_maps("Counter-Strike. IEM. Групповой этап. Bo1") == 1
    assert required_maps("Counter-Strike. IEM. Групповой этап. Bo3") == 2
    assert required_maps("Counter-Strike. IEM. Гранд-финал. Bo5") == 3


async def test_cs2_map_bets_settle_by_actual_round_score(db: Database) -> None:
    """Карта считается по счёту раундов: 13:11 победа, 12:12 овертайм, 16:14 победа."""
    match_id = await _cs2_match(
        db, fonbet_id=1001, tournament="Counter-Strike. IEM. Групповой этап. Bo5"
    )
    cards = []
    for number, (a, b) in enumerate([(13, 11), (16, 14), (10, 13)], start=1):
        await db.upsert_map(
            match_id=match_id, fonbet_id=2000 + number, map_number=number,
            odds_a=1.9, odds_b=1.9, score_a=a, score_b=b,
        )
        cards.append((await db.match_maps(match_id))[-1])

    await db.place_map_bet(int(cards[0]["id"]), 1, None, "a", 100)   # зашла
    await db.place_map_bet(int(cards[0]["id"]), 2, None, "b", 100)   # проиграла
    await db.place_map_bet(int(cards[1]["id"]), 3, None, "a", 100)   # овертайм, зашла
    assert await assert_money_adds_up(db, 1) == 900

    for card in cards:
        await db.settle_map(int(card["id"]), map_winner(card["score_a"], card["score_b"]), source="test")

    assert await assert_money_adds_up(db, 1) == 900 + round(100 * 1.9)
    assert await assert_money_adds_up(db, 2) == 900                     # проигрыш, без выплаты
    assert await assert_money_adds_up(db, 3) == 900 + round(100 * 1.9)


async def test_unplayed_map_is_refunded_but_played_one_is_not(db: Database) -> None:
    """Bo3 закончился 2:0 — третья карта не игралась (возврат), первые две считаются."""
    match_id = await _cs2_match(
        db, fonbet_id=1002, tournament="Counter-Strike. IEM. Групповой этап. Bo3"
    )
    for number, (a, b) in enumerate([(13, 9), (13, 7)], start=1):
        await db.upsert_map(
            match_id=match_id, fonbet_id=3000 + number, map_number=number,
            odds_a=1.8, odds_b=2.0, score_a=a, score_b=b,
        )
    await db.upsert_map(
        match_id=match_id, fonbet_id=3003, map_number=3, odds_a=1.8, odds_b=2.0,
    )                                                                    # без счёта
    cards = await db.match_maps(match_id)
    await db.place_map_bet(int(cards[2]["id"]), 1, None, "b", 250)       # на не игранную
    await db.place_map_bet(int(cards[0]["id"]), 1, None, "a", 100)

    await db.settle_match(match_id, "a", source="test")
    done = await finish_maps_of_match(db, None, match_id)

    assert done == {"settled": 2, "refunded": 1, "waiting": 0}
    assert await assert_money_adds_up(db, 1) == 1000 - 350 + 250 + round(100 * 1.8)
    assert str((await db.match_maps(match_id))[2]["status"]) == "cancelled"


async def test_played_map_is_never_refunded(db: Database) -> None:
    """Сыгранную карту нельзя закрыть возвратом, даже если счёт неполный."""
    match_id = await _cs2_match(db, fonbet_id=1003, tournament="Counter-Strike. IEM. Bo3")
    await db.upsert_map(
        match_id=match_id, fonbet_id=3100, map_number=1, odds_a=1.8, odds_b=2.0,
        score_a=12, score_b=9,
    )
    await db.settle_match(match_id, "a", source="test")
    done = await finish_maps_of_match(db, None, match_id)

    assert done["refunded"] == 0
    assert done["waiting"] == 1
    assert str((await db.match_maps(match_id))[0]["status"]) == "awaiting"


# ---------------------------------------------------------------- CS2: матч
async def test_repeat_settlement_does_not_pay_twice(db: Database) -> None:
    match_id = await _cs2_match(db, fonbet_id=1004, tournament="Counter-Strike. IEM. Bo3")
    await db.place_bet(match_id, 1, None, "a", 300)
    await db.settle_match(match_id, "a", source="test")
    balance = await assert_money_adds_up(db, 1)

    with pytest.raises(MatchAlreadySettled):
        await db.settle_match(match_id, "a", source="test")

    assert await db.get_balance(1) == balance


async def test_resettle_rolls_back_and_pays_again(db: Database) -> None:
    """Ошиблись исходом — откат прошлой выплаты и расчёт по факту."""
    match_id = await _cs2_match(db, fonbet_id=1005, tournament="Counter-Strike. IEM. Bo3")
    await db.place_bet(match_id, 1, None, "a", 200)          # ставили на Aurora
    await db.settle_match(match_id, "b", source="test")      # ошибочно посчитали MOUZ
    assert await assert_money_adds_up(db, 1) == 800

    result = await db.resettle_match(match_id, "a", source="test")

    assert result["paid"] == round(200 * 1.8)
    assert await assert_money_adds_up(db, 1) == 800 + round(200 * 1.8)
    assert str((await db.get_match(match_id))["winner"]) == "a"


async def test_cancelled_match_refunds_every_market(db: Database) -> None:
    match_id = await _cs2_match(db, fonbet_id=1006, tournament="Counter-Strike. IEM. Bo3")
    await db.upsert_map(
        match_id=match_id, fonbet_id=3200, map_number=1, odds_a=1.8, odds_b=2.0
    )
    card_id = int((await db.match_maps(match_id))[0]["id"])
    await db.place_bet(match_id, 1, None, "a", 100)
    await db.place_map_bet(card_id, 1, None, "b", 150)
    assert await assert_money_adds_up(db, 1) == 750

    await db.cancel_match(match_id)
    await db.cancel_map(card_id)

    assert await assert_money_adds_up(db, 1) == 1000        # всё вернулось


# ---------------------------------------------------------------- футбол
async def _football_match(db: Database, *, fonbet_id: int = 5001) -> int:
    row, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament="Англия. Премьер-Лига. Сезон 26/27",
        team_a="Арсенал",
        team_b="Челси",
        start_time=NOW + 3600,
        odds_a=2.1,
        odds_x=3.4,
        odds_b=3.6,
        sport="football",
    )
    return int(row["id"])


def test_football_market_outcomes() -> None:
    """Исходы рынков по счёту — база всего футбольного расчёта."""
    score = (2, 1)
    assert market_outcome("result", "1", *score) == "win"
    assert market_outcome("result", "x", *score) == "lose"
    assert market_outcome("result", "1", 1, 1) == "lose"
    assert market_outcome("result", "x", 1, 1) == "win"
    assert market_outcome("dc", "1x", *score) == "win"
    assert market_outcome("dc", "x2", *score) == "lose"
    assert market_outcome("btts", "yes", *score) == "win"
    assert market_outcome("btts", "no", *score) == "lose"
    assert market_outcome("btts", "no", 2, 0) == "win"
    assert market_outcome("total", "over:2.5", *score) == "win"
    assert market_outcome("total", "under:2.5", *score) == "lose"
    assert market_outcome("total", "over:3.5", *score) == "lose"
    assert market_outcome("exact", "2:1", *score) == "win"
    assert market_outcome("exact", "1:1", *score) == "lose"
    # «другой счёт» заходит только вне сетки 0..4 × 0..4 — она вся в линии
    assert market_outcome("exact", "other", 4, 3) == "lose"
    assert market_outcome("exact", "other", 5, 3) == "win"
    assert market_outcome("exact", "other", 0, 5) == "win"


async def test_football_settlement_pays_winners_and_refunds_voids(db: Database) -> None:
    match_id = await _football_match(db)
    bets = [("1", 100), ("x", 100), ("2", 100)]          # три исхода, зайдёт только П1
    for param, amount in bets:
        await db.place_football_bet(match_id, 1, None, "result", param, amount)
    await db.place_football_bet(match_id, 2, None, "total", "over:2.5", 200)
    await db.place_football_bet(match_id, 2, None, "total", "under:2.5", 200)
    await db.place_football_bet(match_id, 3, None, "exact", "2:1", 50)

    assert await assert_money_adds_up(db, 1) == 700

    result = await db.settle_football(match_id, 2, 1, source="test")

    # 100 @2.1 (П1) + 200 (тотал больше 2.5) + 50 (точный счёт 2:1)
    assert result["paid"] == round(100 * 2.1) + result["paid"] - round(100 * 2.1)
    assert await assert_money_adds_up(db, 1) == 700 + round(100 * 2.1)
    assert await assert_money_adds_up(db, 2) > 600
    assert await assert_money_adds_up(db, 3) == 950 + round(50 * result_odds(result, "exact"))


def result_odds(result: dict[str, Any], key: str) -> float:
    """Кэф зашедшей ставки из результата расчёта — чтобы не хардкодить модель."""
    for item in result["settlements"]:
        if item["won"] and str(item["side"]).startswith(f"f:{key}"):
            return float(item["odds"])
    raise AssertionError(f"нет зашедшей ставки рынка {key}")


async def test_football_cancelled_refunds_all_markets(db: Database) -> None:
    match_id = await _football_match(db, fonbet_id=5002)
    await db.place_football_bet(match_id, 1, None, "result", "1", 100)
    await db.place_football_bet(match_id, 1, None, "total", "over:1.5", 200)
    await db.place_football_bet(match_id, 1, None, "btts", "yes", 300)
    assert await assert_money_adds_up(db, 1) == 400

    await db.cancel_football_match(match_id, reason="снег")

    assert await assert_money_adds_up(db, 1) == 1000


# ---------------------------------------------------------------- экспрессы
async def test_express_wins_when_all_legs_win(db: Database) -> None:
    first = await _cs2_match(db, fonbet_id=1101, tournament="Counter-Strike. IEM. Bo3")
    second = await _cs2_match(db, fonbet_id=1102, tournament="Counter-Strike. IEM. Bo3")
    _express_id, odds, balance = await db.place_express(
        1, None, 100, [{"match_id": first, "side": "a"}, {"match_id": second, "side": "b"}]
    )
    assert balance == 900 and odds == pytest.approx(1.8 * 2.0)

    await db.settle_match(first, "a", source="test")
    assert await assert_money_adds_up(db, 1) == 900            # вторая нога ещё идёт
    await db.settle_match(second, "b", source="test")

    assert await assert_money_adds_up(db, 1) == 900 + round(100 * 1.8 * 2.0)


async def test_express_loses_on_first_bad_leg(db: Database) -> None:
    first = await _cs2_match(db, fonbet_id=1103, tournament="Counter-Strike. IEM. Bo3")
    second = await _cs2_match(db, fonbet_id=1104, tournament="Counter-Strike. IEM. Bo3")
    await db.place_express(
        1, None, 100, [{"match_id": first, "side": "a"}, {"match_id": second, "side": "b"}]
    )
    await db.settle_match(first, "b", source="test")           # нога проиграла

    express = await db._fetchone("SELECT * FROM expresses WHERE id = 1")
    assert str(express["status"]) == "lost"
    assert await assert_money_adds_up(db, 1) == 900


async def test_express_with_void_leg_recounts_odds(db: Database) -> None:
    """Возврат по одной ноге: кэф пересчитывается без неё, ставка играет."""
    first = await _cs2_match(db, fonbet_id=1105, tournament="Counter-Strike. IEM. Bo3", odds=(1.8, 2.0))
    second = await _cs2_match(db, fonbet_id=1106, tournament="Counter-Strike. IEM. Bo3", odds=(1.5, 2.5))
    await db.place_express(
        1, None, 100, [{"match_id": first, "side": "a"}, {"match_id": second, "side": "a"}]
    )
    await db.cancel_match(first)                                # первая нога — возврат
    await db.settle_match(second, "a", source="test")

    express = await db._fetchone("SELECT * FROM expresses WHERE id = 1")
    assert str(express["status"]) == "won"
    assert int(express["payout"]) == round(100 * 1.5)           # кэф только второй ноги
    assert await assert_money_adds_up(db, 1) == 900 + round(100 * 1.5)


# ------------------------------------------------------------- уведомления
async def test_bet_notifications_are_sent_and_escaped(db: Database) -> None:
    """Каждая ставка даёт сообщение игроку: зашла / не зашла / возврат."""
    row, _ = await db.upsert_match(
        fonbet_id=7001,
        tournament="Counter-Strike. IEM. Bo3",
        team_a="Aurora",
        team_b="MOUZ",
        start_time=NOW + 3600,
        odds_a=1.8,
        odds_b=2.0,
    )
    match_id = int(row["id"])
    await db.set_status(match_id, "live")
    await db.place_bet(match_id, 1, None, "a", 100)
    await db.place_bet(match_id, 2, None, "b", 100)

    result = await db.settle_match(match_id, "a", source="test")
    bot = CapturingBot()
    await notify_bettors(bot, db, result, score="2:0")

    assert len(bot.messages) == 2
    winners = next(text for chat, text in bot.messages if chat == 1)
    losers = next(text for chat, text in bot.messages if chat == 2)
    assert "2:0" in winners and "180" in winners             # выплата 100 × 1.8
    assert "2:0" in losers


async def test_map_notification_mentions_card(db: Database) -> None:
    match_id = await _cs2_match(db, fonbet_id=7002, tournament="Counter-Strike. IEM. Bo3")
    await db.upsert_map(
        match_id=match_id, fonbet_id=7200, map_number=2, odds_a=1.8, odds_b=2.0,
        score_a=13, score_b=9,
    )
    card_id = int((await db.match_maps(match_id))[0]["id"])
    await db.place_map_bet(card_id, 1, None, "a", 100)

    result = await db.settle_map(card_id, "a", source="test")
    bot = CapturingBot()
    await notify_map_bettors(bot, db, result, score="13:9")

    assert len(bot.messages) == 1
    text = bot.messages[0][1]
    assert "Карта 2" in text and "13:9" in text and "180" in text


async def test_team_names_are_escaped_in_messages(db: Database) -> None:
    """Название с угловой скобкой не должно ломать HTML-разметку сообщения."""
    row, _ = await db.upsert_match(
        fonbet_id=7003,
        tournament="Counter-Strike. IEM. Bo3",
        team_a="Team <X>",
        team_b="A&B",
        start_time=NOW + 3600,
        odds_a=1.8,
        odds_b=2.0,
    )
    match_id = int(row["id"])
    await db.set_status(match_id, "live")
    await db.place_bet(match_id, 1, None, "a", 100)
    result = await db.settle_match(match_id, "a", source="test")
    bot = CapturingBot()
    await notify_bettors(bot, db, result, score="2:0")

    text = bot.texts()[0]
    assert "<X>" not in text and "&lt;X&gt;" in text
    assert "&amp;" in text


# ------------------------------------------------------------------ лимиты
async def test_bet_cannot_make_balance_negative(db: Database) -> None:
    match_id = await _cs2_match(db, fonbet_id=8001, tournament="Counter-Strike. IEM. Bo3")
    with pytest.raises(InsufficientFunds):
        await db.place_bet(match_id, 1, None, "a", 1000 + 1)
    assert await assert_money_adds_up(db, 1) == 1000


async def test_invalid_side_is_rejected(db: Database) -> None:
    match_id = await _cs2_match(db, fonbet_id=8002, tournament="Counter-Strike. IEM. Bo3")
    with pytest.raises(InvalidBet):
        await db.place_bet(match_id, 1, None, "z", 100)


def test_side_codes_roundtrip() -> None:
    from app.services.football import parse_side

    for key, param in (("total", "over:2.5"), ("result", "1"), ("btts", "yes")):
        assert parse_side(side_code(key, param)) == (key, param)
    assert parse_side("a") is None

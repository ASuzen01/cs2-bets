"""Экспрессы на футбол и хоккей: сборка, деньги и расчёт.

Сценарий игрока: берём исход футбольного матча и рынок хоккейного, ставим
экспресс, ждём расчёта — и проверяем, что ноги считаются честно: футбол по
итоговому счёту, хоккей по тому счёту, который обещает рынок (основное время
или с овертаймом).
"""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.db.database import InvalidBet
from app.services.football import market_label as football_label
from app.services.legs import label, odds, outcome
from app.web.server import build_app

TOKEN = "123456:EXPRESS_SPORTS_TOKEN"
USER = {"id": 5150, "first_name": "Игрок", "username": "player"}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "express.db", start_balance=10_000)
    await database.connect()
    await database.upsert_user(USER["id"], USER["username"], "Игрок")
    yield database
    await database.close()


@pytest.fixture
async def client(db: Database, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    async with TestClient(TestServer(build_app(db))) as test_client:
        yield test_client


async def football_match(db: Database, *, odds_a=2.0, odds_x=3.4, odds_b=3.6) -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=1001,
        tournament="Англия. Премьер-лига",
        team_a="Арсенал",
        team_b="Челси",
        start_time=int(time.time()) + 3600,
        odds_a=odds_a,
        odds_x=odds_x,
        odds_b=odds_b,
        sport="football",
    )
    return row


async def hockey_match(db: Database, *, fonbet_id=2001, odds_a=2.95, odds_x=4.2, odds_b=2.1) -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament="Фонбет КХЛ. Регулярный сезон",
        team_a="Авангард",
        team_b="ЦСКА",
        start_time=int(time.time()) + 3600,
        odds_a=odds_a,
        odds_x=odds_x,
        odds_b=odds_b,
        sport="hockey",
    )
    return row


# ------------------------------------------------------------- кэфы ног (сервис)
async def test_leg_odds_for_football_and_hockey(db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)

    assert odds(fb, "a") == 2.0
    assert odds(fb, "x") == 3.4
    assert odds(fb, "b") == 3.6
    # ничьей в хоккее на уровне матча нет: X бывает только в основном времени
    assert odds(hk, "x") is None
    assert odds(hk, "a") == 2.95
    # рынки хоккея считаются моделью — кэф должен быть настоящим числом
    assert odds(hk, "h:reg:x") == 4.2
    assert odds(hk, "h:final:1") and odds(hk, "h:final:1") > 1
    assert odds(hk, "h:total:over:5.5") and odds(hk, "h:total:over:5.5") > 1
    assert odds(hk, "h:total:over:99.5") is None   # такого рынка в росписи нет


async def test_leg_labels_are_human(db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)

    assert label(fb, "a") == "Арсенал"
    assert label(fb, "x") == "Ничья"
    # ноги подписаны командой: иначе две ноги из разных матчей не различить
    assert label(hk, "h:reg:1") == "Авангард (осн. время)"
    assert label(hk, "h:reg:x") == "Ничья (осн. время)"
    assert label(hk, "h:final:2") == "ЦСКА (с ОТ)"
    assert label(hk, "h:total:over:5.5") == "ТБ 5.5 (основное время)"
    assert label(fb, "f:total:over:2.5") == football_label("total", "over:2.5")


# ------------------------------------------------------------------ ставка HTTP
async def test_express_football_plus_hockey_via_api(client: TestClient, db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)

    response = await client.post(
        "/api/express",
        json={
            "amount": 100,
            "legs": [
                {"match_id": int(fb["id"]), "side": "a"},
                {"match_id": int(hk["id"]), "side": "h:reg:x"},
            ],
        },
    )
    payload = await response.json()

    assert response.status == 200, payload
    assert payload["ok"] is True
    assert payload["odds"] == pytest.approx(2.0 * 4.2, rel=1e-3)
    assert payload["balance"] == 9900
    assert [leg["team"] for leg in payload["legs"]] == ["Арсенал", "Ничья (осн. время)"]
    assert [leg["side"] for leg in payload["legs"]] == ["a", "h:reg:x"]


async def test_express_with_football_draw_is_allowed(client: TestClient, db: Database) -> None:
    """Ничья — полноценный исход футбола, в экспрессе она тоже допустима."""
    fb = await football_match(db)
    hk = await hockey_match(db)

    response = await client.post(
        "/api/express",
        json={
            "amount": 50,
            "legs": [
                {"match_id": int(fb["id"]), "side": "x"},
                {"match_id": int(hk["id"]), "side": "h:final:1"},
            ],
        },
    )
    assert response.status == 200
    body = await response.json()
    assert body["odds"] > 3.4        # ничья умножается на победу хозяев с ОТ


async def test_express_with_hockey_total(client: TestClient, db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)

    response = await client.post(
        "/api/express",
        json={
            "amount": 100,
            "legs": [
                {"match_id": int(fb["id"]), "side": "b"},
                {"match_id": int(hk["id"]), "side": "h:total:over:5.5"},
            ],
        },
    )
    assert response.status == 200


async def test_draw_leg_on_cs2_is_rejected(client: TestClient, db: Database) -> None:
    cs2, _ = await db.upsert_match(
        fonbet_id=3001,
        tournament="IEM. Bo3",
        team_a="Aurora",
        team_b="G2",
        start_time=int(time.time()) + 3600,
        odds_a=1.65,
        odds_b=2.25,
    )
    hk = await hockey_match(db)

    response = await client.post(
        "/api/express",
        json={
            "amount": 100,
            "legs": [
                {"match_id": int(cs2["id"]), "side": "x"},
                {"match_id": int(hk["id"]), "side": "h:reg:1"},
            ],
        },
    )
    assert response.status == 400
    assert "кэфа" in (await response.json())["error"]


async def test_unknown_hockey_market_is_rejected(client: TestClient, db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)

    response = await client.post(
        "/api/express",
        json={
            "amount": 100,
            "legs": [
                {"match_id": int(fb["id"]), "side": "a"},
                {"match_id": int(hk["id"]), "side": "h:whatever:1"},
            ],
        },
    )
    assert response.status == 400


async def test_two_legs_of_one_match_are_rejected(db: Database) -> None:
    fb = await football_match(db)

    with pytest.raises(InvalidBet, match="два исхода одного матча"):
        await db.place_express(USER["id"], None, 100, [
            {"match_id": int(fb["id"]), "side": "a"},
            {"match_id": int(fb["id"]), "side": "x"},
        ])


# --------------------------------------------------------------------- расчёт
async def test_express_football_win_and_hockey_win_pays(db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)
    express_id, total_odds, _ = await db.place_express(
        USER["id"],
        None,
        100,
        [
            {"match_id": int(fb["id"]), "side": "a"},        # Арсенал
            {"match_id": int(hk["id"]), "side": "h:final:1"},  # Авангард с ОТ
        ],
    )

    await db.settle_football(int(fb["id"]), 2, 0, source="admin")
    assert (await db.get_express(express_id))["status"] == "open"   # одна нога ещё в игре

    await db.settle_hockey(int(hk["id"]), 2, 2, 3, 2, source="admin")
    express = await db.get_express(express_id)
    assert express["status"] == "won"
    assert int(express["payout"]) == round(100 * total_odds)
    assert await db.get_balance(USER["id"]) == 10_000 - 100 + round(100 * total_odds)


async def test_hockey_reg_leg_loses_when_draw_in_regulation(db: Database) -> None:
    """Экспресс с «X основного времени» заходит, даже если команда выиграла в овертайме."""
    fb = await football_match(db)
    hk = await hockey_match(db)
    express_id, _, _ = await db.place_express(
        USER["id"],
        None,
        100,
        [
            {"match_id": int(fb["id"]), "side": "b"},         # Челси
            {"match_id": int(hk["id"]), "side": "h:reg:x"},   # ничья в основное время
        ],
    )

    await db.settle_football(int(fb["id"]), 0, 1, source="admin")   # Челси выиграл
    await db.settle_hockey(int(hk["id"]), 2, 2, 3, 2, source="admin")

    legs = {str(leg["side"]): leg for leg in await db.express_items(express_id)}
    assert str(legs["b"]["result"]) == "won"
    assert str(legs["h:reg:x"]["result"]) == "won"
    assert (await db.get_express(express_id))["status"] == "won"


async def test_hockey_final_leg_loses_when_team_loses_in_overtime(db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)
    express_id, _, _ = await db.place_express(
        USER["id"],
        None,
        100,
        [
            {"match_id": int(fb["id"]), "side": "a"},
            {"match_id": int(hk["id"]), "side": "h:final:2"},   # ЦСКА с ОТ
        ],
    )

    await db.settle_football(int(fb["id"]), 3, 0, source="admin")
    # 2:2 в основное время, 3:2 — Авангард дожал в овертайме, ставка на ЦСКА мимо
    await db.settle_hockey(int(hk["id"]), 2, 2, 3, 2, source="admin")

    legs = {str(leg["side"]): leg for leg in await db.express_items(express_id)}
    assert str(legs["h:final:2"]["result"]) == "lost"
    assert (await db.get_express(express_id))["status"] == "lost"
    assert int((await db.get_express(express_id))["payout"] or 0) == 0


async def test_express_football_draw_leg_wins_on_draw(db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)
    express_id, total_odds, _ = await db.place_express(
        USER["id"],
        None,
        200,
        [
            {"match_id": int(fb["id"]), "side": "x"},
            {"match_id": int(hk["id"]), "side": "h:reg:1"},
        ],
    )

    await db.settle_football(int(fb["id"]), 1, 1, source="admin")
    await db.settle_hockey(int(hk["id"]), 4, 1, 4, 1, source="admin")

    express = await db.get_express(express_id)
    assert express["status"] == "won"
    assert int(express["payout"]) == round(200 * total_odds)


async def test_cancelled_hockey_match_voids_the_leg(db: Database) -> None:
    fb = await football_match(db)
    hk = await hockey_match(db)
    express_id, _, _ = await db.place_express(
        USER["id"],
        None,
        100,
        [
            {"match_id": int(fb["id"]), "side": "a"},
            {"match_id": int(hk["id"]), "side": "h:final:1"},
        ],
    )

    await db.settle_football(int(fb["id"]), 2, 0, source="admin")
    await db.cancel_match(int(hk["id"]))

    express = await db.get_express(express_id)
    assert express["status"] == "won"
    # отменённая нога идёт с кэфом 1.0: платим только за футбол
    assert int(express["payout"]) == round(100 * 2.0)


# --------------------------------------------------------------------- сервис
def test_market_side_detection() -> None:
    from app.services.legs import is_market_side

    assert is_market_side("f:total:over:2.5") is True
    assert is_market_side("h:final:1") is True
    assert is_market_side("a") is False
    assert is_market_side("") is False


async def test_outcome_helper_reads_scores(db: Database) -> None:
    fb = await football_match(db)
    await db.settle_football(int(fb["id"]), 2, 1, source="admin")
    row = await db.get_match(int(fb["id"]))

    assert outcome(row, "a", "a") == "won"
    assert outcome(row, "b", "a") == "lost"
    assert outcome(row, "x", "x") == "won"            # исход передан вызывающим
    assert outcome(row, "f:total:over:2.5", "a") == "won"
    assert outcome(row, "f:total:over:3.5", "a") == "lost"
    assert outcome(row, "a", None) == "void"


async def test_legs_of_lost_express_are_marked_after_late_settlement(db: Database) -> None:
    """Экспресс уже проиграл, а второй матч рассчитали позже — нога не должна висеть."""
    fb = await football_match(db)
    hk = await hockey_match(db)
    express_id, _, _ = await db.place_express(
        USER["id"],
        None,
        100,
        [
            {"match_id": int(fb["id"]), "side": "a"},
            {"match_id": int(hk["id"]), "side": "h:final:1"},
        ],
    )

    await db.settle_football(int(fb["id"]), 0, 2, source="admin")   # Арсенал проиграл
    assert (await db.get_express(express_id))["status"] == "lost"

    await db.settle_hockey(int(hk["id"]), 1, 1, 2, 1, source="admin")
    legs = {str(leg["side"]): str(leg["result"]) for leg in await db.express_items(express_id)}
    assert legs == {"a": "lost", "h:final:1": "won"}
    assert (await db.get_express(express_id))["status"] == "lost"


async def test_history_shows_legs_of_football_hockey_express(client: TestClient, db: Database) -> None:
    """Пользовательский сценарий: собрал экспресс → дождался расчёта → увидел историю."""
    fb = await football_match(db)
    hk = await hockey_match(db)

    placed = await client.post(
        "/api/express",
        json={
            "amount": 100,
            "legs": [
                {"match_id": int(fb["id"]), "side": "x"},
                {"match_id": int(hk["id"]), "side": "h:final:1"},
            ],
        },
    )
    body = await placed.json()
    assert placed.status == 200

    await db.settle_football(int(fb["id"]), 2, 2, source="admin")
    await db.settle_hockey(int(hk["id"]), 2, 2, 3, 2, source="admin")

    history = await (await client.get("/api/bets")).json()
    express = next(item for item in history["settled"] if item["kind"] == "express")
    assert express["id"] == body["express_id"]
    assert express["result"] == "won"
    assert express["payout"] == body["potential"]
    assert [leg["team"] for leg in express["legs"]] == ["Ничья", "Авангард (с ОТ)"]
    assert all(leg["result"] == "won" for leg in express["legs"])

"""Купон ставки картинкой: рендер PNG и раздача через мини-апп."""

from __future__ import annotations

import io
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from app.config import settings
from app.db import Database
from app.services.coupon import render_coupon
from app.web.auth import BOT_KEY
from app.web.server import build_app

TOKEN = "123456:COUPON_TEST_TOKEN"
USER = {"id": 4242, "first_name": "Антон", "username": "sap"}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(blob: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(blob)) as image:
        return image.size


# ------------------------------------------------------------------ одиночная
def _single(result: str = "won") -> dict:
    return {
        "kind": "single",
        "id": 11,
        "status": "settled" if result != "open" else "open",
        "result": result,
        "title": "Aurora — G2 Esports",
        "subtitle": "Ставка на Aurora",
        "tournament": "Counter-Strike. IEM Пекин",
        "odds": 1.65,
        "stake": 100,
        "payout": 165 if result == "won" else 0,
        "potential": 165,
        "at": int(time.time()),
        "legs": [],
    }


# ------------------------------------------------------------------- экспресс
def _express(result: str = "won", legs: int = 3) -> dict:
    odds = 1.65 * 2.10 * 1.80
    return {
        "kind": "express",
        "id": 22,
        "status": "settled" if result != "open" else "open",
        "result": result,
        "title": f"Экспресс · {legs} исхода",
        "subtitle": " + ".join(["Aurora"] * legs),
        "stake": 250,
        "odds": round(odds, 2),
        "payout": round(250 * odds) if result == "won" else 0,
        "potential": round(250 * odds),
        "at": int(time.time()),
        "legs": [
            {
                "team": team,
                "odds": odd,
                "result": "void" if result == "void" else ("won" if result == "won" else "lost"),
            }
            for team, odd in zip(
                ["Aurora", "G2 Esports", "Vitality", "Spirit", "FaZe"][:legs],
                [1.65, 2.10, 1.80, 1.95, 2.40][:legs],
                strict=False,
            )
        ],
    }


@pytest.mark.parametrize("result", ["won", "lost", "void"])
def test_single_coupon_renders_png(result: str) -> None:
    blob = render_coupon(_single(result))
    assert blob.startswith(PNG_MAGIC)
    assert png_size(blob) == (1080, 1350)


def test_open_coupon_renders_png() -> None:
    blob = render_coupon(_single("open"))
    assert blob.startswith(PNG_MAGIC)
    assert png_size(blob) == (1080, 1350)


@pytest.mark.parametrize("result", ["won", "lost", "open"])
def test_express_coupon_renders_png(result: str) -> None:
    blob = render_coupon(_express(result))
    assert blob.startswith(PNG_MAGIC)
    assert png_size(blob) == (1080, 1350)


def test_express_coupon_survives_many_legs() -> None:
    """Пять исходов в купоне: лишние строки не должны ронять рендер."""
    blob = render_coupon(_express("won", legs=5))
    assert blob.startswith(PNG_MAGIC)
    assert png_size(blob) == (1080, 1350)


def test_coupon_survives_empty_item() -> None:
    """Пустой словарь — это не 500: рисуем «пустой» купон, а не падаем."""
    blob = render_coupon({})
    assert blob.startswith(PNG_MAGIC)


def test_long_title_wraps_not_overflows() -> None:
    item = _single("won")
    item["title"] = "Natus Vincere — Team Falcons · Гранд-финал BLAST Premier World Final"
    blob = render_coupon(item)
    assert png_size(blob) == (1080, 1350)


# ------------------------------------------------------------------------ HTTP
@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "coupon.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(USER["id"], USER["username"], "Антон")
    await database.upsert_user(999, "other", "Другой")
    yield database
    await database.close()


@pytest.fixture
async def match(db: Database):
    row, _ = await db.upsert_match(
        fonbet_id=777,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + 3600,
        odds_a=1.65,
        odds_b=2.25,
    )
    return row


def _make_client(db: Database, bot=None) -> TestClient:
    """Клиент мини-аппа; бота кладём в приложение до старта — так честнее."""
    app = build_app(db)
    if bot is not None:
        app[BOT_KEY] = bot
    return TestClient(TestServer(app))


@pytest.fixture
async def client(db: Database, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    async with _make_client(db) as test_client:
        yield test_client


async def _place(client: TestClient, match_id: int, amount: int = 100) -> int:
    response = await client.post(
        "/api/bet", json={"match_id": match_id, "side": "a", "amount": amount}
    )
    assert response.status == 200
    body = await (await client.get("/api/bets")).json()
    return int(body["open"][0]["id"])


async def test_coupon_image_by_bet_id(client: TestClient, match) -> None:
    bet_id = await _place(client, int(match["id"]))

    response = await client.get(f"/api/coupon/bet/{bet_id}.png")
    body = await response.read()

    assert response.status == 200
    assert response.content_type == "image/png"
    assert body.startswith(PNG_MAGIC)
    assert png_size(body) == (1080, 1350)


async def test_coupon_image_of_settled_bet(client: TestClient, db: Database, match) -> None:
    bet_id = await _place(client, int(match["id"]))
    await db.settle_match(int(match["id"]), "a", source="manual")

    response = await client.get(f"/api/coupon/bet/{bet_id}.png")
    assert response.status == 200
    assert (await response.read()).startswith(PNG_MAGIC)


async def test_coupon_of_foreign_bet_is_404(client: TestClient, db: Database, match) -> None:
    """Чужую ставку не отдаём даже картинкой."""
    await db.place_bet(int(match["id"]), 999, 999, "a", 100)

    own = await (await client.get("/api/bets")).json()
    assert own["open"] == []

    response = await client.get("/api/coupon/bet/1.png")
    assert response.status == 404
    assert (await response.json())["ok"] is False


async def test_coupon_unknown_kind_is_404(client: TestClient, match) -> None:
    response = await client.get("/api/coupon/stuff/1.png")
    assert response.status == 404


async def test_coupon_share_without_bot_is_503(client: TestClient, match) -> None:
    """В мини-аппе без бота честно говорим «чата нет», а не молчим."""
    bet_id = await _place(client, int(match["id"]))

    response = await client.post("/api/share", json={"kind": "bet", "id": bet_id})
    payload = await response.json()

    assert response.status == 503
    assert payload["ok"] is False
    assert "Чат" in payload["error"]


async def test_coupon_share_sends_photo(db: Database, monkeypatch, match) -> None:
    sent: list[dict] = []

    class FakeBot:
        async def send_photo(self, chat_id, photo, caption=""):
            sent.append({"chat_id": chat_id, "caption": caption, "png": photo.data})

    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    async with _make_client(db, FakeBot()) as client:
        bet_id = await _place(client, int(match["id"]))
        response = await client.post("/api/share", json={"kind": "bet", "id": bet_id})
        payload = await response.json()

    assert response.status == 200
    assert payload["ok"] is True and payload["sent"] is True
    assert len(sent) == 1
    assert sent[0]["chat_id"] == USER["id"]
    assert sent[0]["png"].startswith(PNG_MAGIC)


async def test_coupon_share_telegram_failure_is_502(db: Database, monkeypatch, match) -> None:
    class BrokenBot:
        async def send_photo(self, **kwargs):
            raise RuntimeError("bot was blocked by the user")

    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    async with _make_client(db, BrokenBot()) as client:
        bet_id = await _place(client, int(match["id"]))
        response = await client.post("/api/share", json={"kind": "bet", "id": bet_id})
        payload = await response.json()

    assert response.status == 502
    assert payload["ok"] is False


async def test_coupon_share_foreign_bet_is_404(client: TestClient, db: Database, match) -> None:
    await db.place_bet(int(match["id"]), 999, 999, "a", 100)

    response = await client.post("/api/share", json={"kind": "bet", "id": 1})
    assert response.status == 404

"""Сборка веб-части: /health и /ping должны отвечать без базы и без сети."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from bot import build_web_app


def _client() -> TestClient:
    """Приложение без базы, синка и перепроверки: только маршруты и /health."""
    bot = Bot(token="42:TEST-TOKEN")
    dp = Dispatcher()
    return TestClient(TestServer(build_web_app(bot, dp, db=None, syncer=None, recheck=None)))


async def test_ping_answers_without_database() -> None:
    app_client = _client()
    await app_client.start_server()
    try:
        response = await app_client.get("/ping")
        payload = await response.json()
        assert response.status == 200
        assert payload["ok"] is True
        assert isinstance(payload["at"], int)
    finally:
        await app_client.close()


async def test_health_answers_without_database() -> None:
    app_client = _client()
    await app_client.start_server()
    try:
        response = await app_client.get("/health")
        payload = await response.json()
        assert response.status == 200
        assert payload["ok"] is True
        assert payload["mode"] == "webhook"
        # ключи есть всегда, даже когда сервисы не переданы
        assert "recheck_loop" in payload
        assert "keepalive" in payload
        assert "recheck" in payload
    finally:
        await app_client.close()


def test_render_index_versions_static_assets(monkeypatch) -> None:
    from app.web.server import render_index

    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")
    html = render_index({"user": None, "dev": False})
    assert '/static/app.css?v=abcdef123456' in html
    assert '/static/app.js?v=abcdef123456' in html

"""Keep-alive: сервис на бесплатном Render не должен засыпать."""

from __future__ import annotations

import asyncio

from app.services.keepalive import KeepAlive, resolve_url


def test_url_prefers_explicit_then_webhook_then_render(monkeypatch) -> None:
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://from-render.onrender.com")
    assert resolve_url("https://explicit.example", "https://hook.example") == "https://explicit.example"
    assert resolve_url("", "https://hook.example") == "https://hook.example"
    assert resolve_url("", "") == "https://from-render.onrender.com"


def test_url_ignores_garbage(monkeypatch) -> None:
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert resolve_url("", "") == ""
    assert resolve_url("localhost:8080", "") == ""


def test_bare_host_gets_https(monkeypatch) -> None:
    """render.yaml отдаёт WEBHOOK_BASE_URL как голый хост — тоже принимаем."""
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert resolve_url("", "bot.onrender.com") == "https://bot.onrender.com"


def test_ping_url_points_to_ping() -> None:
    keep = KeepAlive("https://bot.onrender.com/")
    assert keep.url == "https://bot.onrender.com/ping"


async def test_ping_counts_success(monkeypatch) -> None:
    keep = KeepAlive("https://bot.onrender.com")
    calls: list[str] = []

    async def _fetch(url: str) -> None:
        calls.append(url)

    monkeypatch.setattr(keep, "_fetch", _fetch)

    assert await keep.ping_once() is True
    assert calls == ["https://bot.onrender.com/ping"]
    assert keep.pings == 1 and keep.fails == 0
    assert keep.stats["last_ok_at"] is not None
    assert keep.stats["last_error"] is None


async def test_ping_records_failure(monkeypatch) -> None:
    keep = KeepAlive("https://bot.onrender.com")

    async def _fetch(url: str) -> None:
        raise TimeoutError("долго")

    monkeypatch.setattr(keep, "_fetch", _fetch)

    assert await keep.ping_once() is False
    assert keep.fails == 1 and keep.last_ok_at == 0
    assert "TimeoutError" in (keep.stats["last_error"] or "")


def test_failed_ping_is_retried_sooner() -> None:
    """Не ответил — пробуем через пару минут, а не через весь интервал."""
    keep = KeepAlive("https://bot.onrender.com", interval=600)
    assert keep.next_delay(True) == 600
    assert keep.next_delay(False) == 120


async def test_ping_without_url_does_nothing() -> None:
    keep = KeepAlive("")
    assert keep.stats["enabled"] is False
    assert await keep.ping_once() is False


async def test_loop_pings_repeatedly(monkeypatch) -> None:
    keep = KeepAlive("https://bot.onrender.com", interval=60, first_delay=0)
    monkeypatch.setattr("app.services.keepalive.settings.keepalive_enabled", True)
    pings: list[str] = []

    async def _fetch(url: str) -> None:
        pings.append(url)

    real_sleep = asyncio.sleep

    async def _fast_sleep(_: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(keep, "_fetch", _fetch)
    monkeypatch.setattr("asyncio.sleep", _fast_sleep)

    keep.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await keep.stop()

    assert pings, "keep-alive должен был пинговать"

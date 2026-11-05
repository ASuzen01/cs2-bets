"""Не даём бесплатному инстансу Render заснуть.

Render выключает бесплатный веб-сервис после 15 минут без входящих запросов —
и мини-приложение, и бот перестают отвечать, пока кто-нибудь не откроет сайт.
Поэтому раз в `KEEPALIVE_INTERVAL` минут мы сами дёргаем свой публичный `/ping`:
для Render это обычный входящий запрос, сервис считается активным и не спит.

Важно: пинг идёт через публичный адрес (`RENDER_EXTERNAL_URL`), а не через
localhost, — только такой запрос Render видит как внешний. Если сервис всё же
успел заснуть (перезапуск, деплой, длинная пауза), разбудить его может только
запрос снаружи: для этого в README описан внешний пингер (cron-job.org или
UptimeRobot) на тот же `/ping`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

import aiohttp

from app.config import settings

log = logging.getLogger(__name__)

PING_PATH = "/ping"
# Первый пинг — не сразу: HTTP-сервер начинает слушать уже после startup-хуков.
FIRST_PING_DELAY_SECONDS = 30


def resolve_url(explicit: str = "", base: str = "") -> str:
    """Публичный адрес для пинга: настройка → база вебхука → адрес Render.

    Понимает и полный адрес (`https://bot.onrender.com`), и голый хост
    (`bot.onrender.com`) — `fromService: property: host` в render.yaml даёт именно хост.
    """
    for candidate in (explicit, base, os.getenv("RENDER_EXTERNAL_URL", "")):
        url = (candidate or "").strip().rstrip("/")
        if not url:
            continue
        if url.startswith(("http://", "https://")):
            return url
        if "." in url and " " not in url and "/" not in url:
            return f"https://{url}"
    return ""


class KeepAlive:
    """Раз в N секунд дёргает собственный /ping, чтобы сервис не засыпал."""

    def __init__(
        self,
        url: str,
        *,
        interval: int = 600,
        timeout: int = 15,
        first_delay: int = FIRST_PING_DELAY_SECONDS,
    ) -> None:
        self.url = f"{url.rstrip('/')}{PING_PATH}" if url else ""
        self.interval = max(60, int(interval))
        self.timeout = timeout
        self.first_delay = first_delay
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self.pings = 0
        self.fails = 0
        self.last_at: int = 0
        self.last_ok_at: int = 0
        self.last_error: str | None = None

    @classmethod
    def from_settings(cls) -> KeepAlive:
        url = resolve_url(settings.keepalive_url, settings.webhook_base_url)
        instance = cls(url, interval=settings.keepalive_interval)
        if not instance.enabled:
            log.info("Keep-alive выключен: нет публичного адреса сервиса")
        return instance

    @property
    def enabled(self) -> bool:
        return bool(self.url) and settings.keepalive_enabled

    @property
    def stats(self) -> dict[str, Any]:
        """Для /health: видно, что сервис действительно будится."""
        return {
            "enabled": self.enabled,
            "url": self.url or None,
            "interval_seconds": self.interval,
            "pings": self.pings,
            "fails": self.fails,
            "last_at": self.last_at or None,
            "last_ok_at": self.last_ok_at or None,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.enabled:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="keepalive")
            log.info("Keep-alive запущен: %s каждые %s сек", self.url, self.interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _run(self) -> None:
        if self.first_delay:
            await asyncio.sleep(self.first_delay)
        while True:
            await self.ping_once()
            await asyncio.sleep(self.interval)

    # ----------------------------------------------------------------- пинг
    async def ping_once(self) -> bool:
        """Один запрос к своему /ping. False — сервис не ответил."""
        if not self.url:
            return False
        self.pings += 1
        self.last_at = int(time.time())
        try:
            await self._fetch(self.url)
        except Exception as exc:                      # сеть, таймаут, 5xx
            self.fails += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("Keep-alive: %s не ответил (%s)", self.url, self.last_error)
            return False
        self.last_ok_at = self.last_at
        self.last_error = None
        log.debug("Keep-alive: %s ок", self.url)
        return True

    async def _fetch(self, url: str) -> None:
        """Отдельный метод, чтобы тесты не ходили в сеть."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        async with self._session.get(url) as response:
            if response.status >= 500:
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=response.status
                )
            await response.read()


KEEPALIVE = KeepAlive.from_settings()

__all__ = ["KEEPALIVE", "PING_PATH", "KeepAlive", "resolve_url"]

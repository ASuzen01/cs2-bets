"""Периодические Telegram-оповещения о проблемах production-сервиса."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from app.config import settings
from app.web.limits import metrics

log = logging.getLogger(__name__)


class HealthAlertMonitor:
    """Отслеживает БД, устаревшую линию и всплески HTTP 429 без спама админу."""

    def __init__(
        self,
        bot: Any,
        db: Any,
        syncer: Any,
        *,
        interval_seconds: int = 60,
        rate_limit_threshold: int = 10,
        rate_limit_cooldown: int = 1800,
    ) -> None:
        self.bot = bot
        self.db = db
        self.syncer = syncer
        self.interval_seconds = max(10, int(interval_seconds))
        self.rate_limit_threshold = max(1, int(rate_limit_threshold))
        self.rate_limit_cooldown = max(60, int(rate_limit_cooldown))
        self.started_at = int(time.time())
        self.last_checked_at: int | None = None
        self.active: set[str] = set()
        self.last_rate_alert_at = 0
        self._last_rejected = metrics.rejected
        self._task: asyncio.Task[None] | None = None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "last_checked_at": self.last_checked_at,
            "active": sorted(self.active),
            "rate_limited_total": metrics.rejected,
            "rate_alert_threshold_per_interval": self.rate_limit_threshold,
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="health-alert-monitor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Проверка состояния production-сервиса завершилась ошибкой")
            await asyncio.sleep(self.interval_seconds)

    async def _notify(self, text: str) -> None:
        admins = settings.admin_ids_list
        if not admins:
            log.error("Оповещение админам не отправлено: ADMIN_IDS не настроен: %s", text)
            return
        for admin_id in admins:
            try:
                await self.bot.send_message(admin_id, text[:3500])
            except Exception:
                log.exception("Не удалось отправить health-оповещение админу %s", admin_id)

    async def _condition(self, key: str, problem: str | None) -> None:
        was_active = key in self.active
        if problem and not was_active:
            self.active.add(key)
            await self._notify(f"🚨 CS2 Bets: {problem}")
        elif not problem and was_active:
            self.active.discard(key)
            await self._notify(f"✅ CS2 Bets: проблема устранена ({key})")

    async def check_once(self, *, now: int | None = None) -> None:
        """Один проход мониторинга; доступен отдельно для тестов и диагностики."""
        moment = int(time.time() if now is None else now)
        self.last_checked_at = moment

        try:
            await self.db.get_meta("health_probe")
        except Exception as exc:
            await self._condition(
                "db", f"БД недоступна ({type(exc).__name__}); подробности смотрите в логах сервиса."
            )
        else:
            await self._condition("db", None)

        try:
            raw_sync = await self.db.get_meta("last_sync_at")
            last_sync = int(raw_sync) if raw_sync else int(getattr(self.syncer, "last_sync_at", 0) or 0)
        except Exception:
            last_sync = int(getattr(self.syncer, "last_sync_at", 0) or 0)

        stale_after = max(900, int(settings.sync_interval) * 20)
        if last_sync:
            age = max(0, moment - last_sync)
            stale = age > stale_after
            detail = f"Лента матчей не обновлялась {age // 60} мин. Порог — {stale_after // 60} мин."
        else:
            stale = moment - self.started_at > stale_after
            detail = f"Лента ещё не синхронизировалась за {stale_after // 60} мин. после запуска."
        if stale and getattr(self.syncer, "last_error", None):
            detail += " Синхронизатор сообщил об ошибке; подробности смотрите в логах сервиса."
        await self._condition("feed_stale", detail if stale else None)

        rejected = int(metrics.rejected)
        delta = max(0, rejected - self._last_rejected)
        self._last_rejected = rejected
        if (
            delta >= self.rate_limit_threshold
            and moment - self.last_rate_alert_at >= self.rate_limit_cooldown
        ):
            self.last_rate_alert_at = moment
            await self._notify(
                f"⚠️ CS2 Bets: за последний интервал получено {delta} ответов 429 "
                f"от лимитера мини-приложения (порог {self.rate_limit_threshold}). "
                "Проверьте /metrics и авто-спин."
            )


__all__ = ["HealthAlertMonitor"]

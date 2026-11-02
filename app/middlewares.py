from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject, User

from app.db import Database

log = logging.getLogger(__name__)


class UserSyncMiddleware(BaseMiddleware):
    """Заводит пользователя в БД при первом обращении (и обновляет имя/ник)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and not user.is_bot:
            try:
                await self.db.upsert_user(user.id, user.username, user.full_name)
            except Exception:  # не роняем обработку из-за статистики
                log.exception("Не удалось обновить пользователя %s", user.id)

        # групповые чаты запоминаем: туда бот постит новые матчи тир-1
        if isinstance(event, Message) and event.chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        ):
            try:
                title = event.chat.title or event.chat.username or str(event.chat.id)
                await self.db.register_chat(event.chat.id, title)
            except Exception:
                log.exception("Не удалось сохранить чат %s", event.chat.id)
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Простое анти-спам ограничение: не чаще одной обработки в N секунд."""

    def __init__(self, rate_limit: float = 0.4, ignore_ids: Iterable[int] = ()) -> None:
        self.rate_limit = rate_limit
        self.ignore_ids = set(ignore_ids)
        self._last_seen: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if (
            self.rate_limit <= 0
            or user is None
            or user.id in self.ignore_ids
            or isinstance(event, Message) is False
        ):
            return await handler(event, data)

        now = time.monotonic()
        last = self._last_seen.get(user.id)
        if last is not None and now - last < self.rate_limit:
            log.debug("Троттлинг: пропущено событие от %s", user.id)
            return None

        self._last_seen[user.id] = now
        if len(self._last_seen) > 4096:
            self._prune(now)
        return await handler(event, data)

    def _prune(self, now: float) -> None:
        for user_id, seen in list(self._last_seen.items()):
            if now - seen > 60:
                self._last_seen.pop(user_id, None)

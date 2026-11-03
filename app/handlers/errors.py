from __future__ import annotations

import logging
import re
import time
import traceback
from collections import deque
from typing import Any

from aiogram.types import CallbackQuery, ErrorEvent, Message

from app import texts

log = logging.getLogger(__name__)

# последние ошибки — их отдаёт /health, чтобы разбирать поломки без логов Render
LAST_ERRORS: deque[dict[str, Any]] = deque(maxlen=5)

_SECRET = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}|/webhook/[A-Za-z0-9_-]{8,}")


def _clean(text: str, limit: int = 1200) -> str:
    """Убираем токен бота и секрет вебхука: /health открыт наружу."""
    return _SECRET.sub("***", text)[:limit]


def last_errors(count: int = 1) -> list[dict[str, Any]]:
    return list(LAST_ERRORS)[-count:]


async def on_error(event: ErrorEvent) -> None:
    try:
        trace = "".join(
            traceback.format_exception(type(event.exception), event.exception, event.exception.__traceback__)
        )
    except Exception:  # на всякий случай: ошибка не должна ломать обработчик ошибок
        trace = repr(event.exception)

    info = {
        "at": int(time.time()),
        "update_id": event.update.update_id,
        "kind": type(event.update.event).__name__,
        "error": _clean(f"{type(event.exception).__name__}: {event.exception}", 300),
        "trace": _clean(trace),
    }
    LAST_ERRORS.append(info)
    log.error(
        "Ошибка при обработке обновления %s",
        event.update.update_id,
        exc_info=event.exception,
    )

    source = event.update.event
    try:
        if isinstance(source, Message):
            await source.answer(texts.ERROR_TEXT)
        elif isinstance(source, CallbackQuery):
            await source.answer("Что-то пошло не так 😔", show_alert=False)
    except Exception:  # пользователь мог заблокировать бота
        log.debug("Не удалось сообщить об ошибке пользователю")


__all__ = ["LAST_ERRORS", "last_errors", "on_error"]

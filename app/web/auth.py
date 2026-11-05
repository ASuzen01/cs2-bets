"""Авторизация мини-приложения и общая обработка ошибок API.

Отдельный модуль, чтобы `server.py` занимался только маршрутами и обработчиками.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import web

from app.config import settings
from app.db import BettingError
from app.services.miniapp import AuthError, validate_init_data
from app.web.limits import limiter, metrics

log = logging.getLogger(__name__)

DB_KEY = web.AppKey("db", Any)
BOT_KEY = web.AppKey("bot", Any)
USER_KEY = web.RequestKey("user", dict)
ALERT_KEY = web.AppKey("alert", Any)
_ALERT_AT_KEY = web.AppKey("alert_at", float)


async def resolve_user(request: web.Request) -> dict[str, Any]:
    """Возвращает {user_id, username, full_name, dev} или поднимает AuthError."""
    init_data = (
        request.headers.get("X-Telegram-Init-Data") or request.query.get("init_data") or ""
    )

    if init_data:
        parsed = validate_init_data(init_data, settings.bot_token)
        if not parsed["user_id"]:
            raise AuthError("в initData нет пользователя")
        return {
            "user_id": parsed["user_id"],
            "username": parsed["username"],
            "full_name": parsed["full_name"],
            "dev": False,
        }

    if settings.miniapp_dev_user:
        return {
            "user_id": settings.miniapp_dev_user,
            "username": "dev",
            "full_name": "Локальный игрок",
            "dev": True,
        }

    raise AuthError("Откройте приложение кнопкой в Telegram")


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Защищает всё, что начинается с /api/ — интерфейс открыт, данные нет."""
    if not request.path.startswith("/api/"):
        return await handler(request)

    try:
        request[USER_KEY] = await resolve_user(request)
    except AuthError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=401)
    return await handler(request)


@web.middleware
async def json_error_middleware(request: web.Request, handler):
    """Единый формат ошибок: игровые — 400, остальные — 500 без падения сервера."""
    metrics.count_request(request.path)
    try:
        response = await handler(request)
    except web.HTTPException:
        raise
    except BettingError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        metrics.count_error(request.path)
        log.exception("Ошибка мини-приложения на %s", request.path)
        await _alert(request.app, request.path, exc)
        return web.json_response({"ok": False, "error": f"Внутренняя ошибка: {exc}"}, status=500)
    if isinstance(response, web.Response) and response.status >= 500:
        metrics.count_error(request.path)
    return response


async def _alert(app: web.Application, path: str, exc: Exception) -> None:
    """Пишем админу в Telegram, но не чаще раза в пять минут: иначе 500-е спамят."""
    notifier = app.get(ALERT_KEY)
    if notifier is None:
        return
    now = time.monotonic()
    last = app.get(_ALERT_AT_KEY, 0.0)
    if now - last < 300:
        return
    app[_ALERT_AT_KEY] = now
    try:
        await notifier(f"⚠️ 500 в мини-аппе на {path}: {type(exc).__name__}: {exc}"[:900])
    except Exception:      # алерт не должен ломать ответ игроку
        log.debug("Не смог отправить алерт админу", exc_info=True)


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """Тормоз для шквала запросов от одного игрока."""
    if not request.path.startswith("/api/") or request.path in ("/api/health", "/api/metrics"):
        return await handler(request)
    user = request.get(USER_KEY) or {}
    user_id = int(user.get("user_id") or 0)
    if user_id and not limiter.allow(user_id, request.path):
        metrics.rejected += 1
        return web.json_response(
            {"ok": False, "error": "Слишком часто — подожди минуту"},
            status=429,
            headers={"Retry-After": "60"},
        )
    return await handler(request)


# Мельче этого ответы не сжимаем: накладные расходы больше выигрыша.
COMPRESS_MIN_BYTES = 1024


@web.middleware
async def gzip_middleware(request: web.Request, handler):
    """Сжимает большие JSON-ответы: линия с росписью — сотни килобайт.

    Мини-приложение открывается с телефона, и без сжатия `/api/state` с 60+ рынками
    на каждый футбольный матч весит около мегабайта. gzip срезает это в разы,
    а несжатым остаётся всё мелкое — заголовки кэша и тело вебхука не раздуваем.
    """
    response = await handler(request)
    if (
        isinstance(response, web.Response)
        and response.body is not None
        and len(response.body) > COMPRESS_MIN_BYTES
        and "Content-Encoding" not in response.headers
        and request.headers.get("Accept-Encoding", "").find("gzip") >= 0
    ):
        response.enable_compression()
    return response


MIDDLEWARES = [json_error_middleware, auth_middleware, rate_limit_middleware, gzip_middleware]

__all__ = [
    "ALERT_KEY",
    "BOT_KEY",
    "COMPRESS_MIN_BYTES",
    "DB_KEY",
    "MIDDLEWARES",
    "USER_KEY",
    "auth_middleware",
    "gzip_middleware",
    "json_error_middleware",
    "rate_limit_middleware",
    "resolve_user",
]

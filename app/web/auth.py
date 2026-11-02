"""Авторизация мини-приложения и общая обработка ошибок API.

Отдельный модуль, чтобы `server.py` занимался только маршрутами и обработчиками.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from app.config import settings
from app.db import BettingError
from app.services.miniapp import AuthError, validate_init_data

log = logging.getLogger(__name__)

DB_KEY = web.AppKey("db", Any)
USER_KEY = web.RequestKey("user", dict)


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
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except BettingError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("Ошибка мини-приложения на %s", request.path)
        return web.json_response({"ok": False, "error": f"Внутренняя ошибка: {exc}"}, status=500)


MIDDLEWARES = [json_error_middleware, auth_middleware]

__all__ = ["DB_KEY", "MIDDLEWARES", "USER_KEY", "auth_middleware", "json_error_middleware", "resolve_user"]

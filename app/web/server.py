"""HTTP-сервер мини-приложения (Telegram WebApp).

Тот же SQLite, что у бота: одно состояние, разные интерфейсы.
Запуск:  python miniapp.py   (порт MINIAPP_PORT, по умолчанию 8081)

Авторизация — подписанный Telegram initData. Для локальной разработки можно задать
MINIAPP_DEV_USER=<telegram_id>, и приложение будет работать без Telegram.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from app.config import settings
from app.db import BettingError, Database
from app.services.miniapp import AuthError, validate_init_data

log = logging.getLogger(__name__)

DB_KEY = web.AppKey("db", Database)
USER_KEY = web.AppKey("user", dict)

WEB_DIR = Path(__file__).resolve().parent
INDEX_PATH = WEB_DIR / "index.html"


# --------------------------------------------------------------- сериализация
def match_payload(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "tournament": str(row["tournament"]),
        "short_tournament": str(row["tournament"]).split(".")[1].strip()[:42]
        if "." in str(row["tournament"])
        else str(row["tournament"])[:42],
        "team_a": str(row["team_a"]),
        "team_b": str(row["team_b"]),
        "odds_a": float(row["odds_a"]),
        "odds_b": float(row["odds_b"]),
        "start_time": int(row["start_time"]),
        "status": str(row["status"]),
    }


def bet_payload(row: Any) -> dict[str, Any]:
    return {
        "match_id": int(row["match_id"]),
        "side": str(row["side"]),
        "amount": int(row["amount"]),
        "odds": float(row["odds"]),
        "team": row["team_a"] if row["side"] == "a" else row["team_b"],
        "team_a": row["team_a"],
        "team_b": row["team_b"],
        "potential": round(int(row["amount"]) * float(row["odds"])),
    }


def _display_name(user: dict[str, Any], row: Any = None) -> str:
    if row is not None and row["full_name"]:
        return str(row["full_name"])
    if row is not None and row["username"]:
        return f"@{row['username']}"
    if user["full_name"]:
        return str(user["full_name"])
    if user["username"]:
        return f"@{user['username']}"
    return f"id{user['user_id']}"


# ---------------------------------------------------------------------- auth
async def resolve_user(request: web.Request) -> dict[str, Any]:
    """Возвращает {user_id, username, full_name} или поднимает AuthError."""
    init_data = (
        request.headers.get("X-Telegram-Init-Data")
        or request.query.get("init_data")
        or ""
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
async def auth_middleware(request: web.Request, handler):  # noqa: ANN001, ANN201
    if not request.path.startswith("/api/"):
        return await handler(request)

    try:
        request[USER_KEY] = await resolve_user(request)
    except AuthError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=401)
    return await handler(request)


@web.middleware
async def json_error_middleware(request: web.Request, handler):  # noqa: ANN001, ANN201
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except BettingError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка мини-приложения на %s", request.path)
        return web.json_response({"ok": False, "error": f"Внутренняя ошибка: {exc}"}, status=500)


# -------------------------------------------------------------------- routes
MIDDLEWARES = [json_error_middleware, auth_middleware]


def register_routes(
    app: web.Application, db: Database, *, serve_health: bool = True, index_path: str = "/"
) -> web.Application:
    """Вешает API и интерфейс мини-приложения на готовое aiohttp-приложение.

    Нужно, чтобы в webhook-режиме бот и приложение жили в одном процессе и делили одну базу.
    """
    app[DB_KEY] = db
    if serve_health:
        app.router.add_get("/health", health)
    app.router.add_get(index_path, index)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/bet", api_bet)
    app.router.add_post("/api/express", api_express)
    app.router.add_post("/api/bonus", api_bonus)
    app.router.add_get("/api/top", api_top)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/health", health)
    return app


def build_app(db: Database, bot_token: str | None = None) -> web.Application:
    if bot_token is not None:
        settings.bot_token = bot_token
    app = web.Application(middlewares=list(MIDDLEWARES))
    return register_routes(app, db)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "cs2-bets-miniapp", "time": int(time.time())})


def _render_index(boot: dict[str, Any]) -> str:
    html = INDEX_PATH.read_text(encoding="utf-8")
    payload = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    injection = f"<script>window.__BOOT__ = {payload};</script>"
    marker = "<!-- BOOTSTRAP -->"
    return html.replace(marker, injection) if marker in html else html.replace("</head>", injection + "</head>")


async def index(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    boot: dict[str, Any] = {"user": None, "matches": [], "dev": False, "error": None}

    try:
        user = await resolve_user(request)
    except AuthError as exc:
        boot["error"] = str(exc)
    else:
        if not user["dev"]:
            await db.upsert_user(user["user_id"], user["username"], user["full_name"])
        row = await db.get_user(user["user_id"])
        boot["user"] = {
            "id": user["user_id"],
            "name": _display_name(user, row),
            "balance": await db.get_balance(user["user_id"]),
        }
        boot["dev"] = user["dev"]
        boot["matches"] = [match_payload(row) for row in await db.open_matches(limit=60)]

    return web.Response(text=_render_index(boot), content_type="text/html")


async def api_state(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    if not user["dev"]:
        await db.upsert_user(user["user_id"], user["username"], user["full_name"])

    matches = [match_payload(row) for row in await db.open_matches(limit=60)]
    bets = [bet_payload(row) for row in await db.user_active_bets(user["user_id"])]
    expresses = [
        {
            "id": int(row["id"]),
            "stake": int(row["stake"]),
            "odds": float(row["odds"]),
            "potential": round(int(row["stake"]) * float(row["odds"])),
            "legs": int(row["legs"]) if "legs" in row.keys() else None,
        }
        for row in await db.user_open_expresses(user["user_id"])
    ]
    return web.json_response(
        {
            "ok": True,
            "user": {
                "id": user["user_id"],
                "name": _display_name(user, await db.get_user(user["user_id"])),
                "balance": await db.get_balance(user["user_id"]),
                "dev": user["dev"],
            },
            "matches": matches,
            "bets": bets,
            "expresses": expresses,
            "limits": {
                "min_bet": settings.min_bet,
                "max_legs": settings.max_express_legs,
                "max_odds": settings.max_express_odds,
                "daily_bonus": settings.daily_bonus,
            },
        }
    )


async def api_bet(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    body = await request.json() if request.can_read_body else {}

    match_id = int(body.get("match_id") or 0)
    side = str(body.get("side") or "")
    amount = int(body.get("amount") or 0)
    if side not in ("a", "b"):
        return web.json_response({"ok": False, "error": "Неверный исход"}, status=400)

    match = await db.get_match(match_id)
    balance, odds = await db.place_bet(match_id, user["user_id"], None, side, amount)
    return web.json_response(
        {
            "ok": True,
            "balance": balance,
            "odds": odds,
            "match": match_payload(match) if match is not None else None,
            "side": side,
            "amount": amount,
            "potential": round(amount * odds),
        }
    )


async def api_express(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    body = await request.json() if request.can_read_body else {}

    amount = int(body.get("amount") or 0)
    raw_legs = body.get("legs") or []
    legs = [
        {"match_id": int(leg.get("match_id") or 0), "side": str(leg.get("side") or "")}
        for leg in raw_legs
        if isinstance(leg, dict)
    ]
    if not legs or any(leg["side"] not in ("a", "b") for leg in legs):
        return web.json_response({"ok": False, "error": "Нужно минимум 2 исхода"}, status=400)

    express_id, odds, balance = await db.place_express(
        user["user_id"], None, amount, legs, max_odds=settings.max_express_odds
    )
    view_legs = []
    for leg in legs:
        match = await db.get_match(leg["match_id"])
        if match is None:
            continue
        view_legs.append(
            {
                "match_id": leg["match_id"],
                "side": leg["side"],
                "team": match["team_a"] if leg["side"] == "a" else match["team_b"],
                "odds": float(match["odds_a"] if leg["side"] == "a" else match["odds_b"]),
            }
        )

    return web.json_response(
        {
            "ok": True,
            "express_id": express_id,
            "odds": odds,
            "balance": balance,
            "amount": amount,
            "potential": round(amount * odds),
            "legs": view_legs,
        }
    )


async def api_bonus(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    ok, wait_seconds, balance = await db.daily_bonus(
        user["user_id"], settings.daily_bonus, settings.bonus_cooldown_hours * 3600
    )
    return web.json_response(
        {
            "ok": True,
            "received": ok,
            "amount": settings.daily_bonus if ok else 0,
            "wait_seconds": wait_seconds,
            "balance": balance,
        }
    )


async def api_top(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    rows = await db.top_users(10)
    return web.json_response(
        {
            "ok": True,
            "top": [
                {
                    "rank": index + 1,
                    "name": row["full_name"] or (f"@{row['username']}" if row["username"] else f"id{row['user_id']}"),
                    "balance": int(row["balance"]),
                    "me": int(row["user_id"]) == int(request[USER_KEY]["user_id"]),
                }
                for index, row in enumerate(rows)
            ],
        }
    )


async def api_history(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    rows = await db.history(request[USER_KEY]["user_id"], 20)
    return web.json_response(
        {
            "ok": True,
            "items": [
                {
                    "delta": int(row["delta"]),
                    "reason": str(row["reason"]),
                    "at": int(row["created_at"]),
                }
                for row in rows
            ],
        }
    )

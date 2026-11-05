"""HTTP-сервер мини-приложения (Telegram WebApp).

Только маршруты и обработчики: авторизация живёт в `auth.py`, сборка JSON — в `payloads.py`,
разметка — в `index.html`, стили и скрипт — в `static/`.

Запуск отдельно:  python miniapp.py   (порт MINIAPP_PORT, по умолчанию 8081)
В облаке тот же сервер поднимает бот в webhook-режиме — одна база, один процесс.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from app.config import settings
from app.db import Database
from app.services.bonus import TEASE_PRIZE, TEASE_TAPS, daily_layout, slots_payload, spin
from app.services.football import market_label, parse_side
from app.services.hockey import market_label as hockey_label
from app.services.hockey import parse_side as hockey_side
from app.services.miniapp import AuthError
from app.web.auth import DB_KEY, MIDDLEWARES, USER_KEY, resolve_user
from app.web.payloads import (
    bet_payload,
    bets_history,
    display_name,
    express_payload,
    match_payload,
    side_label,
)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
INDEX_PATH = WEB_DIR / "index.html"
STATIC_DIR = WEB_DIR / "static"

BOOTSTRAP_MARKER = "<!-- BOOTSTRAP -->"
# Мельче этого ответы не сжимаем: накладные расходы больше выигрыша.
COMPRESS_MIN_BYTES = 1024
ALLOWED_STATIC = re.compile(r"^[a-z0-9_-]+\.(css|js|png|svg|ico|webp)$")


# ------------------------------------------------------------------- маршруты
def register_routes(
    app: web.Application, db: Database, *, serve_health: bool = True, index_path: str = "/"
) -> web.Application:
    """Вешает API и интерфейс на готовое aiohttp-приложение."""
    app[DB_KEY] = db
    if serve_health:
        app.router.add_get("/health", health)
        app.router.add_get("/api/health", health)

    app.router.add_get(index_path, index)
    app.router.add_get("/static/{name}", static_file)

    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/bet", api_bet)
    app.router.add_post("/api/express", api_express)
    app.router.add_post("/api/bonus", api_bonus)
    app.router.add_post("/api/wheel", api_wheel)
    app.router.add_post("/api/tease", api_tease)
    app.router.add_get("/api/top", api_top)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/bets", api_bets)
    return app


def build_app(db: Database, bot_token: str | None = None) -> web.Application:
    if bot_token is not None:
        settings.bot_token = bot_token
    app = web.Application(middlewares=list(MIDDLEWARES))
    return register_routes(app, db)


async def health(request: web.Request) -> web.Response:
    payload: dict[str, Any] = {
        "ok": True,
        "service": "cs2-bets-miniapp",
        "time": int(time.time()),
        # по этим полям видно, с какими настройками реально крутится сервис
        "sync_interval": settings.sync_interval,
        "live_betting": settings.live_betting,
        "backup_interval_hours": settings.backup_interval_hours,
    }
    # состояние бэкапов: видно, что копии реально снимаются, без захода в бота
    try:
        db: Database = request.app[DB_KEY]
        last = await db.last_backup()
        if last is not None:
            payload["last_backup_at"] = int(last["created_at"])
            payload["last_backup_age_minutes"] = (int(time.time()) - int(last["created_at"])) // 60
            payload["last_backup_kb"] = round(int(last["size_bytes"]) / 1024, 1)
        else:
            payload["last_backup_at"] = None
    except Exception:      # health не должен падать из-за базы
        log.debug("Не смог прочитать состояние бэкапов", exc_info=True)
    return web.json_response(payload)


async def _line_with_maps(db: Database, rows: list[Any]) -> list[dict[str, Any]]:
    """Матчи вместе с рынками «победа в карте» — один запрос на список."""
    maps = await db.maps_for_matches([int(row["id"]) for row in rows])
    return [match_payload(row, maps.get(int(row["id"]), [])) for row in rows]


# ------------------------------------------------------------------ интерфейс
def render_index(boot: dict[str, Any]) -> str:
    """Подставляет стартовые данные прямо в HTML — приложению не нужен лишний запрос."""
    html = INDEX_PATH.read_text(encoding="utf-8")
    payload = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    injection = f"<script>window.__BOOT__ = {payload};</script>"
    if BOOTSTRAP_MARKER in html:
        return html.replace(BOOTSTRAP_MARKER, injection)
    return html.replace("</head>", injection + "</head>")


async def static_file(request: web.Request) -> web.Response:
    """Отдаём стили и скрипт без кеша: после деплоя клиент сразу видит новую версию."""
    name = request.match_info["name"]
    if not ALLOWED_STATIC.match(name):
        return _bad("Нет такого файла", 404)
    path = STATIC_DIR / name
    if not path.is_file():
        return _bad("Нет такого файла", 404)
    suffix = Path(name).suffix
    headers = {"Cache-Control": "no-cache, must-revalidate"}
    if suffix in {".css", ".js", ".svg"}:
        content_type = {".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml"}[suffix]
        return web.Response(text=path.read_text(encoding="utf-8"), content_type=content_type, headers=headers)

    content_type = {".png": "image/png", ".ico": "image/x-icon", ".webp": "image/webp"}[suffix]
    return web.Response(body=path.read_bytes(), content_type=content_type, headers=headers)


async def index(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    boot: dict[str, Any] = {"user": None, "matches": [], "live": [], "dev": False, "error": None}

    try:
        user = await resolve_user(request)
    except AuthError as exc:
        boot["error"] = str(exc)
    else:
        if not user["dev"]:
            await db.upsert_user(user["user_id"], user["username"], user["full_name"])
        boot["user"] = {
            "id": user["user_id"],
            "name": display_name(user, await db.get_user(user["user_id"])),
            "balance": await db.get_balance(user["user_id"]),
        }
        boot["dev"] = user["dev"]
        boot["matches"] = await _line_with_maps(db, await db.open_matches(limit=60, sport="cs2"))
        boot["live"] = await _line_with_maps(db, await db.live_matches(limit=40))
        boot["football"] = await _line_with_maps(
            db, await db.open_matches(limit=200, sport="football")
        )
        boot["hockey"] = await _line_with_maps(
            db, await db.open_matches(limit=120, sport="hockey")
        )

    return web.Response(text=render_index(boot), content_type="text/html")


# ----------------------------------------------------------------------- API
async def api_state(request: web.Request) -> web.Response:
    """Всё для главного экрана: игрок, линия, ставки в игре, лимиты."""
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    user_id = int(user["user_id"])

    if not user["dev"]:
        await db.upsert_user(user_id, user["username"], user["full_name"])

    return web.json_response(
        {
            "ok": True,
            "user": {
                "id": user_id,
                "name": display_name(user, await db.get_user(user_id)),
                "balance": await db.get_balance(user_id),
                "dev": user["dev"],
            },
            "matches": await _line_with_maps(db, await db.open_matches(limit=60, sport="cs2")),
            "live": await _line_with_maps(db, await db.live_matches(limit=40)),
            "football": await _line_with_maps(
                db, await db.open_matches(limit=200, sport="football")
            ),
            "hockey": await _line_with_maps(
                db, await db.open_matches(limit=120, sport="hockey")
            ),
            "bets": [bet_payload(row) for row in await db.user_active_bets(user_id)],
            "expresses": [express_payload(row) for row in await db.user_open_expresses(user_id)],
            "bonus": await _bonus_payload(db, user_id),
            "limits": {
                "min_bet": settings.min_bet,
                "max_legs": settings.max_express_legs,
                "max_odds": settings.max_express_odds,
                "daily_bonus": settings.daily_bonus,
            },
        }
    )


async def _bonus_payload(db: Database, user_id: int) -> dict[str, Any]:
    """Что доступно: ежедневный бонус, колесо, пасхалка."""
    state = await db.bonus_state(
        user_id,
        settings.bonus_cooldown_hours * 3600,
        settings.wheel_cooldown_hours * 3600,
        settings.tease_cooldown_hours * 3600,
    )
    return {
        "daily": {
            "amount": settings.daily_bonus,
            "available": state["daily"]["wait_seconds"] == 0,
            "wait_seconds": state["daily"]["wait_seconds"],
        },
        "wheel": {
            "available": state["wheel"]["wait_seconds"] == 0,
            "wait_seconds": state["wheel"]["wait_seconds"],
            "slots": slots_payload(daily_layout()),
            "cost": settings.wheel_cost,
            "tease_taps": TEASE_TAPS,
        },
        "tease": {
            "amount": TEASE_PRIZE,
            "available": state["tease"]["wait_seconds"] == 0,
            "wait_seconds": state["tease"]["wait_seconds"],
        },
    }


async def _json_body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _bad(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _as_int(value: Any) -> int:
    """Терпимый int: «abc» из запроса — это 0, а не 500 на весь экран."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def api_bet(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    body = await _json_body(request)

    match_id = _as_int(body.get("match_id"))
    side = str(body.get("side") or "")
    amount = _as_int(body.get("amount"))
    market = str(body.get("market") or "match")
    map_id = _as_int(body.get("map_id"))

    hockey = hockey_side(side)
    if hockey is not None:      # рынок хоккея: основное время, победа в матче, тотал
        balance, odds = await db.place_hockey_bet(
            match_id, int(user["user_id"]), None, hockey[0], hockey[1], amount
        )
        match = await db.get_match(match_id)
        return web.json_response(
            {
                "ok": True,
                "balance": balance,
                "odds": odds,
                "match": match_payload(match) if match is not None else None,
                "market": "hockey",
                "side": side,
                "label": hockey_label(hockey[0], hockey[1]),
                "amount": amount,
                "potential": round(amount * odds),
            }
        )

    parsed = parse_side(side)
    if parsed is not None:      # рынок футбола: тотал, фора, точный счёт…
        balance, odds = await db.place_football_bet(
            match_id, int(user["user_id"]), None, parsed[0], parsed[1], amount
        )
        match = await db.get_match(match_id)
        return web.json_response(
            {
                "ok": True,
                "balance": balance,
                "odds": odds,
                "match": match_payload(match) if match is not None else None,
                "market": "football",
                "side": side,
                "label": market_label(*parsed),
                "amount": amount,
                "potential": round(amount * odds),
            }
        )

    if side not in ("a", "x", "b"):
        return _bad("Неверный исход")
    if side == "x" and market != "match":
        return _bad("Ничья — только для основного исхода")

    if market == "map":
        if not map_id:
            return _bad("Не выбрана карта")
        card = await db.get_map(map_id)
        if card is not None and match_id and int(card["match_id"]) != match_id:
            return _bad("Эта карта — из другого матча")
        balance, odds = await db.place_map_bet(
            map_id, int(user["user_id"]), None, side, amount
        )
        match = await db.get_match(int(card["match_id"])) if card is not None else None
    else:
        balance, odds = await db.place_bet(match_id, int(user["user_id"]), None, side, amount)
        match = await db.get_match(match_id)
    return web.json_response(
        {
            "ok": True,
            "balance": balance,
            "odds": odds,
            "match": match_payload(match) if match is not None else None,
            "market": market,
            "side": side,
            "amount": amount,
            "potential": round(amount * odds),
        }
    )


async def api_express(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    body = await _json_body(request)

    amount = _as_int(body.get("amount"))
    raw_legs = body.get("legs") or []
    legs = [
        {"match_id": _as_int(leg.get("match_id")), "side": str(leg.get("side") or "")}
        for leg in raw_legs
        if isinstance(leg, dict)
    ]
    if len(legs) < 2 or any(leg["side"] not in ("a", "x", "b") for leg in legs):
        return _bad("Нужно минимум 2 исхода")

    express_id, odds, balance = await db.place_express(
        int(user["user_id"]), None, amount, legs, max_odds=settings.max_express_odds
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
                "team": side_label(
                    leg["side"], str(match["team_a"]), str(match["team_b"])
                ),
                "odds": float(
                    {"a": match["odds_a"], "x": match["odds_x"], "b": match["odds_b"]}[leg["side"]]
                ),
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
    received, wait_seconds, balance = await db.daily_bonus(
        int(user["user_id"]), settings.daily_bonus, settings.bonus_cooldown_hours * 3600
    )
    return web.json_response(
        {
            "ok": True,
            "received": received,
            "amount": settings.daily_bonus if received else 0,
            "wait_seconds": wait_seconds,
            "balance": balance,
        }
    )


async def api_wheel(request: web.Request) -> web.Response:
    """Крутка колеса. Приз определяет сервер — клиент только показывает результат.

    Первая крутка в сутки бесплатная, дальше каждая стоит settings.wheel_cost.
    """
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    cooldown = settings.wheel_cooldown_hours * 3600
    layout = daily_layout()

    state = await db.bonus_state(
        user_id, settings.bonus_cooldown_hours * 3600, cooldown,
        settings.tease_cooldown_hours * 3600,
    )
    free_left = state["wheel"]["wait_seconds"] == 0
    slot, prize = spin(layout)

    if free_left:
        spun, _, balance = await db.claim_wheel(user_id, prize, cooldown)
        if spun:
            return web.json_response(
                {
                    "ok": True, "spun": True, "free": True, "slot": slot, "prize": prize,
                    "wait_seconds": 0, "balance": balance, "cost": settings.wheel_cost,
                }
            )
        # кто-то успел крутнуть параллельно — считаем как платную, если хватает монет
        free_left = False

    paid, _, balance = await db.claim_paid_wheel(user_id, settings.wheel_cost, prize)
    if not paid:
        return web.json_response(
            {
                "ok": True, "spun": False, "free": False,
                "wait_seconds": 0, "cost": settings.wheel_cost, "balance": balance,
                "error": f"Нужно {settings.wheel_cost} 🪙 — приз за бесплатную крутку уже получен",
            }
        )

    return web.json_response(
        {
            "ok": True, "spun": True, "free": False, "paid": True,
            "slot": slot, "prize": prize, "cost": settings.wheel_cost,
            "wait_seconds": 0, "balance": balance,
        }
    )


async def api_tease(request: web.Request) -> web.Response:
    """Пасхалка: десять нажатий на бонус — полтос на ход ноги."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    claimed, wait_seconds, balance = await db.claim_tease(
        user_id, TEASE_PRIZE, settings.tease_cooldown_hours * 3600
    )
    return web.json_response(
        {
            "ok": True,
            "claimed": claimed,
            "amount": TEASE_PRIZE if claimed else 0,
            "wait_seconds": wait_seconds,
            "balance": balance,
        }
    )


async def api_top(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    me = int(request[USER_KEY]["user_id"])
    rows = await db.top_users(10)
    return web.json_response(
        {
            "ok": True,
            "top": [
                {
                    "rank": index + 1,
                    "name": display_name({"user_id": me, "username": None, "full_name": None}, row),
                    "balance": int(row["balance"]),
                    "me": int(row["user_id"]) == me,
                }
                for index, row in enumerate(rows)
            ],
        }
    )


async def api_history(request: web.Request) -> web.Response:
    """Движения баланса: ставки, выплаты, бонусы."""
    db: Database = request.app[DB_KEY]
    rows = await db.history(int(request[USER_KEY]["user_id"]), 20)
    return web.json_response(
        {
            "ok": True,
            "items": [
                {"delta": int(row["delta"]), "reason": str(row["reason"]), "at": int(row["created_at"])}
                for row in rows
            ],
        }
    )


async def api_bets(request: web.Request) -> web.Response:
    """История ставок: в игре и рассчитанные, одиночные + экспрессы."""
    db: Database = request.app[DB_KEY]
    payload = await bets_history(db, int(request[USER_KEY]["user_id"]))
    return web.json_response({"ok": True, **payload})


__all__ = ["DB_KEY", "STATIC_DIR", "USER_KEY", "build_app", "register_routes", "render_index"]

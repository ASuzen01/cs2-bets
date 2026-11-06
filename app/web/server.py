"""HTTP-сервер мини-приложения (Telegram WebApp).

Только маршруты и обработчики: авторизация живёт в `auth.py`, сборка JSON — в `payloads.py`,
разметка — в `index.html`, стили и скрипт — в `static/`.

Запуск отдельно:  python miniapp.py   (порт MINIAPP_PORT, по умолчанию 8081)
В облаке тот же сервер поднимает бот в webhook-режиме — одна база, один процесс.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from app.config import settings
from app.db import Database
from app.db.database import InsufficientFunds, InvalidBet, MatchNotFound, bonus_amount
from app.services import achievements, baccarat, games, slot_machine
from app.services import blackjack as blackjack_game
from app.services import crash as crash_game
from app.services import ladder as ladder_game
from app.services.bonus import (
    TEASE_PRIZE,
    TEASE_TAPS,
    WHEEL_JACKPOT,
    average_prize,
    daily_layout,
    is_ticket,
    slots_payload,
    spin,
)
from app.services.coupon import item_of as coupon_item
from app.services.coupon import render_coupon
from app.services.football import market_label, parse_side
from app.services.hockey import market_label as hockey_label
from app.services.hockey import parse_side as hockey_side
from app.services.legs import label as leg_label
from app.services.miniapp import AuthError
from app.services.quests import chain_chest, day_number, next_chest
from app.services.quests import payload as quests_payload
from app.web.auth import BOT_KEY, DB_KEY, MIDDLEWARES, USER_KEY, resolve_user
from app.web.limits import metrics
from app.web.payloads import (
    bet_payload,
    bets_history,
    display_name,
    express_payload,
    match_payload,
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
        app.router.add_get("/metrics", metrics_view)

    app.router.add_get(index_path, index)
    app.router.add_get("/static/{name}", static_file)

    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/bet", api_bet)
    app.router.add_post("/api/express", api_express)
    app.router.add_post("/api/bonus", api_bonus)
    app.router.add_post("/api/wheel", api_wheel)
    app.router.add_post("/api/tease", api_tease)
    app.router.add_post("/api/quest", api_quest)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/coupon/{kind}/{bet_id}.png", api_coupon)
    app.router.add_post("/api/share", api_share)
    app.router.add_get("/api/achievements", api_achievements)
    app.router.add_get("/api/referral", api_referral)
    app.router.add_post("/api/referral", api_referral_attach)
    app.router.add_post("/api/quest/chest", api_quest_chest)
    app.router.add_post("/api/cashout", api_cashout)
    app.router.add_get("/api/honesty", api_honesty)
    app.router.add_get("/api/top", api_top)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/bets", api_bets)
    app.router.add_get("/api/casino", api_casino_state)
    app.router.add_post("/api/casino/play", api_casino_play)
    app.router.add_post("/api/blackjack/start", api_blackjack_start)
    app.router.add_post("/api/blackjack/action", api_blackjack_action)
    app.router.add_post("/api/ladder/start", api_ladder_start)
    app.router.add_post("/api/ladder/pick", api_ladder_pick)
    app.router.add_post("/api/ladder/continue", api_ladder_continue)
    app.router.add_post("/api/ladder/cashout", api_ladder_cashout)
    app.router.add_post("/api/crash/start", api_crash_start)
    app.router.add_get("/api/crash/state", api_crash_state)
    app.router.add_post("/api/crash/cashout", api_crash_cashout)
    return app


def build_app(db: Database, bot_token: str | None = None) -> web.Application:
    if bot_token is not None:
        settings.bot_token = bot_token
    app = web.Application(middlewares=list(MIDDLEWARES))
    return register_routes(app, db)


async def metrics_view(request: web.Request) -> web.Response:
    """Счётчики для мониторинга: обычный текст, читается и глазами, и Prometheus."""
    return web.Response(text=metrics.text(), content_type="text/plain", charset="utf-8")


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

    # настоящая проверка базы: без неё «ok» значит только «процесс жив»
    try:
        db = request.app[DB_KEY]
        await db.get_meta("health_probe")
        payload["db"] = "ok"
    except Exception as exc:
        payload["db"] = f"error: {exc}"
        payload["ok"] = False
        return web.json_response(payload, status=503)

    # свежесть линии: если синк молчит, кэфы и результаты устарели — это видно здесь
    try:
        raw = await request.app[DB_KEY].get_meta("last_sync_at")
        last_sync = int(raw) if raw else 0
        age = int(time.time()) - last_sync if last_sync else None
        payload["last_sync_at"] = last_sync or None
        payload["last_sync_age_seconds"] = age
        payload["feed_stale"] = bool(age is None or age > max(900, settings.sync_interval * 20))
    except Exception:
        payload["feed_stale"] = None
    return web.json_response(payload)


async def _line_with_maps(db: Database, rows: list[Any]) -> list[dict[str, Any]]:
    """Матчи вместе с рынками «победа в карте» — один запрос на список."""
    maps = await db.maps_for_matches([int(row["id"]) for row in rows])
    return [match_payload(row, maps.get(int(row["id"]), [])) for row in rows]


# ------------------------------------------------------------------ интерфейс
def render_index(boot: dict[str, Any]) -> str:
    """Подставляет стартовые данные прямо в HTML — приложению не нужен лишний запрос."""
    html = INDEX_PATH.read_text(encoding="utf-8")
    # Telegram WebView/CDN иногда удерживают старые app.js даже после выкладки.
    # Версия коммита заставляет клиента загрузить свежие assets без ручного cache-bust.
    asset_version = (os.getenv("RENDER_GIT_COMMIT") or "local")[:12]
    html = html.replace('href="/static/app.css"', f'href="/static/app.css?v={asset_version}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={asset_version}"')
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

    return web.Response(
        text=render_index(boot),
        content_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# ----------------------------------------------------------------------- API
async def api_state(request: web.Request) -> web.Response:
    """Всё для главного экрана: игрок, линия, ставки в игре, лимиты."""
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    user_id = int(user["user_id"])

    if not user["dev"]:
        await db.upsert_user(user_id, user["username"], user["full_name"])

    quests = quests_payload(await db.quests_state(user_id), day_number())
    fresh = await achievements.sync(db, user_id)
    chain = await db.quest_chain(user_id)
    return web.json_response(
        {
            "ok": True,
            "quests": quests,
            "chain": {
                **chain,
                "chest": chain_chest(chain["days"]),
                "next": next_chest(chain["days"]),
            },
            "achievements": await achievements.payload(db, user_id),
            "new_achievements": [achievements.public(item) for item in fresh],
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
            # билет колеса или фрибет возврата — каждый с подходящим типом ставки
            "tickets": [
                {
                    "id": int(row["id"]),
                    "amount": int(row["amount"]),
                    "kind": str(row["kind"] or "express"),
                    "expires_at": int(row["expires_at"]) if row["expires_at"] else None,
                }
                for row in await db.tickets(user_id)
            ],
            "casino": await _casino_payload(db, user_id),
            "blackjack": await db.blackjack_state(user_id),
            "ladder": await db.ladder_state(user_id),
            "crash": await db.crash_state(user_id),
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
    streak = int(state["daily"].get("streak") or 0)
    next_streak = streak + 1 if streak else 1
    return {
        "daily": {
            "amount": bonus_amount(settings.daily_bonus, next_streak),
            "base_amount": settings.daily_bonus,
            "available": state["daily"]["wait_seconds"] == 0,
            "wait_seconds": state["daily"]["wait_seconds"],
            "streak": streak,
            "next_streak": next_streak,
            "bonus_percent": round(bonus_amount(settings.daily_bonus, next_streak) * 100 / settings.daily_bonus) - 100,
        },
        "wheel": {
            "available": state["wheel"]["wait_seconds"] == 0,
            "wait_seconds": state["wheel"]["wait_seconds"],
            "slots": slots_payload(daily_layout()),
            "cost": settings.wheel_cost,
            "ticket": settings.wheel_ticket_value,
            "jackpot": WHEEL_JACKPOT,
            "average": round(average_prize()),
            "tease_taps": TEASE_TAPS,
        },
        "tease": {
            "amount": TEASE_PRIZE,
            "available": state["tease"]["wait_seconds"] == 0,
            "wait_seconds": state["tease"]["wait_seconds"],
        },
    }


async def honesty_payload(db: Database) -> dict[str, Any]:
    """Что отдаёт каждая игра: считаем по правилам, а не «на слово».

    Рулетка — европейское колесо на 37 лунок, кости — два кубика, колесо — 24
    сектора из `WHEEL_VALUES`. Для ставок показываем реальные суммы по линии.
    """
    roulette_rtp = round(18 / games.ROULETTE_SLOTS * 2 * 100, 1)      # внешние ставки
    wheels = daily_layout()
    expected = average_prize()
    cost = max(1, int(settings.wheel_cost))
    totals = await db.line_totals()
    return {
        "roulette": {
            "rtp": roulette_rtp,
            "slots": games.ROULETTE_SLOTS,
            "note": "европейское колесо: 37 лунок, один ноль",
        },
        "dice": {
            "rtp": round(games.DICE_EDGE * 100, 1),
            "note": "два кубика, 36 равновероятных исходов",
        },
        "wheel": {
            "slots": len(wheels),
            "empty": sum(1 for value in wheels if value == 0),
            "expected": round(expected),
            "cost": cost,
            "rtp": round(expected / cost * 100, 1),
            "jackpot": int(WHEEL_JACKPOT),
            "ticket": settings.wheel_ticket_value,
            "tickets": sum(1 for value in wheels if is_ticket(value)),
            "note": "бесплатная крутка раз в сутки; платная — по цене ниже",
        },
        "line": totals,
        "line_rtp": round(
            (totals["bets_paid"] + totals["express_paid"])
            * 100
            / max(1, totals["bets_staked"] + totals["express_staked"]),
            1,
        ),
        "betting_note": (
            "Кэфы ставок — из линии, маржа остаётся у букмекера; "
            "монеты виртуальные, вывести их нельзя"
        ),
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
    """Терпимый int в диапазоне BIGINT: ошибочный ввод превращается в 0."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if -(2**63) <= parsed <= 2**63 - 1 else 0


async def api_bet(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user = request[USER_KEY]
    body = await _json_body(request)

    match_id = _as_int(body.get("match_id"))
    side = str(body.get("side") or "")
    amount = _as_int(body.get("amount"))
    market = str(body.get("market") or "match")
    map_id = _as_int(body.get("map_id"))
    # фрибет: им оплачивают ординар — сумма должна совпасть с номиналом
    ticket_id = _as_int(body.get("ticket_id")) or None

    if amount <= 0:
        return _bad("Сумма должна быть положительной")
    if amount < settings.min_bet:
        return _bad(f"Минимальная ставка — {settings.min_bet} 🪙")

    hockey = hockey_side(side)
    if hockey is not None:      # рынок хоккея: основное время, победа в матче, тотал
        metrics.count_bet()
        balance, odds = await db.place_hockey_bet(
            match_id, int(user["user_id"]), None, hockey[0], hockey[1], amount,
            token=body.get("token"), ticket_id=ticket_id,
        )
        match = await db.get_match(match_id)
        fresh = await _after_action(db, int(user["user_id"]))
        return web.json_response(
            {
                "ok": True,
                "new_achievements": fresh,
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
        metrics.count_bet()
        balance, odds = await db.place_football_bet(
            match_id, int(user["user_id"]), None, parsed[0], parsed[1], amount,
            token=body.get("token"), ticket_id=ticket_id,
        )
        match = await db.get_match(match_id)
        fresh = await _after_action(db, int(user["user_id"]))
        return web.json_response(
            {
                "ok": True,
                "new_achievements": fresh,
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

    metrics.count_bet()
    if market == "map":
        if not map_id:
            return _bad("Не выбрана карта")
        card = await db.get_map(map_id)
        if card is not None and match_id and int(card["match_id"]) != match_id:
            return _bad("Эта карта — из другого матча")
        balance, odds = await db.place_map_bet(
            map_id, int(user["user_id"]), None, side, amount,
            token=body.get("token"), ticket_id=ticket_id,
        )
        match = await db.get_match(int(card["match_id"])) if card is not None else None
    else:
        balance, odds = await db.place_bet(
            match_id, int(user["user_id"]), None, side, amount,
            token=body.get("token"), ticket_id=ticket_id,
        )
        match = await db.get_match(match_id)
    fresh = await _after_action(db, int(user["user_id"]))
    return web.json_response(
        {
            "ok": True,
            "new_achievements": fresh,
            "balance": balance,
            "odds": odds,
            "match": match_payload(match) if match is not None else None,
            "market": market,
            "side": side,
            "amount": amount,
            "potential": round(amount * odds),
        }
    )


async def _after_action(db: Database, user_id: int) -> list[dict[str, Any]]:
    """Что делаем после любой ставки: реферальный бонус другу и достижения.

    Ошибки здесь не должны ломать саму ставку — она уже прошла.
    """
    try:
        await db.pay_referral(user_id, settings.referral_bonus, settings.referral_welcome)
        return [achievements.public(item) for item in await achievements.sync(db, user_id)]
    except Exception:
        log.debug("Постобработка ставки не удалась", exc_info=True)
        return []


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
    # исход матча (a/x/b) или рынок (f:… футбол, h:… хоккей) — что именно,
    # проверит база: у неё есть линия и кэфы
    if len(legs) < 2 or any(not leg["side"] for leg in legs):
        return _bad("Нужно минимум 2 исхода")
    ticket_id = _as_int(body.get("ticket_id")) or None
    if ticket_id is None and amount < settings.min_bet:
        return _bad(f"Минимальная ставка — {settings.min_bet} 🪙")

    metrics.count_express()
    express_id, odds, balance = await db.place_express(
        int(user["user_id"]),
        None,
        amount,
        legs,
        max_odds=settings.max_express_odds,
        token=body.get("token"),
        ticket_id=ticket_id,
    )

    # кэфы ног берём из того, что реально записалось в базу: клиентские цифры
    # не должны попадать в ответ
    view_legs = [
        {
            "match_id": int(item["match_id"]),
            "side": str(item["side"]),
            "team": leg_label(item, str(item["side"])),
            "odds": float(item["odds"]),
        }
        for item in await db.express_items(express_id)
    ]

    fresh = await _after_action(db, int(user["user_id"]))
    return web.json_response(
        {
            "ok": True,
            "new_achievements": fresh,
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
    metrics.count_bonus()
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
    slot, value = spin(layout)
    # билет — не монеты: на баланс не идёт, кладём его в профиль отдельно
    ticket = is_ticket(value)
    prize = 0 if ticket else int(value)

    if free_left:
        spun, _, balance = await db.claim_wheel(user_id, prize, cooldown)
        if spun:
            payload = {
                "ok": True, "spun": True, "free": True, "slot": slot, "prize": prize,
                "wait_seconds": 0, "balance": balance, "cost": settings.wheel_cost,
            }
            if ticket:
                payload["ticket"] = await _give_ticket(db, user_id)
            return web.json_response(payload)
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

    payload = {
        "ok": True, "spun": True, "free": False, "paid": True,
        "slot": slot, "prize": prize, "cost": settings.wheel_cost,
        "wait_seconds": 0, "balance": balance,
    }
    if ticket:
        payload["ticket"] = await _give_ticket(db, user_id)
    return web.json_response(payload)


async def _give_ticket(db: Database, user_id: int) -> dict[str, Any]:
    """Выдаёт билет в экспресс: приз колеса, который нельзя «не заметить»."""
    ticket_id = await db.add_ticket(user_id, settings.wheel_ticket_value, "колесо")
    return {"id": ticket_id, "amount": settings.wheel_ticket_value}


async def api_coupon(request: web.Request) -> web.Response:
    """Картинка купона: PNG 1080×1350, годится и для чата, и для сторис."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    kind = str(request.match_info["kind"])
    try:
        bet_id = int(request.match_info["bet_id"])
    except ValueError:
        return _bad("Неверный номер ставки")

    item = await coupon_item(db, user_id, kind, bet_id)
    if item is None:
        return _bad("Ставка не найдена", 404)

    png = render_coupon(item)
    return web.Response(
        body=png,
        content_type="image/png",
        headers={"Cache-Control": "private, max-age=60"},
    )


async def api_share(request: web.Request) -> web.Response:
    """Отправить купон игроку в чат с ботом — оттуда его можно переслать другу."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    body = await _json_body(request)
    kind = str(body.get("kind") or "bet").strip().lower()
    try:
        bet_id = int(body.get("id") or 0)
    except (TypeError, ValueError):
        return _bad("Неверный номер ставки")

    item = await coupon_item(db, user_id, kind, bet_id)
    if item is None:
        return _bad("Ставка не найдена", 404)

    bot = request.app.get(BOT_KEY)
    if bot is None:
        # мини-апп, поднятый без бота: честно говорим, что чата нет, и отдаём картинку
        return _bad("Чат с ботом недоступен — купон можно открыть картинкой", 503)

    from aiogram.types import BufferedInputFile

    png = render_coupon(item)
    try:
        await bot.send_photo(
            chat_id=user_id,
            photo=BufferedInputFile(png, filename="coupon.png"),
            caption="Твой купон · Добрая лудка",
        )
    except Exception as exc:  # личка закрыта, бот заблокирован и т.п.
        log.info("Купон не ушёл в чат: %s", exc)
        return _bad("Бот не смог написать в личку — проверь, что он не заблокирован", 502)

    await db.log_event(user_id, "coupon_shared", kind)
    return web.json_response({"ok": True, "sent": True})


async def api_achievements(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    await achievements.sync(db, user_id)
    return web.json_response({"ok": True, **await achievements.payload(db, user_id)})


def _referral_link(user_id: int) -> str:
    """Ссылка-приглашение: открывает мини-апп с параметром ref_<id>."""
    base = settings.bot_username.strip().lstrip("@")
    if not base:
        return ""
    return f"https://t.me/{base}?startapp=ref_{user_id}"


async def api_referral(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    state = await db.referral_state(user_id, settings.referral_bonus)
    return web.json_response(
        {
            "ok": True,
            **state,
            "welcome": settings.referral_welcome,
            "link": _referral_link(user_id),
            "code": f"ref_{user_id}",
        }
    )


async def api_referral_attach(request: web.Request) -> web.Response:
    """Привязка приглашения: мини-апп пришёл с параметром ref_<id>."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    body = await _json_body(request)
    code = str(body.get("code") or "").strip()
    if not code.startswith("ref_") or not code[4:].isdigit():
        return _bad("Некорректная ссылка-приглашение")
    ok, error = await db.attach_referral(user_id, int(code[4:]))
    if not ok and error:
        return _bad(error)
    await db.log_event(user_id, "referral_attach", code[:32])
    return web.json_response({"ok": True, "attached": ok})


async def api_quest_chest(request: web.Request) -> web.Response:
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    received, amount, balance, error = await db.claim_chain_chest(user_id)
    if not received:
        return _bad(error)
    await achievements.sync(db, user_id)
    return web.json_response({"ok": True, "amount": amount, "balance": balance})


async def api_cashout(request: web.Request) -> web.Response:
    """Продажа ставки до старта: возвращаем 95 % ставки, ставка выходит из игры."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    body = await _json_body(request)
    bet_id = _as_int(body.get("bet_id"))
    if not bet_id:
        return _bad("Не выбрана ставка")
    try:
        refund, balance = await db.cashout_bet(bet_id, user_id, settings.cashout_rate)
    except MatchNotFound:
        return _bad("Ставка не найдена", 404)
    await db.log_event(user_id, "cashout", str(bet_id))
    return web.json_response({"ok": True, "refund": refund, "balance": balance})


async def api_honesty(request: web.Request) -> web.Response:
    """Открытые цифры: сколько отдаёт каждая игра и вся линия целиком."""
    db: Database = request.app[DB_KEY]
    return web.json_response({"ok": True, **await honesty_payload(db)})


async def api_quest(request: web.Request) -> web.Response:
    """Забрать награду за задание дня."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    body = await _json_body(request)
    code = str(body.get("code") or "").strip()

    claimed, reward, balance, error = await db.claim_quest(user_id, code)
    if claimed:
        await db.log_event(user_id, "quest_claimed", code)
    return web.json_response(
        {
            "ok": True,
            "claimed": claimed,
            "reward": reward,
            "balance": balance,
            "error": error,
            "quests": quests_payload(await db.quests_state(user_id), day_number()),
        }
    )


async def api_stats(request: web.Request) -> web.Response:
    """Итоги игрока для экрана «Я»: винрейт, ROI, любимый турнир."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    return web.json_response({"ok": True, "stats": await db.player_stats(user_id)})


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
    """Топ: общий зачёт по балансу и недельный сезон по прибыли со ставок."""
    db: Database = request.app[DB_KEY]
    me = int(request[USER_KEY]["user_id"])
    rows = await db.top_users(10)
    mine = await db.rank_of_user(me)
    season_start = db.week_start()
    season_rows = await db.season_top(10, season_start)
    season_mine = await db.season_rank(me, season_start)
    face = {"user_id": me, "username": None, "full_name": None}
    return web.json_response(
        {
            "ok": True,
            "me": mine,
            "top": [
                {
                    "rank": index + 1,
                    "name": display_name(face, row),
                    "balance": int(row["balance"]),
                    "me": int(row["user_id"]) == me,
                }
                for index, row in enumerate(rows)
            ],
            # сезон недели: счёт — чистая прибыль, а не баланс
            "season": {
                "starts_at": season_start,
                "top": [
                    {
                        "rank": index + 1,
                        "name": display_name(face, row),
                        "balance": int(row["score"]),
                        "me": int(row["user_id"]) == me,
                    }
                    for index, row in enumerate(season_rows)
                ],
                "me": season_mine,
            },
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


# -------------------------------------------------------------------- казино
async def _casino_payload(db: Database, user_id: int) -> dict[str, Any]:
    """Роспись игр, сводка и последние раунды — одним куском для экранов."""
    summary = await db.casino_summary(user_id)
    rows = await db.casino_history(user_id, 12)
    return {
        "options": {
            "roulette": games.game_options("roulette"),
            "dice": games.game_options("dice"),
            "baccarat": baccarat.options(),
        },
        "plinko": games.plinko_config(),
        "slots": slot_machine.slot_config(),
        "slot_state": await db.slot_state(user_id),
        "numbers": sorted(games.ROULETTE_RED),
        "red": sorted(games.ROULETTE_RED),
        "summary": {
            "rounds": int(summary["rounds"] or 0) if summary else 0,
            "staked": int(summary["staked"] or 0) if summary else 0,
            "paid": int(summary["paid"] or 0) if summary else 0,
            "wins": int(summary["wins"] or 0) if summary else 0,
            "net": int(summary["net"] or 0) if summary else 0,
        },
        "history": [
            {
                "id": int(row["id"]),
                "game": str(row["game"]),
                "label": str(row["label"]),
                "amount": int(row["amount"]),
                "odds": float(row["odds"]),
                "outcome": str(row["outcome"]),
                "win": bool(row["win"]),
                "payout": int(row["payout"]),
                "at": int(row["created_at"]),
            }
            for row in rows
        ],
    }


async def api_casino_state(request: web.Request) -> web.Response:
    """Экран игр: что можно поставить, как игралось раньше."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    return web.json_response(
        {"ok": True, "balance": await db.get_balance(user_id), **await _casino_payload(db, user_id)}
    )


async def api_casino_play(request: web.Request) -> web.Response:
    """Раунд рулетки, костей, Плинко, слот-машины или баккары.

    Клиент присылает игру, выбор и сумму. Исход бросает сервер (`services.games`),
    клиент получает его уже готовым и только доигрывает анимацию до нужного числа.
    """
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    body = await _json_body(request)

    game = str(body.get("game") or "").strip().lower()
    selection = str(body.get("selection") or "").strip().lower()
    amount = _as_int(body.get("amount"))

    if game not in ("roulette", "dice", "plinko", "slots", "baccarat"):
        return _bad("Такой игры нет")
    if amount < settings.min_bet:
        return _bad(f"Минимальная ставка — {settings.min_bet} 🪙")
    if not selection:
        return _bad("Выбери, на что ставишь")

    metrics.count_casino()
    if game == "slots":
        try:
            result = await db.play_slot(
                user_id,
                amount,
                spin=lambda wager, free: slot_machine.spin(wager, free_spin=free),
                token=body.get("token"),
            )
        except InsufficientFunds:
            return _bad("Недостаточно монет на балансе")
        except InvalidBet as exc:
            return _bad(str(exc))
        await db.pay_referral(user_id, settings.referral_bonus, settings.referral_welcome)
        return web.json_response({"ok": True, **result})

    try:
        result = baccarat.play(selection, amount) if game == "baccarat" else games.play(game, selection, amount)
    except ValueError as exc:
        return _bad(str(exc))

    try:
        balance, round_id = await db.play_casino(
            user_id,
            token=body.get("token"),
            game=game,
            selection=selection,
            label=result["label"],
            amount=amount,
            odds=result["odds"],
            outcome=result["outcome"],
            won=result["win"],
            payout=result["payout"],
            response=result,
        )
    except InsufficientFunds:
        return _bad("Недостаточно монет на балансе")
    except InvalidBet as exc:
        return _bad(str(exc))

    saved_response = await db.casino_round_response(user_id, round_id)
    if saved_response is not None:
        result = saved_response

    await db.pay_referral(user_id, settings.referral_bonus, settings.referral_welcome)
    return web.json_response({"ok": True, "round": round_id, "balance": balance, **result})


async def api_blackjack_start(request: web.Request) -> web.Response:
    """Начинает серверную раздачу классического блэкджека."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    body = await _json_body(request)
    amount = _as_int(body.get("amount"))
    if amount < settings.min_bet:
        return _bad(f"Минимальная ставка — {settings.min_bet} 🪙")
    metrics.count_casino()
    try:
        state = blackjack_game.new_game(amount)
        game = await db.start_blackjack(user_id, amount, state, body.get("token"))
    except InsufficientFunds:
        return _bad("Недостаточно монет на балансе")
    except InvalidBet as exc:
        return _bad(str(exc))
    return web.json_response({"ok": True, "game": game, "balance": game["balance"]})


async def api_blackjack_action(request: web.Request) -> web.Response:
    """Исполняет hit/stand/double/split/insurance только на сервере."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    body = await _json_body(request)
    game_id = _as_int(body.get("game_id"))
    action = str(body.get("action") or "").strip().lower()
    token = body.get("token")
    revision = _as_int(body.get("revision"))
    if game_id <= 0:
        return _bad("Раздача не найдена")
    try:
        result = await db.blackjack_action(user_id, game_id, action, token, revision)
    except InsufficientFunds as exc:
        return _bad(str(exc))
    except InvalidBet as exc:
        return _bad(str(exc))
    return web.json_response({"ok": True, **result})


async def api_ladder_start(request: web.Request) -> web.Response:
    """Начинает «Лесенку»: ставку и призовая дверь фиксирует сервер."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    amount = _as_int((await _json_body(request)).get("amount"))
    try:
        stake = ladder_game.validate_stake(amount)
    except ValueError as exc:
        return _bad(str(exc))
    metrics.count_casino()
    try:
        game = await db.start_ladder(
            user_id, stake, ladder_game.choose_window(ladder_game.window_count(1)),
        )
    except InsufficientFunds:
        return _bad("Недостаточно монет на балансе")
    except InvalidBet as exc:
        return _bad(str(exc))
    return web.json_response({"ok": True, "game": game, "balance": game["balance"]})


async def api_ladder_pick(request: web.Request) -> web.Response:
    """Выбирает закрытая дверь и только после выбора раскрывает результат."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    body = await _json_body(request)
    game_id = _as_int(body.get("game_id"))
    window_index = _as_int(body.get("window_index"))
    game = await db.ladder_state(user_id)
    if game is None or int(game["id"]) != game_id:
        return _bad("Активная партия не найдена")
    try:
        current_payout = ladder_game.payout(int(game["stake"]), int(game["round"]))
        result = await db.pick_ladder(user_id, game_id, window_index, current_payout)
    except (ValueError, InvalidBet) as exc:
        return _bad(str(exc))
    return web.json_response({"ok": True, "game": result, "balance": result["balance"]})


async def api_ladder_continue(request: web.Request) -> web.Response:
    """Продолжает после найденного приза: добавляет дверь и прячет новый приз."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    game_id = _as_int((await _json_body(request)).get("game_id"))
    game = await db.ladder_state(user_id)
    if game is None or int(game["id"]) != game_id:
        return _bad("Активная партия не найдена")
    windows = int(game["round"]) + 2
    try:
        result = await db.continue_ladder(
            user_id, game_id, ladder_game.choose_window(windows)
        )
    except (ValueError, InvalidBet) as exc:
        return _bad(str(exc))
    return web.json_response({"ok": True, "game": result, "balance": result["balance"]})


async def api_ladder_cashout(request: web.Request) -> web.Response:
    """Забирает доступную выплату, найденную на текущем этаже."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    game_id = _as_int((await _json_body(request)).get("game_id"))
    try:
        result = await db.cashout_ladder(user_id, game_id)
    except InvalidBet as exc:
        return _bad(str(exc))
    return web.json_response({"ok": True, "game": result, "balance": result["balance"]})


async def api_crash_start(request: web.Request) -> web.Response:
    """Списывает ставку и начинает полёт с заранее зафиксированным исходом."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    amount = _as_int((await _json_body(request)).get("amount"))
    try:
        stake = crash_game.validate_stake(amount)
        result = await db.start_crash(user_id, stake, crash_game.crash_point())
    except ValueError as exc:
        return _bad(str(exc))
    except InsufficientFunds:
        return _bad("Недостаточно монет на балансе")
    except InvalidBet as exc:
        return _bad(str(exc))
    metrics.count_casino()
    return web.json_response({"ok": True, "game": result, "balance": result["balance"]})


async def api_crash_state(request: web.Request) -> web.Response:
    """Синхронизирует полёт/автоматически фиксирует проигрыш после вылета."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    game = await db.crash_state(user_id)
    return web.json_response({"ok": True, "game": game, "balance": await db.get_balance(user_id)})


async def api_crash_cashout(request: web.Request) -> web.Response:
    """Кассация на серверном множителе — клиентский таймер исход не определяет."""
    db: Database = request.app[DB_KEY]
    user_id = int(request[USER_KEY]["user_id"])
    game_id = _as_int((await _json_body(request)).get("game_id"))
    if game_id <= 0:
        return _bad("Партия «Самолётик» не найдена")
    try:
        result = await db.cashout_crash(user_id, game_id)
    except InvalidBet as exc:
        return _bad(str(exc))
    return web.json_response({"ok": True, "game": result, "balance": result["balance"]})


__all__ = ["DB_KEY", "STATIC_DIR", "USER_KEY", "build_app", "register_routes", "render_index"]

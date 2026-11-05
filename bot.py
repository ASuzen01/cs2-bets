"""Точка входа: long polling локально, webhook в облаке.

Локально:   cp .env.example .env && python bot.py
В облаке:   RUN_MODE=webhook + WEBHOOK_BASE_URL + WEBHOOK_SECRET
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from functools import partial
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

from app.config import settings
from app.db import Database
from app.handlers import setup_handlers
from app.handlers.errors import last_errors
from app.logging import setup_logging
from app.middlewares import ThrottlingMiddleware, UserSyncMiddleware
from app.services.backup import BackupService
from app.services.keepalive import KEEPALIVE, PING_PATH
from app.services.manual_result import apply_manual_results
from app.services.maps import repair_refunded_maps
from app.services.recheck import (
    LAST_REPORT,
    RecheckService,
)
from app.services.sync import MatchSyncer

log = logging.getLogger(__name__)

USER_COMMANDS: list[tuple[str, str]] = [
    ("start", "Открыть приложение"),
    ("help", "Правила и выплаты"),
    ("app", "Ссылка на приложение"),
]
ADMIN_COMMANDS: list[tuple[str, str]] = [
    ("results", "Подтвердить исходы"),
    ("stats", "Статистика"),
    ("grant", "Начислить монеты игроку"),
]


# ------------------------------------------------------------------ фабрики
def build_storage() -> BaseStorage:
    """Redis-хранилище FSM включается автоматически, если задан REDIS_URL."""
    redis_url = settings.redis_url
    if redis_url:
        try:
            from aiogram.fsm.storage.redis import RedisStorage

            log.info("FSM: Redis")
            return RedisStorage.from_url(redis_url)
        except ImportError:
            log.warning("REDIS_URL задан, но пакет redis не установлен — беру MemoryStorage")
    return MemoryStorage()


def create_bot() -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(
    db: Database,
    syncer: MatchSyncer,
    backup: BackupService | None = None,
    recheck: RecheckService | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=build_storage())

    # доступно во всех хендлерах как аргументы db=... и syncer=...
    dp["db"] = db
    dp["syncer"] = syncer
    if backup is not None:
        dp["backup"] = backup
    if recheck is not None:
        dp["recheck"] = recheck

    dp.update.outer_middleware(UserSyncMiddleware(db))
    dp.message.outer_middleware(
        ThrottlingMiddleware(settings.throttle_seconds, settings.admin_ids_set)
    )

    setup_handlers(dp)

    # allowed_updates передаём в startup через partial: если положить его в workflow data,
    # он конфликтует с параметром allowed_updates у start_polling
    dp.startup.register(
        partial(on_startup, allowed_updates=dp.resolve_used_update_types())
    )
    dp.shutdown.register(on_shutdown)
    return dp


# ------------------------------------------------------------------ startup
async def on_startup(
    bot: Bot,
    db: Database,
    syncer: MatchSyncer,
    allowed_updates: list[str],
    backup: BackupService | None = None,
    recheck: RecheckService | None = None,
) -> None:
    await db.connect()

    # карты, которые раньше вернули как «не сыгранные», хотя счёт раундов есть
    try:
        repaired = await repair_refunded_maps(db, bot)
        if repaired["settled"]:
            log.info(
                "Пересчитал возвращённых карт: %s, выплачено %s",
                repaired["settled"],
                repaired["paid"],
            )
    except Exception:
        log.exception("Не смог пересчитать возвращённые карты")

    # ручные результаты сыгранных матчей (Fonbet счёт после матча не отдаёт)
    try:
        applied = await apply_manual_results(db, bot)
        for line in applied:
            log.info("Ручной результат: %s", line)
    except Exception:
        log.exception("Не смог применить ручные результаты")

    # перепроверка расчётов: сразу при старте, дальше — раз в час.
    # «всё ок» приходит одной сводкой в сутки, расхождения — сразу.
    if recheck is not None:
        try:
            await recheck.run_once()
        except Exception:
            log.exception("Перепроверка расчётов не удалась")
        recheck.start()

    syncer.start()
    # сервис на бесплатном Render засыпает после 15 минут тишины — пингуем себя
    KEEPALIVE.start()
    if backup is not None:
        backup.start()

    me = await bot.get_me()
    settings.bot_username = me.username or ""   # для ссылок-входов из групповых чатов
    log.info("Запущен @%s (id %s), режим: %s", me.username, me.id, settings.run_mode)

    await _set_commands(bot)

    # вебхук: ошибка (например, неверный WEBHOOK_BASE_URL) не должна убивать сервис —
    # иначе Render считает деплой неудачным и продолжает крутить старую версию
    if not await install_webhook(bot, allowed_updates):
        global _WEBHOOK_TASK
        _WEBHOOK_TASK = asyncio.create_task(
            _webhook_watchdog(bot, allowed_updates), name="webhook-watchdog"
        )


async def on_shutdown(
    bot: Bot,
    db: Database,
    syncer: MatchSyncer,
    backup: BackupService | None = None,
    recheck: RecheckService | None = None,
) -> None:
    log.info("Остановка бота")
    if recheck is not None:
        await recheck.stop()
    await KEEPALIVE.stop()
    if backup is not None:
        await backup.stop()
    await syncer.stop()
    await db.close()
    await bot.session.close()


async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command=name, description=text) for name, text in USER_COMMANDS],
        scope=BotCommandScopeDefault(),
    )
    # постоянная кнопка «Ставки» слева в строке ввода — открывает приложение
    from app import keyboards

    url = keyboards.app_url()
    if url:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Ставки", web_app=WebAppInfo(url=url))
            )
        except TelegramAPIError as exc:
            log.warning("Не удалось поставить меню-кнопку приложения: %s", exc)
    for admin_id in settings.admin_ids_set:
        try:
            await bot.set_my_commands(
                [BotCommand(command=name, description=text) for name, text in USER_COMMANDS + ADMIN_COMMANDS],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except TelegramAPIError as exc:
            log.warning("Не удалось выставить команды админу %s: %s", admin_id, exc)


# ---------------------------------------------------------------------- run
async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=settings.drop_pending_updates)
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        polling_timeout=settings.polling_timeout,
    )


def build_web_app(
    bot: Bot,
    dp: Dispatcher,
    db: Database | None = None,
    syncer: MatchSyncer | None = None,
    recheck: RecheckService | None = None,
):
    """Собирает aiohttp-приложение: вебхук Telegram, мини-приложение, /health и /ping.

    Отдельная функция (а не всё внутри `run_webhook`), чтобы приложение можно было
    поднять в тестах и убедиться, что маршруты зарегистрированы и отвечают.
    """
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    from app.web.auth import MIDDLEWARES
    from app.web.server import register_routes

    app = web.Application(middlewares=list(MIDDLEWARES))

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
    ).register(app, path=settings.webhook_path)

    async def health(_: web.Request) -> web.Response:
        # по этим полям видно, с какими настройками реально работает сервис
        payload: dict[str, Any] = {
            "ok": True,
            "mode": "webhook",
            # какой коммит реально работает: Render подставляет RENDER_GIT_COMMIT,
            # без него видно только локальную сборку
            "commit": (os.getenv("RENDER_GIT_COMMIT") or "local")[:7],
            "sync_interval": settings.sync_interval,
            "live_betting": settings.live_betting,
            "football": settings.football_include,
            "hockey": settings.hockey_include if settings.hockey_enabled else None,
            "backup_interval_hours": settings.backup_interval_hours,
            # последняя ошибка обработчика — чтобы чинить поломки без логов Render
            "last_error": last_errors(1),
            "sync": {
                "last_at": syncer.last_sync_at if syncer else None,
                "last_error": syncer.last_error if syncer else None,
                "stats": syncer.last_stats if syncer else None,
            },
            # последняя перепроверка расчётов и её расписание
            "recheck": dict(LAST_REPORT) if LAST_REPORT else None,
            "recheck_loop": recheck.stats if recheck is not None else None,
            # держим сервис проснувшимся: когда был последний успешный пинг
            "keepalive": KEEPALIVE.stats,
            # встал ли вебхук: видно, если Telegram не принял адрес
            "webhook": dict(WEBHOOK_STATE),
        }
        # состояние линии и бэкапов — без авторизации, но и без личных данных
        if db is not None:
            try:
                payload["line"] = {
                    "cs2": len(await db.open_matches(limit=500, sport="cs2")),
                    "football": len(await db.open_matches(limit=500, sport="football")),
                    "hockey": len(await db.open_matches(limit=500, sport="hockey")),
                    "live": len(await db.live_matches(limit=500)),
                }
                payload["matches"] = {
                    "football": await db.status_counts("football"),
                    "hockey": await db.status_counts("hockey"),
                    "cs2": await db.status_counts("cs2"),
                }
                payload["started_open"] = {
                    "football": await db.started_open_count("football"),
                    "hockey": await db.started_open_count("hockey"),
                    "cs2": await db.started_open_count("cs2"),
                }
                # пара матчей для разбора: что висит и с каким счётом
                payload["sample"] = {
                    sport: [
                        {
                            "id": int(row["id"]),
                            "teams": f"{row['team_a']} — {row['team_b']}",
                            "tour": str(row["tournament"])[-24:],
                            "status": str(row["status"]),
                            "started_min_ago": int((time.time() - int(row["start_time"])) // 60),
                            "score": f"{row['score_a']}:{row['score_b']}",
                        }
                        for row in await db.sample_matches(
                            sport, ("live", "awaiting", "open")
                        )
                    ]
                    for sport in ("football", "hockey", "cs2")
                }
                last = await db.last_backup()
                payload["last_backup_at"] = int(last["created_at"]) if last else None
                payload["db"] = "ok"
                raw = await db.get_meta("last_sync_at")
                sync_at = int(raw) if raw else 0
                age = int(time.time()) - sync_at if sync_at else None
                payload["last_sync_age_seconds"] = age
                payload["feed_stale"] = bool(age is None or age > max(900, settings.sync_interval * 20))
            except Exception:
                log.debug("health: не смог прочитать состояние линии", exc_info=True)
                payload["db"] = "error"
        return web.json_response(payload)

    app.router.add_get("/health", health)

    async def metrics_view(_: web.Request) -> web.Response:
        """Счётчики процесса: тот же /metrics, что и у мини-аппа."""
        from app.web.limits import metrics

        return web.Response(text=metrics.text(), content_type="text/plain", charset="utf-8")

    app.router.add_get("/metrics", metrics_view)

    async def ping(_: web.Request) -> web.Response:
        """Лёгкий ответ без базы: его дёргает keep-alive и внешний пингер."""
        return web.json_response({"ok": True, "at": int(time.time())})

    app.router.add_get(PING_PATH, ping)

    # мини-приложение в том же процессе: один адрес, одна база
    if db is not None:
        register_routes(app, db, serve_health=False, index_path="/")
        from app.web.auth import BOT_KEY

        app[BOT_KEY] = bot      # купон ставки уходит игроку в чат через бота
        if settings.admin_ids_list:
            from app.web.auth import ALERT_KEY

            async def alert_admin(text: str) -> None:
                """500-е в мини-аппе — админу в личку (не чаще раза в пять минут)."""
                await bot.send_message(settings.admin_ids_list[0], text)

            app[ALERT_KEY] = alert_admin
        log.info("Мини-приложение доступно по адресу %s/", settings.webhook_base_url or "/")

    setup_application(app, dp, bot=bot)
    return app


def run_webhook(
    bot: Bot,
    dp: Dispatcher,
    db: Database | None = None,
    syncer: MatchSyncer | None = None,
    recheck: RecheckService | None = None,
) -> None:
    from aiohttp import web

    app = build_web_app(bot, dp, db, syncer, recheck)
    log.info("HTTP-сервер: %s:%s%s", settings.host, settings.port, settings.webhook_path)
    web.run_app(app, host=settings.host, port=settings.port, print=None)


_lock_handle = None

# Поставился ли вебхук: если Telegram отверг адрес, сервис всё равно живёт, а
# состояние видно в /health — иначе ошибка конфигурации роняет весь деплой.
WEBHOOK_STATE: dict[str, Any] = {"ok": None, "url": "", "error": None, "set_at": None}
_WEBHOOK_TASK: asyncio.Task[None] | None = None


async def install_webhook(bot: Bot, allowed_updates: list[str]) -> bool:
    """Ставит вебхук. Не падает: при ошибке сервис продолжает работать."""
    if settings.run_mode != "webhook":
        return True
    try:
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.webhook_secret,
            drop_pending_updates=settings.drop_pending_updates,
            allowed_updates=allowed_updates,
        )
    except Exception as exc:
        WEBHOOK_STATE.update(
            {"ok": False, "url": settings.webhook_url, "error": str(exc)[:300], "set_at": None}
        )
        log.error(
            "Вебхук %s не поставился (%s). Сервис работает, повторяю каждые 5 минут.",
            settings.webhook_url,
            exc,
        )
        return False
    WEBHOOK_STATE.update(
        {"ok": True, "url": settings.webhook_url, "error": None, "set_at": int(time.time())}
    )
    log.info("Webhook установлен: %s", settings.webhook_url)
    return True


async def _webhook_watchdog(bot: Bot, allowed_updates: list[str], delay: int = 300) -> None:
    """Повторяет установку вебхука, пока Telegram не примет адрес."""
    while True:
        await asyncio.sleep(delay)
        if await install_webhook(bot, allowed_updates):
            return


def acquire_single_instance_lock() -> None:
    """Не даём запустить второй polling: Telegram отдаёт 409 и бот «молчит»."""
    global _lock_handle
    lock_path = Path(settings.db_file).parent / "bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)

    handle = lock_path.open("r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error(
            "Уже запущен другой экземпляр бота (%s). Второй polling получит 409 — выходим.",
            lock_path,
        )
        raise SystemExit(2) from None

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle


def main() -> None:
    setup_logging()
    settings.validate_for_run()
    acquire_single_instance_lock()

    bot = create_bot()
    db = Database(settings.db_file, start_balance=settings.start_balance, url=settings.database_url, schema=settings.database_schema)
    syncer = MatchSyncer(bot, db)
    backup = BackupService(bot, db)
    recheck = RecheckService(bot, db)
    dp = create_dispatcher(db, syncer, backup, recheck)

    if settings.run_mode == "webhook":
        run_webhook(bot, dp, db, syncer, recheck)
    else:
        asyncio.run(run_polling(bot, dp))


if __name__ == "__main__":
    main()

"""Мини-приложение CS2 BETS (Telegram WebApp) — сервер.

Запуск:  python miniapp.py
Порт:    MINIAPP_PORT (по умолчанию 8081), слушает 0.0.0.0

Открывается кнопкой «📱 Приложение» в меню бота (нужен MINIAPP_URL с https).
Для локальной отладки в браузере: MINIAPP_DEV_USER=<telegram_id> python miniapp.py
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from app.config import settings
from app.db import Database
from app.web.server import build_app


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


async def main() -> None:
    setup_logging()
    log = logging.getLogger("miniapp")

    db = Database(settings.db_file, start_balance=settings.start_balance, url=settings.database_url, schema=settings.database_schema)
    await db.connect()

    app = build_app(db)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.miniapp_port)
    await site.start()

    mode = "Telegram initData + dev-режим" if settings.miniapp_dev_user else "Telegram initData"
    log.info(
        "Мини-приложение на http://%s:%s (авторизация: %s)", settings.host, settings.miniapp_port, mode
    )

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

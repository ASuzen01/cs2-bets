from __future__ import annotations

from aiogram import Dispatcher

from app.handlers import admin, common, misc
from app.handlers.errors import on_error


def setup_handlers(dp: Dispatcher) -> None:
    # порядок важен: админские команды → вход с кнопкой приложения → фолбэк
    dp.include_router(admin.router)
    dp.include_router(common.router)
    dp.include_router(misc.router)
    dp.errors.register(on_error)


__all__ = ["setup_handlers", "admin", "common", "misc", "on_error"]

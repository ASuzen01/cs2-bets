from __future__ import annotations

import logging

from aiogram.types import CallbackQuery, ErrorEvent, Message

from app import texts

log = logging.getLogger(__name__)


async def on_error(event: ErrorEvent) -> None:
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

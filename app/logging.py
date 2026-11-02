from __future__ import annotations

import logging

from app.config import settings


def setup_logging() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    # INFO — в логах видно каждый обработанный апдейт (удобно при деплое)
    logging.getLogger("aiogram.event").setLevel(logging.INFO)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

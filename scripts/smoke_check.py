"""Быстрая проверка сборки: диспетчер, роутеры, БД, тексты.

Запуск:  python scripts/smoke_check.py
Не требует токена и не обращается к Telegram API.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.texts import match_card  # noqa: E402


async def main() -> None:
    settings.bot_token = "123456:SMOKE-TEST"

    from bot import create_bot, create_dispatcher  # импорт после подстановки токена
    from app.services.sync import MatchSyncer

    bot = create_bot()
    db = Database(Path("data/_smoke.db"), start_balance=settings.start_balance)
    dp = create_dispatcher(db, MatchSyncer(bot, db))

    routers = [r.name for r in dp.sub_routers]
    assert {"admin", "common", "ui"} <= set(routers), routers

    await db.connect()
    await db.upsert_user(42, "smoke", "Smoke Test")
    match, _ = await db.upsert_match(
        fonbet_id=1,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=1793671200,
        odds_a=1.65,
        odds_b=2.25,
    )
    balance, odds = await db.place_bet(int(match["id"]), 42, -100, "a", 100)
    assert balance == 900 and odds == 1.65
    card = match_card(match)
    assert "Aurora" in card and "1.65" in card

    await db.close()
    await bot.session.close()

    Path("data/_smoke.db").unlink(missing_ok=True)
    Path("data/_smoke.db-wal").unlink(missing_ok=True)
    Path("data/_smoke.db-shm").unlink(missing_ok=True)

    print("OK: роутеры:", ", ".join(routers))
    print("OK: обновления:", ", ".join(dp.resolve_used_update_types()))
    print("OK: карточка матча и БД работают")


if __name__ == "__main__":
    asyncio.run(main())

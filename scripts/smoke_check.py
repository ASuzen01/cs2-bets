"""Быстрая проверка сборки: диспетчер, роутеры, БД, тексты.

Запуск:  python scripts/smoke_check.py
Не требует токена и не обращается к Telegram API.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Database
from app.texts import match_card


async def main() -> None:
    settings.bot_token = "123456:SMOKE-TEST"

    from app.services.sync import MatchSyncer
    from bot import create_bot, create_dispatcher  # импорт после подстановки токена

    smoke_path = Path("data/_smoke.db")
    bot = create_bot()
    db = Database(smoke_path, start_balance=settings.start_balance)
    try:
        dp = create_dispatcher(db, MatchSyncer(bot, db))
        routers = [r.name for r in dp.sub_routers]
        assert {"admin", "common", "misc"} <= set(routers), routers

        await db.connect()
        await db.upsert_user(42, "smoke", "Smoke Test")
        match, _ = await db.upsert_match(
            fonbet_id=1,
            tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
            team_a="Aurora",
            team_b="G2 Esports",
            start_time=int(time.time()) + 3600,
            odds_a=1.65,
            odds_b=2.25,
        )
        balance, odds = await db.place_bet(int(match["id"]), 42, -100, "a", 100)
        assert balance == 900 and odds == 1.65
        card = match_card(match)
        assert "Aurora" in card and "1.65" in card
        updates = dp.resolve_used_update_types()
    finally:
        await db.close()
        await bot.session.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{smoke_path}{suffix}").unlink(missing_ok=True)

    print("OK: роутеры:", ", ".join(routers))
    print("OK: обновления:", ", ".join(updates))
    print("OK: карточка матча и БД работают")


if __name__ == "__main__":
    asyncio.run(main())

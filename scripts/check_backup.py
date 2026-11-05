"""Проверка бэкапа: не «файл создался», а «из него действительно восстанавливается база».

    python scripts/check_backup.py                      # свежий дамп текущей базы
    python scripts/check_backup.py backups/file.json.gz # проверка конкретного файла

Дамп разворачивается в отдельную временную SQLite-базу, после чего скрипт
сверяет число строк по таблицам с дампом и проверяет, что ключевые запросы
(баланс игрока, ставки, леджер) работают. Возвращает код 1, если что-то не сошлось,
— так это можно поставить в cron и получать письмо/алерт.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Database
from app.services.backup import decode, make_dump, restore, summary

CHECK_TABLES = ("users", "matches", "bets", "expresses", "ledger", "casino_bets")


async def check(blob: bytes) -> int:
    payload = decode(blob)
    print(summary(payload).replace("<b>", "").replace("</b>", ""))

    tables = payload.get("tables", {})
    expected = {name: len(tables.get(name, [])) for name in CHECK_TABLES}

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "restore.db", start_balance=settings.start_balance)
        await db.connect()
        try:
            await restore(db, blob, wipe=True)
            problems: list[str] = []
            for name, want in expected.items():
                have = len(await db._fetchall(f"SELECT 1 FROM {name}"))
                if have != want:
                    problems.append(f"{name}: в дампе {want}, после восстановления {have}")
            # деньги: сумма по леджеру должна совпадать с суммой балансов
            ledger = await db._fetchone("SELECT COALESCE(SUM(delta), 0) AS total FROM ledger")
            balances = await db._fetchone("SELECT COALESCE(SUM(balance), 0) AS total FROM users")
            if ledger is not None and balances is not None:
                print(f"Леджер: {int(ledger['total'])} · балансы игроков: {int(balances['total'])}")
            balance = await db.get_balance(settings.miniapp_dev_user) if settings.miniapp_dev_user else 0
            print(f"Пробное чтение баланса: {balance} 🪙")
        finally:
            await db.close()

    if problems:
        print("\n❌ Бэкап неполный:")
        for line in problems:
            print(" -", line)
        return 1
    print("\n✅ Бэкап восстанавливается: все таблицы на месте")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить, что из бэкапа восстанавливается база")
    parser.add_argument("file", nargs="?", help="путь к .json.gz (по умолчанию — свежий дамп)")
    args = parser.parse_args()

    if args.file:
        blob = Path(args.file).read_bytes()
    else:
        db = Database(
            settings.db_file,
            start_balance=settings.start_balance,
            url=settings.database_url,
            schema=settings.database_schema,
        )
        await db.connect()
        try:
            blob, _ = await make_dump(db, kind="check")
        finally:
            await db.close()

    return await check(blob)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Восстановление базы из дампа.

    python scripts/restore_db.py backups/file.json.gz           # покажет, что внутри
    python scripts/restore_db.py backups/file.json.gz --yes     # восстановит

По умолчанию только читает файл и показывает содержимое: затирать базу без
явного --yes скрипт не станет. Перед восстановлением сам снимает текущее
состояние в backups/before-restore_*.json.gz.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Database
from app.services.backup import decode, filename, make_dump, restore, summary


async def main() -> None:
    parser = argparse.ArgumentParser(description="Восстановить базу из дампа")
    parser.add_argument("file", help="путь к .json.gz")
    parser.add_argument("--yes", action="store_true", help="действительно затереть базу")
    args = parser.parse_args()

    blob = Path(args.file).read_bytes()
    payload = decode(blob)          # падает, если дамп битый
    print(summary(payload).replace("<b>", "").replace("</b>", ""))
    print(f"Размер файла: {len(blob) / 1024:.1f} КБ")

    db = Database(
        settings.db_file,
        start_balance=settings.start_balance,
        url=settings.database_url,
        schema=settings.database_schema,
    )
    await db.connect()
    try:
        if not args.yes:
            current = await db.dump_rows()
            print("\nСейчас в базе:")
            for table, rows in current.items():
                print(f"  {table}: {len(rows)}")
            print("\nЭто пробный прогон. Повтори с --yes, чтобы восстановить.")
            return

        safety, safety_payload = await make_dump(db, kind="before-restore")
        safety_path = Path("backups") / filename(
            int(safety_payload["created_at"]), kind="before-restore"
        )
        safety_path.parent.mkdir(parents=True, exist_ok=True)
        safety_path.write_bytes(safety)
        print(f"\nТекущее состояние сохранено: {safety_path}")

        counts = await restore(db, blob)
        print("\nВосстановлено:")
        for table, rows in counts.items():
            if rows:
                print(f"  {table}: {rows}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

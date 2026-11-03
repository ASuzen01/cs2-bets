"""Ручной бэкап базы: снимает дамп в файл рядом со скриптом.

    python scripts/backup_db.py                  # в ./backups/
    python scripts/backup_db.py --out /tmp/b.gz  # в конкретный файл

Дамп читается и на SQLite, и на Postgres — восстанавливать можно куда угодно.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Database
from app.services.backup import filename, make_dump, summary


async def main() -> None:
    parser = argparse.ArgumentParser(description="Снять дамп базы")
    parser.add_argument("--out", help="куда положить файл")
    args = parser.parse_args()

    db = Database(
        settings.db_file,
        start_balance=settings.start_balance,
        url=settings.database_url,
        schema=settings.database_schema,
    )
    await db.connect()
    try:
        blob, payload = await make_dump(db, kind="cli")
    finally:
        await db.close()

    target = Path(args.out) if args.out else Path("backups") / filename(int(payload["created_at"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    print(summary(payload).replace("<b>", "").replace("</b>", ""))
    print(f"Файл: {target} ({len(blob) / 1024:.1f} КБ)")


if __name__ == "__main__":
    asyncio.run(main())

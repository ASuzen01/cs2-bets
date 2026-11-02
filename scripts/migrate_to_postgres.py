"""Переносит игроков, чаты и историю из локального SQLite в Postgres (Neon).

Ставки и экспрессы не переносятся: они живут вместе с матчами, а матчи на новом
хостинге заново придут из линии Fonbet. Балансы и история операций сохраняются.

    DATABASE_URL='postgresql://...neon.tech/neondb?sslmode=require' \
        python scripts/migrate_to_postgres.py

Флаг --dry-run показывает, что будет перенесено, ничего не записывая.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Database


def read_sqlite(path: Path) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
    if not path.exists():
        raise SystemExit(f"Нет файла базы: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    users = conn.execute("SELECT * FROM users").fetchall()
    chats = conn.execute("SELECT * FROM chats").fetchall()
    ledger = conn.execute("SELECT * FROM ledger ORDER BY id").fetchall()
    conn.close()
    return users, chats, ledger


async def migrate(dsn: str, source: Path, dry_run: bool) -> None:
    users, chats, ledger = read_sqlite(source)
    print(f"Из {source}: игроков {len(users)}, чатов {len(chats)}, операций {len(ledger)}")

    if dry_run:
        for row in users[:10]:
            print(f"  • {row['user_id']} {row['full_name'] or row['username'] or ''} — {row['balance']} 🪙")
        if len(users) > 10:
            print(f"  … и ещё {len(users) - 10}")
        print("Dry-run: ничего не записано.")
        return

    db = Database(url=dsn, start_balance=settings.start_balance)
    await db.connect()
    try:
        for row in users:
            await db.conn.execute(
                "INSERT INTO users (user_id, username, full_name, balance, bonus_at, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
                "full_name = excluded.full_name, balance = excluded.balance, "
                "bonus_at = excluded.bonus_at, last_seen_at = excluded.last_seen_at",
                (
                    int(row["user_id"]),
                    row["username"],
                    row["full_name"],
                    int(row["balance"]),
                    int(row["bonus_at"]),
                    int(row["created_at"]),
                    int(row["last_seen_at"]),
                ),
            )
        await db.conn.commit()

        for row in chats:
            await db.conn.execute(
                "INSERT INTO chats (chat_id, title, autopost, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, autopost = excluded.autopost",
                (int(row["chat_id"]), row["title"], int(row["autopost"]), int(row["added_at"])),
            )
        await db.conn.commit()

        for row in ledger:
            await db.conn.execute(
                "INSERT INTO ledger (user_id, delta, reason, created_at) VALUES (?, ?, ?, ?)",
                (int(row["user_id"]), int(row["delta"]), row["reason"], int(row["created_at"])),
            )
        await db.conn.commit()
        print(f"Готово: {len(users)} игроков, {len(chats)} чатов, {len(ledger)} операций перенесено.")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Перенос данных из SQLite в Postgres")
    parser.add_argument("--source", default=str(settings.db_file), help="путь к SQLite-базе бота")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет перенесено")
    args = parser.parse_args()

    dsn = settings.database_url
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise SystemExit("Задайте DATABASE_URL (строка подключения Neon) перед запуском")
    asyncio.run(migrate(dsn, Path(args.source), args.dry_run))


if __name__ == "__main__":
    main()

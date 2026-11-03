"""id чатов и игроков: супергруппы не влезают в Postgres-ный int4.

У супергрупп chat_id вида -1001234567890 — это больше 2^31. Пока колонка была
INTEGER, запись падала с «NumericValueOutOfRange: integer out of range», и любая
команда бота в группе отвечала «Что-то сломалось на моей стороне».
"""

from __future__ import annotations

import pytest

from app.db import Database
from app.db.database import WIDE_COLUMNS

SUPERGROUP = -1001234567890


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "chat.db", start_balance=1000)
    await database.connect()
    yield database
    await database.close()


def test_chat_columns_are_wide() -> None:
    assert ("users", "last_chat_id") in WIDE_COLUMNS
    assert ("chats", "chat_id") in WIDE_COLUMNS
    assert ("bets", "chat_id") in WIDE_COLUMNS


async def test_remember_chat_keeps_supergroup_id(db: Database) -> None:
    await db.upsert_user(1, "sap", "Антон")
    await db.remember_chat(1, SUPERGROUP)

    row = await db.get_user(1)
    assert int(row["last_chat_id"]) == SUPERGROUP


async def test_register_chat_keeps_supergroup_id(db: Database) -> None:
    await db.register_chat(SUPERGROUP, "Общий чат")

    chats = await db.list_autopost_chats()
    assert [int(chat["chat_id"]) for chat in chats] == [SUPERGROUP]


async def test_private_chat_is_not_remembered(db: Database) -> None:
    await db.upsert_user(2, "sap", "Антон")
    await db.remember_chat(2, 500)          # личка — писать и так есть куда

    row = await db.get_user(2)
    assert row["last_chat_id"] in (None, 0)

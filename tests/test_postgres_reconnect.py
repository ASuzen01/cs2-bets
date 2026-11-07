from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.db.backends import PostgresBackend


class FakeCursor:
    description = None
    rowcount = 1

    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.calls.append((sql, params))
        if self.conn.fail_next:
            self.conn.fail_next = False
            self.conn.closed = True
            raise RuntimeError("connection closed during statement")

    async def fetchall(self) -> list[Any]:
        return []

    async def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self, *, closed: bool = False, fail_next: bool = False) -> None:
        self.closed = closed
        self.fail_next = fail_next
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    async def commit(self) -> None:
        if self.closed:
            raise RuntimeError("connection closed")

    async def rollback(self) -> None:
        if self.closed:
            raise RuntimeError("connection closed")


@pytest.mark.asyncio
async def test_closed_connection_reopens_before_next_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = FakeConnection(closed=True)
    live = FakeConnection()
    opened: list[tuple[str, str]] = []

    async def fake_open(dsn: str, schema: str) -> FakeConnection:
        opened.append((dsn, schema))
        return live

    monkeypatch.setattr(PostgresBackend, "_open_connection", staticmethod(fake_open))
    backend = PostgresBackend(stale, dsn="postgres://test", schema="bot_data")

    result = await backend.execute("UPDATE users SET balance = ?", (12,))

    assert opened == [("postgres://test", "bot_data")]
    assert live.cursors[0].calls == [("UPDATE users SET balance = %s", (12,))]
    assert result.rowcount == 1
    assert backend._reconnects == 1


@pytest.mark.asyncio
async def test_failed_write_is_not_replayed_but_next_query_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeConnection(fail_next=True)
    second = FakeConnection()
    opened = 0

    async def fake_open(dsn: str, schema: str) -> FakeConnection:
        nonlocal opened
        opened += 1
        return second

    monkeypatch.setattr(PostgresBackend, "_open_connection", staticmethod(fake_open))
    backend = PostgresBackend(first, dsn="postgres://test")

    with pytest.raises(RuntimeError, match="connection closed during statement"):
        await backend.execute("UPDATE ledger SET amount = ?", (5,))

    # The failed write was attempted only once; only the next operation reconnects.
    assert len(first.cursors) == 1
    assert first.cursors[0].calls == [("UPDATE ledger SET amount = %s", (5,))]
    assert opened == 0

    await backend.execute("SELECT 1")

    assert opened == 1
    assert second.cursors[0].calls == [("SELECT 1", ())]
    assert backend._reconnects == 1


@pytest.mark.asyncio
async def test_lost_transaction_is_not_reconnected_or_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeConnection()
    second = FakeConnection()
    opened = 0

    async def fake_open(dsn: str, schema: str) -> FakeConnection:
        nonlocal opened
        opened += 1
        return second

    monkeypatch.setattr(PostgresBackend, "_open_connection", staticmethod(fake_open))
    backend = PostgresBackend(first, dsn="postgres://test")

    await backend.execute("BEGIN")
    first.closed = True
    with pytest.raises(RuntimeError, match="during a transaction"):
        await backend.execute("UPDATE users SET balance = ?", (9,))

    assert opened == 0
    assert len(first.cursors) == 1  # only BEGIN reached the old connection
    with pytest.raises(RuntimeError, match="connection closed"):
        await backend.rollback()
    assert backend._transaction_active is False

    await backend.execute("SELECT 1")
    assert opened == 1
    assert second.cursors[0].calls == [("SELECT 1", ())]


@pytest.mark.asyncio
async def test_concurrent_reconnect_attempts_share_one_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = FakeConnection(closed=True)
    live = FakeConnection()
    opened = 0

    async def fake_open(dsn: str, schema: str) -> FakeConnection:
        nonlocal opened
        opened += 1
        await asyncio.sleep(0)
        return live

    monkeypatch.setattr(PostgresBackend, "_open_connection", staticmethod(fake_open))
    backend = PostgresBackend(stale, dsn="postgres://test")

    await asyncio.gather(backend._ensure_connected(), backend._ensure_connected())

    assert opened == 1
    assert backend._conn is live

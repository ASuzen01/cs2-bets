from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from app.db.backends import SQLiteBackend
from app.services.telegram_sqlite import SNAPSHOT_MARKER, TelegramSQLiteSnapshot


def _sqlite_blob(path: Path) -> bytes:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY);"
            "CREATE TABLE matches (id INTEGER PRIMARY KEY);"
            "CREATE TABLE ledger (id INTEGER PRIMARY KEY);"
            "INSERT INTO users VALUES (1);"
        )
    finally:
        conn.close()
    return gzip.compress(path.read_bytes(), mtime=0)


class FakeBot:
    def __init__(self, compressed: bytes) -> None:
        self.compressed = compressed
        self.edited_media = None
        self.chat = None

    async def get_chat(self, chat_id: int):
        return self.chat

    async def get_me(self):
        return SimpleNamespace(id=99)

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(
            status="administrator", can_post_messages=True, can_edit_messages=True
        )

    async def get_file(self, file_id: str):
        return SimpleNamespace(file_path="snapshot.gz", file_size=len(self.compressed))

    async def download_file(self, file_path: str, destination: io.BytesIO):
        destination.write(self.compressed)
        return destination

    async def edit_message_media(self, **kwargs):
        self.edited_media = kwargs["media"]
        return SimpleNamespace(message_id=kwargs["message_id"])


def _fake_chat(compressed: bytes):
    sha = hashlib.sha256(compressed).hexdigest()
    message = SimpleNamespace(
        message_id=7,
        caption=f"{SNAPSHOT_MARKER}\nSHA-256: {sha}",
        document=SimpleNamespace(file_id="file-id"),
    )
    return SimpleNamespace(type="channel", pinned_message=message)


@pytest.mark.asyncio
async def test_restore_downloads_validates_and_atomically_writes_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    compressed = _sqlite_blob(source)
    bot = FakeBot(compressed)
    bot.chat = _fake_chat(compressed)
    store = TelegramSQLiteSnapshot(bot, -1001234567890)
    target = tmp_path / "data" / "bot.db"

    await store.restore_to_path(target)

    conn = sqlite3.connect(target)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    finally:
        conn.close()
    assert store._message_id == 7


@pytest.mark.asyncio
async def test_commit_snapshot_edits_existing_pinned_document(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    compressed = _sqlite_blob(source)
    bot = FakeBot(compressed)
    bot.chat = _fake_chat(compressed)
    store = TelegramSQLiteSnapshot(bot, -1001234567890)
    local = tmp_path / "local.sqlite"
    await store.restore_to_path(local)

    conn = await aiosqlite.connect(local)
    try:
        await conn.execute("INSERT INTO users VALUES (2)")
        await conn.commit()
        await store.persist_connection(conn)
    finally:
        await conn.close()

    media = bot.edited_media
    assert media is not None
    assert media.caption.startswith(SNAPSHOT_MARKER)
    updated = gzip.decompress(media.media.data)
    with sqlite3.connect(":memory:") as verify:
        verify.deserialize(updated)
        assert verify.execute("SELECT count(*) FROM users").fetchone()[0] == 2
        assert verify.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.asyncio
async def test_live_database_snapshot_uses_separate_connection(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    compressed = _sqlite_blob(source)
    bot = FakeBot(compressed)
    bot.chat = _fake_chat(compressed)
    store = TelegramSQLiteSnapshot(bot, -1001234567890)
    local = tmp_path / "live.sqlite"
    await store.restore_to_path(local)
    backend = await SQLiteBackend.open(local)

    async def forbidden_connection_backup(*_args, **_kwargs):
        raise AssertionError("snapshot must not occupy the app SQLite worker")

    backend._conn.backup = forbidden_connection_backup
    try:
        await backend.execute("INSERT INTO users VALUES (2)")
        await backend.commit()
        await store.persist_connection(backend._conn)
        rows = await backend.execute("SELECT count(*) AS n FROM users")
        assert (await rows.fetchone())["n"] == 2

        updated_path = tmp_path / "updated.sqlite"
        updated_path.write_bytes(gzip.decompress(bot.edited_media.media.data))
        with sqlite3.connect(updated_path) as verify:
            assert verify.execute("SELECT count(*) FROM users").fetchone()[0] == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_commit_does_not_wait_for_telegram_and_coalesces_pending_snapshots(tmp_path: Path) -> None:
    backend = await SQLiteBackend.open(tmp_path / "async-backup.db")
    await backend.executescript("CREATE TABLE values_table (value INTEGER)")
    store = TelegramSQLiteSnapshot(FakeBot(b""), -1001234567890)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_persist(_connection) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    store.persist_connection = slow_persist
    backend.set_commit_hook(store.schedule_persist_connection)
    try:
        await backend.execute("INSERT INTO values_table VALUES (1)")
        # This commit must return even though the remote snapshot is held open.
        await asyncio.wait_for(backend.commit(), timeout=0.2)
        await asyncio.wait_for(entered.wait(), timeout=1)
        await store.schedule_persist_connection(backend._conn)
        await store.schedule_persist_connection(backend._conn)
        release.set()
        await store.flush_pending()
        assert calls == 2  # one in-flight snapshot plus one coalesced latest copy
    finally:
        release.set()
        await store.flush_pending()
        await backend.close()


@pytest.mark.asyncio
async def test_restore_refuses_unmarked_pinned_document(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    compressed = _sqlite_blob(source)
    bot = FakeBot(compressed)
    chat = _fake_chat(compressed)
    chat.pinned_message.caption = "an unrelated document"
    bot.chat = chat
    store = TelegramSQLiteSnapshot(bot, -1001234567890)

    with pytest.raises(RuntimeError, match="not our SQLite snapshot"):
        await store.restore_to_path(tmp_path / "bot.db")


@pytest.mark.asyncio
async def test_failed_snapshot_blocks_next_query_until_durability_retry(tmp_path: Path) -> None:
    backend = await SQLiteBackend.open(tmp_path / "backend.db")
    await backend.executescript("CREATE TABLE values_table (value INTEGER)")
    attempts = 0

    async def hook(_connection) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary Telegram failure")

    backend.set_commit_hook(hook)
    try:
        await backend.execute("INSERT INTO values_table VALUES (1)")
        with pytest.raises(RuntimeError, match="temporary Telegram failure"):
            await backend.commit()
        assert backend._durability_dirty is True

        rows = await backend.execute("SELECT value FROM values_table")
        assert (await rows.fetchone())["value"] == 1
        assert backend._durability_dirty is False
        assert attempts == 2
    finally:
        await backend.close()

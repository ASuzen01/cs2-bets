"""Durable SQLite snapshots stored as one pinned Telegram channel document.

Telegram is not a SQL server: SQLite remains the query engine, while the pinned
compressed database file is the durable source restored on process startup.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaDocument

log = logging.getLogger(__name__)

SNAPSHOT_MARKER = "TELEGRAM_SQLITE_SNAPSHOT:v1"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Telegram Bot API getFile limit


class TelegramSnapshotError(RuntimeError):
    """The pinned Telegram snapshot could not safely be read or updated."""


class TelegramSQLiteSnapshot:
    def __init__(self, bot: Bot, chat_id: int) -> None:
        if not chat_id:
            raise ValueError("TELEGRAM_DB_CHAT_ID must be a non-zero channel id")
        self.bot = bot
        self.chat_id = int(chat_id)
        self._message_id: int | None = None
        self._write_lock = asyncio.Lock()
        self._last_sha256: str | None = None

    async def _validate_channel(self) -> Any:
        chat = await self.bot.get_chat(self.chat_id)
        if chat.type != "channel":
            raise TelegramSnapshotError("TELEGRAM_DB_CHAT_ID must identify a channel")
        me = await self.bot.get_me()
        member = await self.bot.get_chat_member(self.chat_id, me.id)
        if member.status != "administrator":
            raise TelegramSnapshotError("Bot must be a channel administrator")
        if not member.can_post_messages or not member.can_edit_messages:
            raise TelegramSnapshotError(
                "Bot needs Post Messages and Edit Messages permissions in the channel"
            )
        return chat

    @staticmethod
    def _validate_sqlite(raw: bytes) -> None:
        if not raw.startswith(b"SQLite format 3\x00"):
            raise TelegramSnapshotError("Pinned file does not contain a SQLite database")
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
            tmp.write(raw)
            tmp.flush()
            try:
                conn = sqlite3.connect(tmp.name)
                try:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0] != "ok":
                        raise TelegramSnapshotError("SQLite integrity_check failed")
                    tables = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    required = {"users", "matches", "ledger"}
                    if not required.issubset(tables):
                        raise TelegramSnapshotError(
                            "SQLite snapshot is missing required application tables"
                        )
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                raise TelegramSnapshotError("Pinned SQLite snapshot cannot be opened") from exc

    async def restore_to_path(self, path: str | Path) -> None:
        """Download and validate the pinned snapshot before SQLite opens its file."""
        chat = await self._validate_channel()
        message = chat.pinned_message
        if message is None or message.document is None:
            raise TelegramSnapshotError("Channel has no pinned SQLite snapshot document")
        caption = message.caption or ""
        if SNAPSHOT_MARKER not in caption:
            raise TelegramSnapshotError("Pinned channel message is not our SQLite snapshot")
        self._message_id = message.message_id

        file_info = await self.bot.get_file(message.document.file_id)
        if file_info.file_size is not None and file_info.file_size > MAX_DOWNLOAD_BYTES:
            raise TelegramSnapshotError("Pinned snapshot exceeds Telegram's 20 MB download limit")
        destination = io.BytesIO()
        await self.bot.download_file(file_info.file_path, destination=destination)
        compressed = destination.getvalue()
        if len(compressed) > MAX_DOWNLOAD_BYTES:
            raise TelegramSnapshotError("Downloaded snapshot exceeds Telegram's 20 MB limit")
        expected_sha = self._caption_sha(caption)
        digest = hashlib.sha256(compressed).hexdigest()
        if expected_sha and digest != expected_sha:
            raise TelegramSnapshotError("Pinned snapshot SHA-256 does not match its caption")
        try:
            raw = gzip.decompress(compressed)
        except (OSError, EOFError) as exc:
            raise TelegramSnapshotError("Pinned snapshot is not a valid gzip file") from exc
        self._validate_sqlite(raw)

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as tmp:
            tmp.write(raw)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)
        try:
            # A previous process may have left WAL sidecars beside the local cache.
            Path(f"{target}-wal").unlink(missing_ok=True)
            Path(f"{target}-shm").unlink(missing_ok=True)
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
        self._last_sha256 = digest
        log.info("Restored SQLite snapshot from Telegram channel %s", self.chat_id)

    async def persist_connection(self, connection: Any) -> None:
        """Create a consistent SQLite backup and replace the pinned document."""
        async with self._write_lock:
            if self._message_id is None:
                raise TelegramSnapshotError("Pinned snapshot was not loaded before database writes")
            # Serialize edits to the single channel post; Telegram itself supplies
            # retry_after when its per-chat rate limit is reached.
            with tempfile.TemporaryDirectory(prefix="telegram-sqlite-") as td:
                db_path = Path(td) / "snapshot.sqlite"
                target = sqlite3.connect(db_path)
                try:
                    await connection.backup(target)
                finally:
                    target.close()
                raw = db_path.read_bytes()
            self._validate_sqlite(raw)
            compressed = gzip.compress(raw, compresslevel=6, mtime=0)
            if len(compressed) > MAX_DOWNLOAD_BYTES:
                raise TelegramSnapshotError("Compressed SQLite snapshot exceeds 20 MB")
            digest = hashlib.sha256(compressed).hexdigest()
            if digest == self._last_sha256:
                return
            caption = (
                f"{SNAPSHOT_MARKER}\nUpdated UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}\n"
                f"SHA-256: {digest}"
            )
            media = InputMediaDocument(
                media=BufferedInputFile(compressed, filename="dobraya-ludka.sqlite.gz"),
                caption=caption,
            )
            for attempt in range(4):
                try:
                    await self.bot.edit_message_media(
                        chat_id=self.chat_id,
                        message_id=self._message_id,
                        media=media,
                    )
                    break
                except TelegramRetryAfter as exc:
                    if attempt == 3:
                        raise TelegramSnapshotError(
                            "Telegram rate limit prevented snapshot update after local commit"
                        ) from exc
                    await asyncio.sleep(exc.retry_after + 0.1)
                except Exception as exc:
                    log.exception("Could not update pinned Telegram SQLite snapshot")
                    raise TelegramSnapshotError(
                        "Telegram snapshot update failed after local commit"
                    ) from exc
            self._last_sha256 = digest

    @staticmethod
    def _caption_sha(caption: str) -> str | None:
        for line in caption.splitlines():
            if line.startswith("SHA-256:"):
                value = line.partition(":")[2].strip().lower()
                if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value):
                    return value
        return None

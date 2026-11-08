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
        self._pending_connection: Any | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        self._last_sha256: str | None = None
        self._last_attempt_at = 0
        self._last_success_at = 0
        self._last_snapshot_bytes = 0
        self._last_error: str | None = None

    def health_status(self) -> dict[str, Any]:
        """Operational status without exposing Telegram file IDs or error text."""
        return {
            "ok": self._last_error is None,
            "last_attempt_at": self._last_attempt_at or None,
            "last_success_at": self._last_success_at or None,
            "size_bytes": self._last_snapshot_bytes or None,
            "limit_bytes": MAX_DOWNLOAD_BYTES,
            "near_limit": self._last_snapshot_bytes >= int(MAX_DOWNLOAD_BYTES * 0.8),
            "last_error": bool(self._last_error),
        }

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
        self._last_snapshot_bytes = len(compressed)
        self._last_attempt_at = self._last_success_at = int(time.time())
        self._last_error = None
        log.info("Restored SQLite snapshot from Telegram channel %s", self.chat_id)

    async def schedule_persist_connection(self, connection: Any) -> None:
        """Queue a coalesced snapshot without making a database commit wait on Telegram.

        The local SQLite commit is the request-path durability boundary. Telegram
        remains the remote recovery copy; a slow Bot API upload must never keep a
        game transaction open while its round is already running.
        """
        self._pending_connection = connection
        if self._snapshot_task is None or self._snapshot_task.done():
            self._snapshot_task = asyncio.create_task(self._drain_pending_snapshots())

    async def _drain_pending_snapshots(self) -> None:
        while self._pending_connection is not None:
            connection = self._pending_connection
            self._pending_connection = None
            for attempt in range(3):
                try:
                    await self.persist_connection(connection)
                    break
                except Exception:
                    # Local commit already succeeded. Record/report the remote-copy
                    # failure; retry briefly unless a newer database state is queued.
                    log.exception("Background Telegram SQLite snapshot failed")
                    if self._pending_connection is not None or attempt == 2:
                        break
                    await asyncio.sleep(1 << attempt)

    async def flush_pending(self) -> None:
        """Wait for queued snapshots before a graceful database shutdown."""
        while self._snapshot_task is not None:
            task = self._snapshot_task
            await task
            if task is self._snapshot_task and task.done():
                self._snapshot_task = None
            if self._pending_connection is not None and self._snapshot_task is None:
                self._snapshot_task = asyncio.create_task(self._drain_pending_snapshots())

    async def persist_connection(self, connection: Any) -> None:
        """Track snapshot health around a durable remote write."""
        self._last_attempt_at = int(time.time())
        try:
            await self._persist_connection(connection)
        except Exception as exc:
            self._last_error = type(exc).__name__
            raise
        else:
            self._last_success_at = int(time.time())
            self._last_error = None

    async def _persist_connection(self, connection: Any) -> None:
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
            if (
                len(compressed) >= int(MAX_DOWNLOAD_BYTES * 0.8)
                and self._last_snapshot_bytes < int(MAX_DOWNLOAD_BYTES * 0.8)
            ):
                log.warning(
                    "Telegram SQLite snapshot is approaching Bot API limit: %.1f / %.1f MB",
                    len(compressed) / (1024 * 1024),
                    MAX_DOWNLOAD_BYTES / (1024 * 1024),
                )
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
            self._last_snapshot_bytes = len(compressed)

    @staticmethod
    def _caption_sha(caption: str) -> str | None:
        for line in caption.splitlines():
            if line.startswith("SHA-256:"):
                value = line.partition(":")[2].strip().lower()
                if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value):
                    return value
        return None

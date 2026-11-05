"""Драйверы БД: SQLite (локально) и Postgres/Neon (хостинг).

Оба бэкенда отдают одинаковый интерфейс: `execute()`, `fetchone/fetchall`,
контекстный менеджер и `lastrowid`. SQL пишется один раз с `?`-плейсхолдерами,
для Postgres они переводятся в `%s` — так игра не зависит от хостинга.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    full_name    TEXT,
    balance      INTEGER NOT NULL,
    bonus_at     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    last_chat_id INTEGER
);

CREATE TABLE IF NOT EXISTS wheel_spins (
    user_id  INTEGER PRIMARY KEY,
    spun_at  INTEGER NOT NULL,
    prize    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tease_claims (
    user_id    INTEGER PRIMARY KEY,
    claimed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id  INTEGER PRIMARY KEY,
    title    TEXT,
    autopost INTEGER NOT NULL DEFAULT 1,
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fonbet_id         INTEGER NOT NULL UNIQUE,
    tournament        TEXT    NOT NULL,
    team_a            TEXT    NOT NULL,
    team_b            TEXT    NOT NULL,
    start_time        INTEGER NOT NULL,
    sport             TEXT    NOT NULL DEFAULT 'cs2',
    odds_a            REAL,
    odds_x            REAL,
    odds_b            REAL,
    score_a           INTEGER,
    score_b           INTEGER,
    reg_a             INTEGER,
    reg_b             INTEGER,
    status            TEXT    NOT NULL DEFAULT 'open',
    winner            TEXT,
    result_source     TEXT,
    posted_chat_id    INTEGER,
    posted_message_id INTEGER,
    last_result_check INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    finished_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status, start_time);
CREATE INDEX IF NOT EXISTS idx_matches_fonbet ON matches(fonbet_id);

CREATE TABLE IF NOT EXISTS match_maps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    fonbet_id   INTEGER NOT NULL UNIQUE,
    map_number  INTEGER NOT NULL,
    odds_a      REAL,
    odds_b      REAL,
    score_a     INTEGER,
    score_b     INTEGER,
    status      TEXT    NOT NULL DEFAULT 'open',
    winner      TEXT,
    created_at  INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_maps_match ON match_maps(match_id);

CREATE TABLE IF NOT EXISTS bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id   INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER,
    side       TEXT    NOT NULL,
    amount     INTEGER NOT NULL,
    odds       REAL    NOT NULL,
    payout     INTEGER,
    market     TEXT    NOT NULL DEFAULT 'match',
    map_id     INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bets_match ON bets(match_id);
CREATE INDEX IF NOT EXISTS idx_bets_user  ON bets(user_id);

CREATE TABLE IF NOT EXISTS expresses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER,
    stake      INTEGER NOT NULL,
    odds       REAL    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open',
    payout     INTEGER,
    created_at INTEGER NOT NULL,
    settled_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_expresses_user ON expresses(user_id, status);

CREATE TABLE IF NOT EXISTS express_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    express_id INTEGER NOT NULL REFERENCES expresses(id) ON DELETE CASCADE,
    match_id   INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    side       TEXT    NOT NULL,
    odds       REAL    NOT NULL,
    result     TEXT    NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_express_items_match ON express_items(match_id);
CREATE INDEX IF NOT EXISTS idx_express_items_express ON express_items(express_id);

CREATE TABLE IF NOT EXISTS backup_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    users      INTEGER NOT NULL,
    bets       INTEGER NOT NULL,
    kind       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS app_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);

CREATE TABLE IF NOT EXISTS casino_bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    game       TEXT    NOT NULL,
    selection  TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    amount     INTEGER NOT NULL,
    odds       REAL    NOT NULL,
    outcome    TEXT    NOT NULL,
    win        INTEGER NOT NULL,
    payout     INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_user ON casino_bets(user_id, created_at);
"""

SCHEMA_POSTGRES = (
    SCHEMA_SQLITE
    .replace(
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
    )
    .replace("REAL", "DOUBLE PRECISION")
    .replace("INTEGER", "BIGINT")
)


class Query:
    """`await conn.execute(...)` и `async with conn.execute(...)` — оба работают."""

    def __init__(self, runner) -> None:  # runner: () -> Awaitable[Rows]
        self._runner = runner
        self._rows: Rows | None = None

    async def _resolve(self) -> Rows:
        if self._rows is None:
            self._rows = await self._runner()
        return self._rows

    def __await__(self):
        return self._resolve().__await__()

    async def __aenter__(self) -> Rows:
        return await self._resolve()

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class Rows:
    """Материализованный результат запроса — одинаково для SQLite и Postgres."""

    def __init__(
        self,
        rows: Sequence[Any],
        *,
        lastrowid: int | None = None,
        rowcount: int | None = None,
    ) -> None:
        self._rows = list(rows)
        self.lastrowid = lastrowid
        self.rowcount = len(self._rows) if rowcount is None else rowcount

    async def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[Any]:
        return list(self._rows)

    async def __aenter__(self) -> Rows:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


def to_postgres_sql(sql: str) -> str:
    """`?` → `%s`, `BEGIN IMMEDIATE` → `BEGIN`."""
    sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
    return sql.replace("?", "%s")


# --------------------------------------------------------------------- SQLite
class SQLiteBackend:
    name = "sqlite"
    begin_sql = "BEGIN IMMEDIATE"

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, path: str | Path) -> SQLiteBackend:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")
        return cls(conn)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Query:
        return Query(lambda: self._run(sql, params))

    async def _run(self, sql: str, params: Sequence[Any]) -> Rows:
        cur = await self._conn.execute(sql, tuple(params))
        try:
            try:
                rows = await cur.fetchall()
            except aiosqlite.ProgrammingError:
                rows = []
            return Rows(rows, lastrowid=cur.lastrowid, rowcount=cur.rowcount)
        finally:
            await cur.close()

    async def executescript(self, script: str) -> None:
        await self._conn.executescript(script)

    async def table_columns(self, table: str) -> set[str]:
        async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
            return {row["name"] for row in await cur.fetchall()}

    async def has_table(self, table: str) -> bool:
        async with self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ) as cur:
            return await cur.fetchone() is not None

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()

    async def close(self) -> None:
        await self._conn.close()


# ------------------------------------------------------------------- Postgres
class PostgresBackend:
    name = "postgres"
    begin_sql = "BEGIN"

    def __init__(self, conn: Any) -> None:  # psycopg.AsyncConnection
        self._conn = conn

    @classmethod
    async def open(cls, dsn: str, *, schema: str = "") -> PostgresBackend:
        import psycopg
        from psycopg.rows import dict_row

        conn = await psycopg.AsyncConnection.connect(
            dsn, autocommit=True, row_factory=dict_row, prepare_threshold=0
        )
        if schema:
            # пулеры Neon не пропускают search_path в стартовых параметрах — задаём запросом
            if not schema.replace("_", "").isalnum():
                raise ValueError(f"Недопустимое имя схемы: {schema}")
            cur = conn.cursor()
            try:
                await cur.execute(f'SET search_path TO "{schema}"')
            finally:
                await cur.close()
        log.info("Подключился к Postgres")
        return cls(conn)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Query:
        return Query(lambda: self._run(sql, params))

    async def _run(self, sql: str, params: Sequence[Any]) -> Rows:
        cur = self._conn.cursor()
        try:
            await cur.execute(to_postgres_sql(sql), tuple(params))
            rows = await cur.fetchall() if cur.description else []
            lastrowid = None
            if rows and isinstance(rows[0], dict) and "id" in rows[0] and len(rows[0]) == 1:
                lastrowid = int(rows[0]["id"])
            return Rows(rows, lastrowid=lastrowid, rowcount=cur.rowcount)
        finally:
            await cur.close()

    async def executescript(self, script: str) -> None:
        """Postgres не принимает несколько команд в одном запросе — режем по `;`."""
        lines = [line.split("--", 1)[0] for line in script.splitlines()]
        cleaned = "\n".join(lines)
        for chunk in cleaned.split(";"):
            statement = chunk.strip()
            if not statement:
                continue
            cur = self._conn.cursor()
            try:
                await cur.execute(statement)
            finally:
                await cur.close()

    async def table_columns(self, table: str) -> set[str]:
        data = await (
            await self.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_name = ? AND table_schema = current_schema()",
                (table,),
            )
        ).fetchall()
        return {row["name"] for row in data}

    async def has_table(self, table: str) -> bool:
        return await (
            await self.execute(
                "SELECT 1 AS c FROM information_schema.tables "
                "WHERE table_name = ? AND table_schema = current_schema()",
                (table,),
            )
        ).fetchone() is not None

    async def commit(self) -> None:
        try:
            await self._conn.commit()
        except Exception as exc:
            log.debug("commit: %s", exc)

    async def rollback(self) -> None:
        try:
            await self._conn.rollback()
        except Exception as exc:
            log.debug("rollback: %s", exc)

    async def close(self) -> None:
        await self._conn.close()

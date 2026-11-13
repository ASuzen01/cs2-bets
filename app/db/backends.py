"""Драйверы БД: SQLite (локально) и Postgres/Neon (хостинг).

Оба бэкенда отдают одинаковый интерфейс: `execute()`, `fetchone/fetchall`,
контекстный менеджер и `lastrowid`. SQL пишется один раз с `?`-плейсхолдерами,
для Postgres они переводятся в `%s` — так игра не зависит от хостинга.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
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
    claimed_at INTEGER NOT NULL,
    tap_count  INTEGER NOT NULL DEFAULT 0,
    tap_at     INTEGER NOT NULL DEFAULT 0
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
CREATE INDEX IF NOT EXISTS idx_matches_sport_status_start ON matches(sport, status, start_time, id);
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

-- Настройки личных уведомлений и подписки на матчи.
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id                  INTEGER PRIMARY KEY,
    result_notifications     INTEGER NOT NULL DEFAULT 1,
    updated_at               INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS match_follows (
    user_id    INTEGER NOT NULL,
    match_id   INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, match_id)
);
CREATE INDEX IF NOT EXISTS idx_match_follows_match ON match_follows(match_id);

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
    cashed_out INTEGER NOT NULL DEFAULT 0,
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
    outcome     TEXT    NOT NULL,
    win         INTEGER NOT NULL,
    payout      INTEGER NOT NULL,
    result_json TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_user ON casino_bets(user_id, created_at);

-- «Пьяница»: приглашения и завершённые серверные партии с результатом.
CREATE TABLE IF NOT EXISTS drunkard_games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id   INTEGER NOT NULL,
    opponent_id  INTEGER,
    creator_name TEXT NOT NULL,
    opponent_name TEXT NOT NULL,
    mode         TEXT NOT NULL,
    deck_size    INTEGER NOT NULL,
    stake        INTEGER NOT NULL,
    status       TEXT NOT NULL,
    result_json  TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    settled_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_drunkard_creator ON drunkard_games(creator_id, id);
CREATE INDEX IF NOT EXISTS idx_drunkard_opponent ON drunkard_games(opponent_id, id);
CREATE INDEX IF NOT EXISTS idx_drunkard_status ON drunkard_games(status, created_at);

-- Серверные столы «Дурака»: правила и скрытые руки живут только в state_json.
CREATE TABLE IF NOT EXISTS durak_games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER NOT NULL,
    room_code    TEXT NOT NULL UNIQUE,
    visibility   TEXT NOT NULL,
    status       TEXT NOT NULL,
    config_json  TEXT NOT NULL,
    state_json   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_durak_status_updated ON durak_games(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_durak_owner ON durak_games(owner_id, id);

-- незавершённые фри-спины и их зафиксированная ставка хранятся на сервере
CREATE TABLE IF NOT EXISTS slot_states (
    user_id     INTEGER PRIMARY KEY,
    free_spins  INTEGER NOT NULL DEFAULT 0,
    spin_bet   INTEGER NOT NULL DEFAULT 0,
    bonus_json TEXT,
    updated_at INTEGER NOT NULL
);

-- незавершённый бонус «Золотого разлома»: выигрыш и накопитель множителя
CREATE TABLE IF NOT EXISTS riftfall_states (
    user_id     INTEGER PRIMARY KEY,
    free_spins  INTEGER NOT NULL DEFAULT 0,
    spin_bet    INTEGER NOT NULL DEFAULT 0,
    bonus_json  TEXT,
    updated_at  INTEGER NOT NULL
);

-- активная партия «Лесенки»: скрытое окно хранится только на сервере
CREATE TABLE IF NOT EXISTS ladder_games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    stake        INTEGER NOT NULL,
    round        INTEGER NOT NULL,
    prize_index  INTEGER NOT NULL,
    picked_index INTEGER,
    status       TEXT NOT NULL,
    payout       INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ladder_user ON ladder_games(user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ladder_one_active ON ladder_games(user_id)
    WHERE status IN ('choosing', 'won');

-- блэкджек: shoe и закрытая карта дилера доступны только серверу
CREATE TABLE IF NOT EXISTS blackjack_games (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    stake      INTEGER NOT NULL,
    status     TEXT NOT NULL,
    state_json TEXT NOT NULL,
    payout     INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    settled_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_blackjack_user ON blackjack_games(user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_blackjack_one_active ON blackjack_games(user_id)
    WHERE status IN ('insurance', 'playing');

-- crash game «Самолётик»: скрытая точка вылета хранится только на сервере
CREATE TABLE IF NOT EXISTS crash_games (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    stake            INTEGER NOT NULL,
    crash_multiplier REAL NOT NULL,
    started_at       REAL NOT NULL,
    cashout_multiplier REAL,
    status           TEXT NOT NULL,
    payout           INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crash_user ON crash_games(user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_crash_one_active ON crash_games(user_id)
    WHERE status = 'flying';

-- «Мины»: полный расклад хранится только на сервере до завершения раунда
CREATE TABLE IF NOT EXISTS mines_games (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    stake          INTEGER NOT NULL,
    mine_count     INTEGER NOT NULL,
    mine_positions TEXT NOT NULL,
    opened_cells   TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL,
    payout         INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mines_user ON mines_games(user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mines_one_active ON mines_games(user_id)
    WHERE status = 'active';

-- задания дня: прогресс и награда, по строке на игрока в сутки
CREATE TABLE IF NOT EXISTS quests (
    user_id    INTEGER NOT NULL,
    day        INTEGER NOT NULL,
    code       TEXT    NOT NULL,
    progress   INTEGER NOT NULL DEFAULT 0,
    target     INTEGER NOT NULL,
    reward     INTEGER NOT NULL,
    claimed_at INTEGER,
    PRIMARY KEY (user_id, day, code)
);

-- события для аналитики: что игроки делают на самом деле
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    name       TEXT    NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

-- достижения: открываются один раз и остаются в профиле
CREATE TABLE IF NOT EXISTS achievements (
    user_id     INTEGER NOT NULL,
    code        TEXT    NOT NULL,
    unlocked_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, code)
);

-- цепочка дней с выполненными заданиями: сундук за 3 и 7 дней
CREATE TABLE IF NOT EXISTS quest_chain (
    user_id   INTEGER PRIMARY KEY,
    days      INTEGER NOT NULL DEFAULT 0,
    last_day  INTEGER NOT NULL DEFAULT 0,
    best_days INTEGER NOT NULL DEFAULT 0,
    chest_day INTEGER NOT NULL DEFAULT 0
);

-- приглашённые друзья: платим, когда друг делает первую ставку
CREATE TABLE IF NOT EXISTS referrals (
    invited_id INTEGER PRIMARY KEY,
    inviter_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    paid_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_referrals_inviter ON referrals(inviter_id);

-- билеты в экспресс: приз колеса, ставка за счёт заведения
CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'wheel',
    kind       TEXT    NOT NULL DEFAULT 'express',
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    used_at    INTEGER,
    express_id INTEGER,
    bet_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);

-- идемпотентность действий игрока: один и тот же тап не должен списать дважды
CREATE TABLE IF NOT EXISTS bet_requests (
    user_id    INTEGER NOT NULL,
    channel    TEXT    NOT NULL,
    token      TEXT    NOT NULL,
    bet_id     INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, channel, token)
);
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

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        commit_hook: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
    ) -> None:
        self._conn = conn
        self._commit_hook = commit_hook
        self._durability_dirty = False

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

    async def _sync_durable(self) -> None:
        if self._commit_hook is None:
            self._durability_dirty = False
            return
        try:
            await self._commit_hook(self._conn)
        except Exception:
            self._durability_dirty = True
            raise
        self._durability_dirty = False

    async def _ensure_durable(self) -> None:
        if self._durability_dirty:
            await self._sync_durable()

    async def _run(self, sql: str, params: Sequence[Any]) -> Rows:
        await self._ensure_durable()
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

    def set_commit_hook(
        self, hook: Callable[[aiosqlite.Connection], Awaitable[None]] | None
    ) -> None:
        self._commit_hook = hook

    async def commit(self) -> None:
        await self._conn.commit()
        if self._commit_hook is not None:
            await self._sync_durable()

    async def rollback(self) -> None:
        await self._conn.rollback()

    async def close(self) -> None:
        await self._conn.close()


# ------------------------------------------------------------------- Postgres
class PostgresBackend:
    name = "postgres"
    begin_sql = "BEGIN"

    def __init__(self, conn: Any, *, dsn: str = "", schema: str = "") -> None:  # psycopg.AsyncConnection
        self._conn = conn
        self._dsn = dsn
        self._schema = schema
        self._reconnect_lock = asyncio.Lock()
        self._reconnects = 0
        self._transaction_active = False

    @staticmethod
    async def _open_connection(dsn: str, schema: str) -> Any:
        import psycopg
        from psycopg.rows import dict_row

        conn = await psycopg.AsyncConnection.connect(
            dsn, autocommit=True, row_factory=dict_row, prepare_threshold=0,
        )
        if schema:
            # Пулеры Neon не пропускают search_path в стартовых параметрах — задаём запросом.
            if not schema.replace("_", "").isalnum():
                await conn.close()
                raise ValueError(f"Недопустимое имя схемы: {schema}")
            try:
                cur = conn.cursor()
                try:
                    await cur.execute(f'SET search_path TO "{schema}"')
                finally:
                    await cur.close()
            except BaseException:
                await conn.close()
                raise
        return conn

    @classmethod
    async def open(cls, dsn: str, *, schema: str = "") -> PostgresBackend:
        conn = await cls._open_connection(dsn, schema)
        log.info("Подключился к Postgres")
        return cls(conn, dsn=dsn, schema=schema)

    async def _ensure_connected(self) -> None:
        """Reconnect after Neon/pooler closes an idle socket.

        This runs before a new statement only. A statement that fails mid-flight
        is never replayed, so writes cannot be duplicated and an interrupted
        transaction is surfaced to its caller.
        """
        if not getattr(self._conn, "closed", False):
            return
        if self._transaction_active:
            raise RuntimeError("Postgres connection was lost during a transaction; refusing to replay it")
        async with self._reconnect_lock:
            if not getattr(self._conn, "closed", False):
                return
            if self._transaction_active:
                raise RuntimeError("Postgres connection was lost during a transaction; refusing to replay it")
            if not self._dsn:
                raise RuntimeError("Postgres connection is closed and no DSN is available for reconnect")
            self._conn = await self._open_connection(self._dsn, self._schema)
            self._reconnects += 1
            log.warning("Соединение Postgres было закрыто; подключение восстановлено (reconnects=%s)", self._reconnects)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Query:
        return Query(lambda: self._run(sql, params))

    async def _run(self, sql: str, params: Sequence[Any]) -> Rows:
        await self._ensure_connected()
        cur = self._conn.cursor()
        try:
            await cur.execute(to_postgres_sql(sql), tuple(params))
            command = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
            if command == "BEGIN":
                self._transaction_active = True
            elif command in {"COMMIT", "ROLLBACK"}:
                self._transaction_active = False
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
        # Do not hide a lost connection: transaction callers must not report
        # success when the server did not acknowledge the commit.
        try:
            await self._conn.commit()
        finally:
            self._transaction_active = False

    async def rollback(self) -> None:
        try:
            await self._conn.rollback()
        finally:
            self._transaction_active = False

    async def close(self) -> None:
        await self._conn.close()

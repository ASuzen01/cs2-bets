"""Слой данных: SQLite (локально) или Postgres/Neon (хостинг).

Модель:
  matches — реальные матчи CS2 из линии Fonbet (один ряд на событие), кэфы на победителя;
  bets    — ставки с зафиксированным на момент ставки кэфом (fixed odds);
  users   — виртуальные балансы;
  ledger  — история операций.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app.config import settings
from app.db.backends import (
    SCHEMA_POSTGRES,
    SCHEMA_SQLITE,
    PostgresBackend,
    SQLiteBackend,
)

log = logging.getLogger(__name__)

# колонки, добавленные после первых деплоев: дорисовываем их в старых базах
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("matches", "score_a", "ALTER TABLE matches ADD COLUMN score_a INTEGER"),
    ("matches", "score_b", "ALTER TABLE matches ADD COLUMN score_b INTEGER"),
    ("bets", "market", "ALTER TABLE bets ADD COLUMN market TEXT NOT NULL DEFAULT 'match'"),
    ("bets", "map_id", "ALTER TABLE bets ADD COLUMN map_id INTEGER"),
    ("users", "last_chat_id", "ALTER TABLE users ADD COLUMN last_chat_id BIGINT"),
    ("matches", "sport", "ALTER TABLE matches ADD COLUMN sport TEXT NOT NULL DEFAULT 'cs2'"),
    ("matches", "odds_x", "ALTER TABLE matches ADD COLUMN odds_x REAL"),
    ("matches", "team_a_en", "ALTER TABLE matches ADD COLUMN team_a_en TEXT"),
    ("matches", "team_b_en", "ALTER TABLE matches ADD COLUMN team_b_en TEXT"),
)


BACKUP_TABLES: tuple[str, ...] = (
    "users",
    "chats",
    "matches",
    "match_maps",
    "bets",
    "expresses",
    "express_items",
    "ledger",
    "wheel_spins",
    "tease_claims",
    "backup_log",
)
"""Порядок важен: сначала родители, потом зависимые строки."""


# id чатов и игроков не влезают в Postgres-ный INTEGER (int4): у супергрупп
# chat_id вида -1001234567890 → «NumericValueOutOfRange: integer out of range».
# Колонки, которые обязаны быть BIGINT.
WIDE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("users", "user_id"),
    ("users", "last_chat_id"),
    ("chats", "chat_id"),
    ("matches", "fonbet_id"),
    ("matches", "posted_chat_id"),
    ("match_maps", "fonbet_id"),
    ("bets", "user_id"),
    ("bets", "chat_id"),
    ("expresses", "user_id"),
    ("expresses", "chat_id"),
    ("ledger", "user_id"),
    ("wheel_spins", "user_id"),
    ("tease_claims", "user_id"),
)


class BettingError(Exception):
    """Игровая ошибка, текст можно показывать пользователю."""


class MatchNotFound(BettingError):
    pass


class MatchNotOpen(BettingError):
    pass


class BackupError(Exception):
    """Дамп битый или не от этой версии приложения."""


class MatchAlreadySettled(BettingError):
    pass


class InsufficientFunds(BettingError):
    pass


class InvalidBet(BettingError):
    pass


class Database:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        start_balance: int = 1000,
        url: str = "",
        schema: str = "",
    ) -> None:
        self._path = Path(path) if path else Path("data/bot.db")
        self._url = (url or "").strip()
        self._schema = (schema or "").strip()
        self.start_balance = start_balance
        self._backend: SQLiteBackend | PostgresBackend | None = None

    # ------------------------------------------------------------------ infra
    @property
    def conn(self) -> SQLiteBackend | PostgresBackend:
        if self._backend is None:
            raise RuntimeError("Database.connect() не был вызван")
        return self._backend

    async def connect(self) -> None:
        if self._url.startswith(("postgres://", "postgresql://")):
            backend = await PostgresBackend.open(self._url, schema=self._schema)
            await backend.executescript(SCHEMA_POSTGRES)
            await self._migrate(backend)
            await self._widen_ids(backend)
            self._backend = backend
            return

        backend = await SQLiteBackend.open(self._path)
        # миграция старой схемы (ручные матчи) — ДО создания индексов новой схемы
        if await self._has_incompatible_matches(backend):
            await backend.executescript("DROP TABLE IF EXISTS bets; DROP TABLE IF EXISTS matches;")
        await backend.executescript(SCHEMA_SQLITE)
        await self._migrate(backend)
        await backend.commit()
        self._backend = backend

    @staticmethod
    async def _migrate(backend: SQLiteBackend | PostgresBackend) -> None:
        """Добавляет колонки, которых нет в старой базе (счёт по картам и т.п.)."""
        for table, column, sql in MIGRATIONS:
            if not await backend.has_table(table):
                continue
            if column in await backend.table_columns(table):
                continue
            await backend.execute(sql)
        await backend.commit()

    @staticmethod
    async def _widen_ids(backend: PostgresBackend) -> None:
        """Postgres: INTEGER (int4) не держит id чатов и больших игроков — расширяем до BIGINT.

        У супергрупп chat_id вида -1001234567890: при записи в int4 Postgres падает
        с «NumericValueOutOfRange: integer out of range», и команды бота в группе
        отвечают «Что-то сломалось».
        """
        for table, column in WIDE_COLUMNS:
            if not await backend.has_table(table):
                continue
            row = await (
                await backend.execute(
                    "SELECT data_type AS t FROM information_schema.columns "
                    "WHERE table_name = ? AND column_name = ? AND table_schema = current_schema()",
                    (table, column),
                )
            ).fetchone()
            if row is None or str(row["t"]) != "integer":
                continue
            try:
                await backend.execute(
                    f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE BIGINT'
                )
                log.info("Колонка %s.%s расширена до BIGINT", table, column)
            except Exception:
                log.exception("Не удалось расширить %s.%s", table, column)

    @staticmethod
    async def _has_incompatible_matches(backend: SQLiteBackend) -> bool:
        if not await backend.has_table("matches"):
            return False
        columns = await backend.table_columns("matches")
        return bool(columns) and "fonbet_id" not in columns

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        conn = self.conn
        await conn.execute(conn.begin_sql)
        try:
            yield conn
        except BaseException:
            await conn.rollback()
            raise
        else:
            await conn.commit()

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Any | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _ledger(
        self, conn: Any, user_id: int, delta: int, reason: str, now: int | None = None
    ) -> None:
        await conn.execute(
            "INSERT INTO ledger (user_id, delta, reason, created_at) VALUES (?, ?, ?, ?)",
            (user_id, delta, reason, now or int(time.time())),
        )

    # ----------------------------------------------------------------- users
    async def upsert_user(self, user_id: int, username: str | None, full_name: str | None) -> None:
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, full_name, balance, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username     = excluded.username,
                full_name    = excluded.full_name,
                last_seen_at = excluded.last_seen_at
            """,
            (user_id, username, full_name, self.start_balance, now, now),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> Any | None:
        return await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))

    async def get_balance(self, user_id: int) -> int:
        row = await self._fetchone("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        return int(row["balance"]) if row else 0

    async def daily_bonus(
        self, user_id: int, amount: int, cooldown_seconds: int
    ) -> tuple[bool, int, int]:
        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT bonus_at, balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                balance = self.start_balance + amount
                await conn.execute(
                    "INSERT INTO users (user_id, balance, bonus_at, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, balance, now, now, now),
                )
                await self._ledger(conn, user_id, amount, "ежедневный бонус", now)
                return True, 0, balance

            elapsed = now - int(row["bonus_at"])
            if elapsed < cooldown_seconds:
                return False, cooldown_seconds - elapsed, int(row["balance"])

            await conn.execute(
                "UPDATE users SET balance = balance + ?, bonus_at = ? WHERE user_id = ?",
                (amount, now, user_id),
            )
            await self._ledger(conn, user_id, amount, "ежедневный бонус", now)
            return True, 0, int(row["balance"]) + amount

    # ----------------------------------------------------------------- бэкапы
    def is_postgres(self) -> bool:
        return self._url.startswith(("postgres://", "postgresql://"))

    async def dump_rows(self) -> dict[str, list[dict[str, Any]]]:
        """Снимок всех таблиц: переносимый между SQLite и Postgres."""
        data: dict[str, list[dict[str, Any]]] = {}
        for table in BACKUP_TABLES:
            rows = await self._fetchall(f"SELECT * FROM {table}")
            data[table] = [dict(row) for row in rows]
        return data

    async def restore_rows(self, data: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        """Полная замена содержимого базы данными из дампа."""
        unknown = set(data) - set(BACKUP_TABLES)
        if unknown:
            raise BackupError(f"В дампе незнакомые таблицы: {', '.join(sorted(unknown))}")

        counts: dict[str, int] = {}
        async with self.transaction() as conn:
            for table in reversed(BACKUP_TABLES):
                await conn.execute(f"DELETE FROM {table}")
            for table in BACKUP_TABLES:
                rows = data.get(table) or []
                for row in rows:
                    columns = list(row)
                    placeholders = ", ".join("?" for _ in columns)
                    await conn.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        tuple(row[column] for column in columns),
                    )
                counts[table] = len(rows)

        if self.is_postgres():
            await self._reset_sequences()
        return counts

    async def _reset_sequences(self) -> None:
        """После вставки строк с явными id сдвигаем счётчики Postgres."""
        for table in BACKUP_TABLES:
            columns = await self._backend.table_columns(table)  # type: ignore[union-attr]
            if "id" not in columns:
                continue
            await self._backend.execute(  # type: ignore[union-attr]
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            )

    async def log_backup(self, size_bytes: int, users: int, bets: int, kind: str) -> int:
        now = int(time.time())
        await self.conn.execute(
            "INSERT INTO backup_log (created_at, size_bytes, users, bets, kind) VALUES (?, ?, ?, ?, ?)",
            (now, size_bytes, users, bets, kind),
        )
        await self.conn.commit()
        return now

    async def last_backup(self) -> Any | None:
        return await self._fetchone("SELECT * FROM backup_log ORDER BY created_at DESC LIMIT 1")

    # ---------------------------------------------------------------- бонусы
    async def bonus_state(self, user_id: int, daily_cooldown: int, wheel_cooldown: int,
                          tease_cooldown: int) -> dict[str, Any]:
        """Что из бонусов доступно прямо сейчас."""
        now = int(time.time())
        user = await self._fetchone("SELECT bonus_at FROM users WHERE user_id = ?", (user_id,))
        wheel = await self._fetchone("SELECT spun_at FROM wheel_spins WHERE user_id = ?", (user_id,))
        tease = await self._fetchone("SELECT claimed_at FROM tease_claims WHERE user_id = ?", (user_id,))

        def wait(ts: int | None, cooldown: int) -> int:
            if not ts:
                return 0
            return max(0, cooldown - (now - int(ts)))

        return {
            "daily": {"wait_seconds": wait(int(user["bonus_at"]) if user else 0, daily_cooldown)},
            "wheel": {"wait_seconds": wait(int(wheel["spun_at"]) if wheel else 0, wheel_cooldown)},
            "tease": {"wait_seconds": wait(int(tease["claimed_at"]) if tease else 0, tease_cooldown)},
        }

    async def claim_wheel(self, user_id: int, prize: int, cooldown: int) -> tuple[bool, int, int]:
        """Крутка колеса: не чаще раза в cooldown. Возвращает (успех, ждать_сек, баланс)."""
        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT spun_at FROM wheel_spins WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            last = int(row["spun_at"]) if row else 0
            if last and now - last < cooldown:
                return False, cooldown - (now - last), await self._balance_in(conn, user_id)

            if row:
                await conn.execute(
                    "UPDATE wheel_spins SET spun_at = ?, prize = ? WHERE user_id = ?",
                    (now, prize, user_id),
                )
            else:
                await conn.execute(
                    "INSERT INTO wheel_spins (user_id, spun_at, prize) VALUES (?, ?, ?)",
                    (user_id, now, prize),
                )
            return await self._credit(conn, user_id, prize, "колесо фортуны", now)

    async def claim_paid_wheel(self, user_id: int, cost: int, prize: int) -> tuple[bool, int, int]:
        """Платная крутка: списываем стоимость и сразу начисляем приз."""
        now = int(time.time())
        async with self.transaction() as conn:
            balance = await self._balance_in(conn, user_id)
            if balance < cost:
                return False, 0, balance
            await conn.execute(
                "UPDATE users SET balance = balance - ? + ? WHERE user_id = ?",
                (cost, prize, user_id),
            )
            await self._ledger(conn, user_id, prize - cost, "колесо фортуны · платная крутка", now)
            return True, 0, await self._balance_in(conn, user_id)

    async def claim_tease(self, user_id: int, amount: int, cooldown: int) -> tuple[bool, int, int]:
        """Пасхалка за десять нажатий: тоже раз в сутки."""
        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT claimed_at FROM tease_claims WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            last = int(row["claimed_at"]) if row else 0
            if last and now - last < cooldown:
                return False, cooldown - (now - last), await self._balance_in(conn, user_id)

            if row:
                await conn.execute(
                    "UPDATE tease_claims SET claimed_at = ? WHERE user_id = ?", (now, user_id)
                )
            else:
                await conn.execute(
                    "INSERT INTO tease_claims (user_id, claimed_at) VALUES (?, ?)", (user_id, now)
                )
            return await self._credit(conn, user_id, amount, "за настойчивость", now)

    @staticmethod
    async def _balance_in(conn: Any, user_id: int) -> int:
        async with conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return int(row["balance"]) if row else 0

    async def _credit(
        self, conn: Any, user_id: int, amount: int, reason: str, now: int
    ) -> tuple[bool, int, int]:
        await conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id)
        )
        await self._ledger(conn, user_id, amount, reason, now)
        return True, 0, await self._balance_in(conn, user_id)

    async def remember_chat(self, user_id: int, chat_id: int) -> None:
        """Помним, из какого чата игрок заходил: туда напишем, если личка закрыта."""
        if chat_id >= 0:
            return  # личка и так есть
        await self.conn.execute(
            "UPDATE users SET last_chat_id = ? WHERE user_id = ?", (chat_id, user_id)
        )
        await self.conn.commit()

    async def find_user(self, ident: str) -> Any | None:
        """Ищет игрока по id, @username или username — чтобы админу не переписывать цифры."""
        ident = ident.strip().lstrip("@")
        if not ident:
            return None
        if ident.lstrip("-").isdigit():
            row = await self._fetchone("SELECT * FROM users WHERE user_id = ?", (int(ident),))
            if row is not None:
                return row
        return await self._fetchone(
            "SELECT * FROM users WHERE lower(username) = lower(?) ORDER BY last_seen_at DESC LIMIT 1",
            (ident,),
        )

    async def recent_users(self, limit: int = 20) -> list[Any]:
        """Последние активные игроки — удобно для начисления бонусов."""
        return await self._fetchall(
            "SELECT user_id, username, full_name, balance FROM users "
            "ORDER BY last_seen_at DESC, user_id LIMIT ?",
            (limit,),
        )

    async def top_users(self, limit: int = 10) -> list[Any]:
        return await self._fetchall(
            "SELECT user_id, username, full_name, balance FROM users ORDER BY balance DESC, user_id LIMIT ?",
            (limit,),
        )

    async def history(self, user_id: int, limit: int = 10) -> list[Any]:
        return await self._fetchall(
            "SELECT delta, reason, created_at FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    async def add_coins(self, user_id: int, amount: int, reason: str) -> int:
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id)
            )
            await self._ledger(conn, user_id, amount, reason)
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        return int(row["balance"]) if row else 0

    # ----------------------------------------------------------------- chats
    async def register_chat(self, chat_id: int, title: str | None) -> None:
        await self.conn.execute(
            """
            INSERT INTO chats (chat_id, title, added_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
            """,
            (chat_id, title, int(time.time())),
        )
        await self.conn.commit()

    async def set_autopost(self, chat_id: int, enabled: bool) -> None:
        await self.conn.execute(
            """
            INSERT INTO chats (chat_id, autopost, added_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET autopost = excluded.autopost
            """,
            (chat_id, 1 if enabled else 0, int(time.time())),
        )
        await self.conn.commit()

    async def list_autopost_chats(self) -> list[Any]:
        return await self._fetchall("SELECT chat_id, title FROM chats WHERE autopost = 1")

    # --------------------------------------------------------------- matches
    async def match_by_fonbet(self, fonbet_id: int) -> Any | None:
        return await self._fetchone("SELECT * FROM matches WHERE fonbet_id = ?", (fonbet_id,))

    async def recent_cancelled(self, limit: int = 10) -> list[Any]:
        return await self._fetchall(
            "SELECT * FROM matches WHERE status = 'cancelled' ORDER BY finished_at DESC LIMIT ?",
            (limit,),
        )

    async def upsert_match(
        self,
        *,
        fonbet_id: int,
        tournament: str,
        team_a: str,
        team_b: str,
        start_time: int,
        odds_a: float | None,
        odds_b: float | None,
        score_a: int | None = None,
        score_b: int | None = None,
        odds_x: float | None = None,
        sport: str = "cs2",
    ) -> tuple[Any, bool]:
        """Возвращает (матч, создан_ли_сейчас)."""
        now = int(time.time())
        existing = await self._fetchone("SELECT * FROM matches WHERE fonbet_id = ?", (fonbet_id,))
        if existing is None:
            await self.conn.execute(
                """
                INSERT INTO matches (fonbet_id, tournament, team_a, team_b, start_time, sport,
                                     odds_a, odds_x, odds_b, score_a, score_b, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (fonbet_id, tournament, team_a, team_b, start_time, sport, odds_a, odds_x, odds_b,
                 score_a, score_b, now),
            )
            await self.conn.commit()
            created = await self._fetchone(
                "SELECT * FROM matches WHERE fonbet_id = ?", (fonbet_id,)
            )
            assert created is not None
            return created, True

        # у матча с открытым приёмом (линия и live) обновляем кэфы:
        # ставки, уже сделанные, сохраняют свой кэф
        new_score_a = score_a if score_a is not None else existing["score_a"]
        new_score_b = score_b if score_b is not None else existing["score_b"]

        if existing["status"] in ("open", "live"):
            await self.conn.execute(
                """
                UPDATE matches SET odds_a = COALESCE(?, odds_a),
                                   odds_x = COALESCE(?, odds_x),
                                   odds_b = COALESCE(?, odds_b),
                                   start_time = ?, score_a = ?, score_b = ?
                WHERE id = ?
                """,
                (odds_a, odds_x, odds_b, start_time, new_score_a, new_score_b, existing["id"]),
            )
            await self.conn.commit()
            refreshed = await self._fetchone("SELECT * FROM matches WHERE id = ?", (existing["id"],))
            return refreshed or existing, False

        if existing["status"] == "awaiting" and (score_a is not None or score_b is not None):
            # матч доигран, ждём подтверждения исхода — обновляем хотя бы счёт
            await self.conn.execute(
                "UPDATE matches SET score_a = ?, score_b = ? WHERE id = ?",
                (new_score_a, new_score_b, existing["id"]),
            )
            await self.conn.commit()
            refreshed = await self._fetchone("SELECT * FROM matches WHERE id = ?", (existing["id"],))
            return refreshed or existing, False

        return existing, False

    async def get_match(self, match_id: int) -> Any | None:
        return await self._fetchone("SELECT * FROM matches WHERE id = ?", (match_id,))

    async def open_matches(self, limit: int = 10, sport: str | None = None) -> list[Any]:
        if sport is None:
            return await self._fetchall(
                "SELECT * FROM matches WHERE status = 'open' ORDER BY start_time LIMIT ?", (limit,)
            )
        return await self._fetchall(
            "SELECT * FROM matches WHERE status = 'open' AND sport = ? ORDER BY start_time LIMIT ?",
            (sport, limit),
        )

    async def live_matches(self, limit: int = 40) -> list[Any]:
        """Матчи, которые идут прямо сейчас или ждут подтверждения исхода."""
        return await self._fetchall(
            """
            SELECT * FROM matches WHERE status IN ('live', 'awaiting')
            ORDER BY start_time DESC LIMIT ?
            """,
            (limit,),
        )

    async def status_counts(self, sport: str | None = None) -> dict[str, int]:
        """Сколько матчей в каждом статусе — для /health и админской диагностики."""
        sql = "SELECT status, COUNT(*) AS n FROM matches "
        if sport:
            sql += "WHERE sport = ? "
        sql += "GROUP BY status"
        rows = await self._fetchall(sql, (sport,) if sport else ())
        return {str(row["status"]): int(row["n"]) for row in rows}

    async def started_open_count(self, sport: str) -> int:
        """Сколько матчей уже начались, но всё ещё висят в линии (не переехали в live)."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM matches WHERE status = 'open' AND sport = ? "
            "AND start_time <= ?",
            (sport, int(time.time())),
        )
        return int(row["n"]) if row else 0

    async def sample_matches(self, sport: str, statuses: Sequence[str], limit: int = 4) -> list[Any]:
        """Несколько матчей для диагностики в /health (без игроков и ставок)."""
        marks = ", ".join("?" for _ in statuses)
        return await self._fetchall(
            f"SELECT id, team_a, team_b, tournament, status, start_time, score_a, score_b "
            f"FROM matches "
            f"WHERE sport = ? AND status IN ({marks}) ORDER BY start_time LIMIT ?",
            (sport, *statuses, limit),
        )

    async def matches_by_status(self, statuses: Sequence[str]) -> list[Any]:
        placeholders = ",".join("?" for _ in statuses)
        return await self._fetchall(
            f"SELECT * FROM matches WHERE status IN ({placeholders}) ORDER BY start_time",
            tuple(statuses),
        )

    async def set_status(self, match_id: int, status: str) -> None:
        await self.conn.execute("UPDATE matches SET status = ? WHERE id = ?", (status, match_id))
        await self.conn.commit()

    async def close_started_matches(self, now: int | None = None) -> list[Any]:
        """Закрывает приём ставок у матчей, которые уже начались. Возвращает затронутые."""
        now = now or int(time.time())
        started = await self._fetchall(
            "SELECT * FROM matches WHERE status = 'open' AND start_time <= ?", (now,)
        )
        for match in started:
            await self.conn.execute(
                "UPDATE matches SET status = 'live' WHERE id = ?", (match["id"],)
            )
        await self.conn.commit()
        return started

    async def set_card(self, match_id: int, chat_id: int, message_id: int) -> None:
        await self.conn.execute(
            "UPDATE matches SET posted_chat_id = ?, posted_message_id = ? WHERE id = ?",
            (chat_id, message_id, match_id),
        )
        await self.conn.commit()

    async def mark_result_check(self, match_id: int, when: int | None = None) -> None:
        await self.conn.execute(
            "UPDATE matches SET last_result_check = ? WHERE id = ?",
            (int(time.time()) if when is None else when, match_id),
        )
        await self.conn.commit()

    async def recent_matches(self, limit: int = 500) -> list[Any]:
        """Последние матчи — для поиска по названиям команд (без SQL-lower: кириллица)."""
        return await self._fetchall(
            "SELECT * FROM matches ORDER BY start_time DESC LIMIT ?", (limit,)
        )

    async def set_match_names_en(self, match_id: int, team_a_en: str, team_b_en: str) -> None:
        """Английские названия команд: по ним ищем финальный результат во внешнем фиде."""
        await self.conn.execute(
            "UPDATE matches SET team_a_en = ?, team_b_en = ? WHERE id = ?",
            (team_a_en, team_b_en, match_id),
        )
        await self.conn.commit()

    async def matches_without_names_en(self, sport: str = "football") -> list[Any]:
        return await self._fetchall(
            "SELECT * FROM matches WHERE sport = ? AND status IN ('open', 'live', 'awaiting') "
            "AND (team_a_en IS NULL OR team_b_en IS NULL) ORDER BY start_time",
            (sport,),
        )

    async def match_bet_count(self, match_id: int) -> int:
        """Сколько ставок (включая экспрессы) висит на матче — решает, можно ли его снимать."""
        row = await self._fetchone(
            "SELECT (SELECT COUNT(*) FROM bets WHERE match_id = ?)"
            " + (SELECT COUNT(*) FROM express_items WHERE match_id = ?) AS c",
            (match_id, match_id),
        )
        return int(row["c"]) if row else 0

    async def stale_matches(self, hours: int) -> list[Any]:
        """Матчи, которые давно идут (или ждут), а результата так и нет."""
        threshold = int(time.time()) - hours * 3600
        return await self._fetchall(
            "SELECT * FROM matches WHERE status IN ('live', 'awaiting') AND start_time < ? "
            "ORDER BY start_time",
            (threshold,),
        )

    async def matches_for_result_check(
        self, older_than: int, check_cooldown: int = 300
    ) -> list[Any]:
        """Матчи, которые пора проверить на результат: 'live'/'awaiting' и стартовали давно."""
        now = int(time.time())
        return await self._fetchall(
            """
            SELECT * FROM matches
            WHERE status IN ('live', 'awaiting')
              AND start_time <= ?
              AND last_result_check <= ?
            ORDER BY start_time
            """,
            (now - older_than, now - check_cooldown),
        )

    # ------------------------------------------------------------------- bets
    # ------------------------------------------------------------------ карты
    async def upsert_map(
        self,
        *,
        match_id: int,
        fonbet_id: int,
        map_number: int,
        odds_a: float | None,
        odds_b: float | None,
        score_a: int | None = None,
        score_b: int | None = None,
    ) -> tuple[Any, bool]:
        """Карта матча: кэфы на её победителя + счёт по раундам."""
        now = int(time.time())
        existing = await self._fetchone(
            "SELECT * FROM match_maps WHERE fonbet_id = ?", (fonbet_id,)
        )
        if existing is None:
            await self.conn.execute(
                """
                INSERT INTO match_maps (match_id, fonbet_id, map_number, odds_a, odds_b,
                                        score_a, score_b, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (match_id, fonbet_id, map_number, odds_a, odds_b, score_a, score_b, now),
            )
            await self.conn.commit()
            created = await self._fetchone(
                "SELECT * FROM match_maps WHERE fonbet_id = ?", (fonbet_id,)
            )
            assert created is not None
            return created, True

        if existing["status"] in ("open", "live"):
            await self.conn.execute(
                """
                UPDATE match_maps SET odds_a = ?, odds_b = ?, score_a = ?, score_b = ?
                WHERE id = ?
                """,
                (
                    odds_a if odds_a is not None else existing["odds_a"],
                    odds_b if odds_b is not None else existing["odds_b"],
                    score_a if score_a is not None else existing["score_a"],
                    score_b if score_b is not None else existing["score_b"],
                    existing["id"],
                ),
            )
            await self.conn.commit()
            refreshed = await self._fetchone(
                "SELECT * FROM match_maps WHERE id = ?", (existing["id"],)
            )
            return refreshed or existing, False
        return existing, False

    async def match_maps(self, match_id: int) -> list[Any]:
        return await self._fetchall(
            "SELECT * FROM match_maps WHERE match_id = ? ORDER BY map_number", (match_id,)
        )

    async def maps_for_matches(self, match_ids: Sequence[int]) -> dict[int, list[Any]]:
        """Карты сразу для списка матчей — один запрос на экран."""
        if not match_ids:
            return {}
        placeholders = ",".join("?" for _ in match_ids)
        rows = await self._fetchall(
            f"SELECT * FROM match_maps WHERE match_id IN ({placeholders}) ORDER BY map_number",
            tuple(int(item) for item in match_ids),
        )
        grouped: dict[int, list[Any]] = {}
        for row in rows:
            grouped.setdefault(int(row["match_id"]), []).append(row)
        return grouped

    async def maps_by_status(self, statuses: Sequence[str]) -> list[Any]:
        placeholders = ",".join("?" for _ in statuses)
        return await self._fetchall(
            f"SELECT * FROM match_maps WHERE status IN ({placeholders}) ORDER BY id",
            tuple(statuses),
        )

    async def get_map(self, map_id: int) -> Any | None:
        return await self._fetchone("SELECT * FROM match_maps WHERE id = ?", (map_id,))

    async def set_map_score(self, map_id: int, score_a: int | None, score_b: int | None) -> None:
        """Счёт раундов карты (из внешнего фида): нужен для расчёта и для карточки."""
        await self.conn.execute(
            "UPDATE match_maps SET score_a = ?, score_b = ? WHERE id = ?",
            (score_a, score_b, map_id),
        )
        await self.conn.commit()

    async def set_map_status(self, map_id: int, status: str) -> None:
        await self.conn.execute(
            "UPDATE match_maps SET status = ? WHERE id = ?", (status, map_id)
        )
        await self.conn.commit()

    async def start_maps_of_match(self, match_id: int) -> list[Any]:
        """Матч начался — карты переезжают из линии в live (ставки при live_betting остаются)."""
        rows = await self._fetchall(
            "SELECT * FROM match_maps WHERE match_id = ? AND status = 'open'", (match_id,)
        )
        for row in rows:
            await self.conn.execute(
                "UPDATE match_maps SET status = 'live' WHERE id = ?", (row["id"],)
            )
        await self.conn.commit()
        return rows

    async def place_map_bet(
        self, map_id: int, user_id: int, chat_id: int | None, side: str, amount: int
    ) -> tuple[int, float]:
        """Ставка на победителя конкретной карты. Возвращает (новый баланс, кэф)."""
        if side not in ("a", "b", "x"):
            raise InvalidBet("Сторона должна быть 1, X или 2")
        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM match_maps WHERE id = ?", (map_id,)) as cur:
                card = await cur.fetchone()
            if card is None:
                raise MatchNotFound("Карта не найдена")
            async with conn.execute(
                "SELECT * FROM matches WHERE id = ?", (int(card["match_id"]),)
            ) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")

            # статус матча тут не главный: серия может числиться в «ждём результат»,
            # пока идёт очередная карта. Смотрим на саму карту.
            if str(match["status"]) in ("finished", "cancelled"):
                raise MatchNotOpen("Матч уже рассчитан — ставки на карту закрыты")
            if str(card["status"]) not in ("open", "live"):
                raise MatchNotOpen("Ставки на эту карту закрыты")
            if not settings.live_betting and int(match["start_time"]) <= now:
                raise MatchNotOpen("Матч уже начался — ставки не принимаются")

            odds = card["odds_a"] if side == "a" else card["odds_b"]
            if not odds:
                raise InvalidBet("На этот исход нет кэфа")

            cur = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (amount, user_id, amount),
            )
            if cur.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")

            await conn.execute(
                """
                INSERT INTO bets (match_id, user_id, chat_id, side, amount, odds,
                                  market, map_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'map', ?, ?)
                """,
                (int(match["id"]), user_id, chat_id, side, amount, float(odds), map_id, now),
            )
            await self._ledger(
                conn, user_id, -amount, f"ставка · карта {int(card['map_number'])}", now
            )
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur2:
                row = await cur2.fetchone()
        return (int(row["balance"]) if row else 0), float(odds)

    async def settle_map(
        self, map_id: int, winner: str, source: str = "fonbet"
    ) -> dict[str, Any]:
        """Расчёт карты: платим только ставкам на эту карту."""
        if winner not in ("a", "b"):
            raise InvalidBet("Победитель должен быть 1 или 2")

        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM match_maps WHERE id = ?", (map_id,)) as cur:
                card = await cur.fetchone()
            if card is None:
                raise MatchNotFound("Карта не найдена")
            if str(card["status"]) in ("finished", "cancelled"):
                raise MatchAlreadySettled("Карта уже рассчитана")
            async with conn.execute(
                "SELECT * FROM matches WHERE id = ?", (int(card["match_id"]),)
            ) as cur:
                match = await cur.fetchone()

            async with conn.execute(
                "SELECT * FROM bets WHERE map_id = ? AND market = 'map'", (map_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            paid = 0
            payouts: dict[int, int] = {}
            settlements: list[dict[str, Any]] = []
            for bet in bets:
                amount = int(bet["amount"])
                win = bet["side"] == winner
                payout = round(amount * float(bet["odds"])) if win else 0
                if win:
                    paid += payout
                    payouts[int(bet["user_id"])] = payouts.get(int(bet["user_id"]), 0) + payout
                settlements.append(
                    {
                        "bet_id": int(bet["id"]),
                        "user_id": int(bet["user_id"]),
                        "side": str(bet["side"]),
                        "amount": amount,
                        "odds": float(bet["odds"]),
                        "payout": payout,
                        "won": win,
                    }
                )
                await conn.execute(
                    "UPDATE bets SET payout = ? WHERE id = ?", (payout, bet["id"])
                )

            for user_id, total in payouts.items():
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?", (total, user_id)
                )
                await self._ledger(
                    conn, user_id, total, f"выплата · карта {int(card['map_number'])}"
                )

            await conn.execute(
                "UPDATE match_maps SET status = 'finished', winner = ?, finished_at = ? WHERE id = ?",
                (winner, int(time.time()), map_id),
            )

        team_a = str(match["team_a"]) if match is not None else ""
        team_b = str(match["team_b"]) if match is not None else ""
        return {
            "map_id": map_id,
            "map_number": int(card["map_number"]),
            "team_a": team_a,
            "team_b": team_b,
            "team": team_a if winner == "a" else team_b,
            "paid": paid,
            "bets": len(bets),
            "winners": len(payouts),
            "settlements": settlements,
            "source": source,
        }

    @staticmethod
    def _betting_refusal(match: Any, now: int) -> str | None:
        """None — ставку принять можно; иначе текст отказа для игрока."""
        status = str(match["status"])
        if status == "live":
            return None if settings.live_betting else "Матч уже начался — ставки не принимаются"
        if status == "open":
            if settings.live_betting or int(match["start_time"]) > now:
                return None
            return "Матч уже начался — ставки не принимаются"
        return "Ставки по этому матчу закрыты"

    async def place_bet(
        self, match_id: int, user_id: int, chat_id: int | None, side: str, amount: int
    ) -> tuple[int, float]:
        """Списывает ставку по текущему кэфу матча. Возвращает (новый баланс, кэф)."""
        if side not in ("a", "b", "x"):
            raise InvalidBet("Сторона должна быть 1, X или 2")
        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if side == "x" and str(match["sport"]) != "football":
                raise InvalidBet("Ничья бывает только в футболе")
            refusal = self._betting_refusal(match, now)
            if refusal:
                raise MatchNotOpen(refusal)

            odds = {"a": match["odds_a"], "x": match["odds_x"], "b": match["odds_b"]}[side]
            if not odds:
                raise InvalidBet("На этот исход нет кэфа")

            cur = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (amount, user_id, amount),
            )
            if cur.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")

            await conn.execute(
                """
                INSERT INTO bets (match_id, user_id, chat_id, side, amount, odds, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (match_id, user_id, chat_id, side, amount, float(odds), now),
            )
            await self._ledger(conn, user_id, -amount, f"ставка · матч #{match_id}", now)
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur2:
                row = await cur2.fetchone()
        return (int(row["balance"]) if row else 0), float(odds)

    # --------------------------------------------------------------- футбол
    async def place_football_bet(
        self,
        match_id: int,
        user_id: int,
        chat_id: int | None,
        key: str,
        param: str,
        amount: int,
    ) -> tuple[int, float]:
        """Ставка на рынок футбольного матча (тотал, точный счёт, форы и т.д.).

        Кэф берётся из той же модели, что видит игрок, и фиксируется в ставке —
        дальнейшие пересчёты линии на уже сделанную ставку не влияют.
        """
        from app.services.football import markets_of, side_code

        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if str(match["sport"]) != "football":
                raise InvalidBet("Это не футбольный матч")
            if str(match["status"]) != "open" or int(match["start_time"]) <= now:
                raise MatchNotOpen("Матч начался — роспись закрыта")

            market = next(
                (m for m in markets_of(match) if m["key"] == key and m["param"] == param), None
            )
            if market is None:
                raise InvalidBet("Такого рынка нет в линии")
            odds = float(market["odds"])

            cur = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (amount, user_id, amount),
            )
            if cur.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")

            await conn.execute(
                """
                INSERT INTO bets (match_id, user_id, chat_id, side, amount, odds, market, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'football', ?)
                """,
                (match_id, user_id, chat_id, side_code(key, param), amount, odds, now),
            )
            await self._ledger(conn, user_id, -amount, f"ставка · {market['label']} · матч #{match_id}", now)
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur2:
                row = await cur2.fetchone()
        return (int(row["balance"]) if row else 0), odds

    async def settle_football(
        self, match_id: int, score_a: int, score_b: int, source: str = "auto"
    ) -> dict[str, Any]:
        """Расчёт футбольного матча по счёту: исход, тоталы, форы, точный счёт."""
        from app.services.football import market_label, market_outcome, parse_side

        if score_a == score_b:
            winner = "x"
        elif score_a > score_b:
            winner = "a"
        else:
            winner = "b"

        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if str(match["status"]) in ("finished", "cancelled"):
                raise MatchAlreadySettled("Матч уже рассчитан или отменён")

            async with conn.execute("SELECT * FROM bets WHERE match_id = ?", (match_id,)) as cur:
                bets = list(await cur.fetchall())

            total_stake = sum(int(bet["amount"]) for bet in bets)
            paid = 0
            refunded = 0
            payouts: dict[int, int] = {}
            settlements: list[dict[str, Any]] = []

            for bet in bets:
                amount = int(bet["amount"])
                side = str(bet["side"])
                parsed = parse_side(side)
                if parsed is None:
                    outcome = "win" if side == winner else "lose"
                    label = "П1" if side == "a" else ("Ничья" if side == "x" else "П2")
                else:
                    outcome = market_outcome(parsed[0], parsed[1], score_a, score_b)
                    label = market_label(parsed[0], parsed[1])

                if outcome == "win":
                    payout = round(amount * float(bet["odds"]))
                elif outcome == "void":
                    payout = amount
                    refunded += payout
                else:
                    payout = 0
                if payout:
                    paid += payout
                    payouts[int(bet["user_id"])] = payouts.get(int(bet["user_id"]), 0) + payout
                settlements.append(
                    {
                        "bet_id": int(bet["id"]),
                        "user_id": int(bet["user_id"]),
                        "chat_id": int(bet["chat_id"]) if bet["chat_id"] else None,
                        "side": side,
                        "label": label,
                        "amount": amount,
                        "odds": float(bet["odds"]),
                        "payout": payout,
                        "won": outcome == "win",
                        "void": outcome == "void",
                    }
                )
                await conn.execute("UPDATE bets SET payout = ? WHERE id = ?", (payout, bet["id"]))

            for user_id, total in payouts.items():
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?", (total, user_id)
                )
                await self._ledger(conn, user_id, total, f"выплата · матч #{match_id}")

            await conn.execute(
                """
                UPDATE matches SET status = 'finished', winner = ?, result_source = ?,
                                   score_a = ?, score_b = ?, finished_at = ?
                WHERE id = ?
                """,
                (winner, source, score_a, score_b, int(time.time()), match_id),
            )
            expresses = await self._resolve_expresses(conn, match_id, winner)

        return {
            "team": str(match["team_a"] if winner == "a" else match["team_b"]),
            "score": f"{score_a}:{score_b}",
            "winner": winner,
            "paid": paid,
            "refunded": refunded,
            "stake": total_stake,
            "bets": len(bets),
            "winners": len(payouts),
            "payouts": payouts,
            "settlements": settlements,
            "team_a": str(match["team_a"]),
            "team_b": str(match["team_b"]),
            "expresses": expresses,
        }

    # -------------------------------------------------------------- экспрессы
    async def place_express(
        self,
        user_id: int,
        chat_id: int | None,
        stake: int,
        legs: list[dict[str, Any]],
        *,
        max_odds: float | None = None,
    ) -> tuple[int, float, int]:
        """Ставит экспресс. legs: [{'match_id': int, 'side': 'a'|'b'}].

        Возвращает (express_id, итоговый кэф, новый баланс).
        """
        if len(legs) < 2:
            raise InvalidBet("В экспрессе должно быть минимум 2 исхода")
        if stake <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        match_ids = [int(leg["match_id"]) for leg in legs]
        if len(set(match_ids)) != len(match_ids):
            raise InvalidBet("Нельзя брать два исхода одного матча в один экспресс")

        now = int(time.time())
        async with self.transaction() as conn:
            total_odds = 1.0
            prepared: list[tuple[int, str, float]] = []
            for leg in legs:
                side = leg["side"]
                if side not in ("a", "b"):
                    raise InvalidBet("Исход должен быть 1 или 2")
                async with conn.execute(
                    "SELECT * FROM matches WHERE id = ?", (int(leg["match_id"]),)
                ) as cur:
                    match = await cur.fetchone()
                if match is None:
                    raise MatchNotFound(f"Матч #{leg['match_id']} не найден")
                refusal = self._betting_refusal(match, now)
                if refusal:
                    raise MatchNotOpen(f"Матч #{match['id']}: {refusal.lower()}")
                odds = match["odds_a"] if side == "a" else match["odds_b"]
                if not odds:
                    raise InvalidBet(f"На матч #{match['id']} нет кэфа на этот исход")
                total_odds *= float(odds)
                prepared.append((int(match["id"]), side, float(odds)))

            if max_odds and total_odds > max_odds:  # 0/None — потолка нет
                raise InvalidBet(
                    f"Итоговый кэф {total_odds:.2f} выше лимита {max_odds:.0f} — убери лишние исходы"
                )

            cur = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (stake, user_id, stake),
            )
            if cur.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")

            cur = await conn.execute(
                "INSERT INTO expresses (user_id, chat_id, stake, odds, status, created_at) "
                "VALUES (?, ?, ?, ?, 'open', ?) RETURNING id",
                (user_id, chat_id, stake, round(total_odds, 3), now),
            )
            created = await cur.fetchone()
            express_id = int(created["id"]) if created else int(cur.lastrowid or 0)

            for match_id, side, odds in prepared:
                await conn.execute(
                    "INSERT INTO express_items (express_id, match_id, side, odds, result) VALUES (?, ?, ?, ?, 'pending')",
                    (express_id, match_id, side, odds),
                )

            await self._ledger(conn, user_id, -stake, f"экспресс #{express_id}", now)
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur2:
                row = await cur2.fetchone()

        return express_id, round(total_odds, 3), (int(row["balance"]) if row else 0)

    async def get_express(self, express_id: int) -> Any | None:
        return await self._fetchone("SELECT * FROM expresses WHERE id = ?", (express_id,))

    async def express_items(self, express_id: int) -> list[Any]:
        return await self._fetchall(
            """
            SELECT i.*, m.team_a, m.team_b, m.status AS match_status, m.winner AS match_winner
            FROM express_items i JOIN matches m ON m.id = i.match_id
            WHERE i.express_id = ? ORDER BY i.id
            """,
            (express_id,),
        )

    async def user_open_expresses(self, user_id: int) -> list[Any]:
        return await self._fetchall(
            "SELECT * FROM expresses WHERE user_id = ? AND status = 'open' ORDER BY id",
            (user_id,),
        )

    async def _resolve_expresses(
        self, conn: Any, match_id: int, winner: str | None
    ) -> list[dict[str, Any]]:
        """Обновляет ноги экспрессов после расчёта матча (winner=None — матч отменён)."""
        async with conn.execute(
            """
            SELECT i.* FROM express_items i
            JOIN expresses e ON e.id = i.express_id
            WHERE i.match_id = ? AND e.status = 'open'
            """,
            (match_id,),
        ) as cur:
            items = list(await cur.fetchall())

        affected: set[int] = set()
        for item in items:
            result = "void" if winner is None else ("won" if item["side"] == winner else "lost")
            await conn.execute(
                "UPDATE express_items SET result = ? WHERE id = ?", (result, item["id"])
            )
            affected.add(int(item["express_id"]))

        resolved: list[dict[str, Any]] = []
        now = int(time.time())

        for express_id in affected:
            async with conn.execute(
                "SELECT * FROM expresses WHERE id = ?", (express_id,)
            ) as cur:
                express = await cur.fetchone()
            if express is None or express["status"] != "open":
                continue

            async with conn.execute(
                """
                SELECT i.*, m.team_a, m.team_b FROM express_items i
                JOIN matches m ON m.id = i.match_id
                WHERE i.express_id = ?
                """,
                (express_id,),
            ) as cur:
                legs = list(await cur.fetchall())

            if any(leg["result"] == "lost" for leg in legs):
                status, payout = "lost", 0
            elif all(leg["result"] in ("won", "void") for leg in legs):
                if all(leg["result"] == "void" for leg in legs):
                    status, payout = "void", int(express["stake"])  # полный возврат
                else:
                    effective = 1.0
                    for leg in legs:
                        effective *= float(leg["odds"]) if leg["result"] == "won" else 1.0
                    status, payout = "won", round(int(express["stake"]) * effective)
            else:
                continue  # часть ног ещё не рассчитана

            await conn.execute(
                "UPDATE expresses SET status = ?, payout = ?, settled_at = ? WHERE id = ?",
                (status, payout, now, express_id),
            )
            if payout:
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (payout, int(express["user_id"])),
                )
                reason = f"экспресс #{express_id} · {'возврат' if status == 'void' else 'выплата'}"
                await self._ledger(conn, int(express["user_id"]), payout, reason, now)

            lost_leg = next((leg for leg in legs if leg["result"] == "lost"), None)
            resolved.append(
                {
                    "express_id": express_id,
                    "user_id": int(express["user_id"]),
                    "chat_id": int(express["chat_id"]) if express["chat_id"] else None,
                    "status": status,
                    "stake": int(express["stake"]),
                    "odds": float(express["odds"]),
                    "payout": payout,
                    "legs": len(legs),
                    "lost_leg": (
                        f"{lost_leg['team_a']} — {lost_leg['team_b']}" if lost_leg else None
                    ),
                }
            )
        return resolved

    async def match_bets(self, match_id: int) -> list[Any]:
        return await self._fetchall(
            "SELECT * FROM bets WHERE match_id = ? ORDER BY id", (match_id,)
        )

    async def chat_ids_with_bets(self, match_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT DISTINCT chat_id FROM bets WHERE match_id = ? AND chat_id IS NOT NULL",
            (match_id,),
        )
        return [int(row["chat_id"]) for row in rows]

    async def user_active_bets(self, user_id: int) -> list[Any]:
        return await self._fetchall(
            """
            SELECT b.*, m.team_a, m.team_b, m.start_time, m.status, m.fonbet_id,
                   mm.map_number, mm.status AS map_status
            FROM bets b
            JOIN matches m ON m.id = b.match_id
            LEFT JOIN match_maps mm ON mm.id = b.map_id
            WHERE b.user_id = ? AND (
                (b.market = 'match' AND m.status IN ('open', 'live', 'awaiting'))
                OR (b.market = 'map' AND mm.status IN ('open', 'live', 'awaiting'))
            )
            ORDER BY m.start_time
            """,
            (user_id,),
        )

    # --------------------------------------------------- история ставок игрока
    async def user_bets_all(self, user_id: int, limit: int = 50) -> list[Any]:
        """Все одиночные ставки игрока + матч, новые сверху."""
        return await self._fetchall(
            """
            SELECT b.id, b.match_id, b.side, b.amount, b.odds, b.payout, b.created_at,
                   b.market, mm.map_number,
                   m.tournament, m.team_a, m.team_b, m.start_time,
                   m.status AS match_status, m.winner, m.finished_at,
                   mm.status AS map_status, mm.winner AS map_winner
            FROM bets b
            JOIN matches m ON m.id = b.match_id
            LEFT JOIN match_maps mm ON mm.id = b.map_id
            WHERE b.user_id = ?
            ORDER BY b.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )

    async def user_expresses_all(self, user_id: int, limit: int = 20) -> list[Any]:
        """Все экспрессы игрока, новые сверху."""
        return await self._fetchall(
            """
            SELECT id, stake, odds, status, payout, created_at, settled_at
            FROM expresses
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )

    async def express_legs_many(self, express_ids: Sequence[int]) -> list[Any]:
        """Ноги экспрессов одним запросом (для списка истории ставок)."""
        if not express_ids:
            return []
        placeholders = ",".join("?" for _ in express_ids)
        return await self._fetchall(
            f"""
            SELECT i.express_id, i.match_id, i.side, i.odds, i.result,
                   m.team_a, m.team_b, m.status AS match_status, m.winner
            FROM express_items i
            JOIN matches m ON m.id = i.match_id
            WHERE i.express_id IN ({placeholders})
            ORDER BY i.id
            """,
            tuple(int(item) for item in express_ids),
        )

    async def settle_match(
        self, match_id: int, winner: str, source: str = "manual"
    ) -> dict[str, Any]:
        """Выплата по фиксированным кэфам: amount * odds угадавшим."""
        if winner not in ("a", "b"):
            raise InvalidBet("Победитель должен быть 1 или 2")

        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if match["status"] in ("finished", "cancelled"):
                raise MatchAlreadySettled("Матч уже рассчитан или отменён")

            # ставки на конкретные карты рассчитываются отдельно, по своей карте
            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ? AND market = 'match'", (match_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            total_stake = sum(int(bet["amount"]) for bet in bets)
            paid = 0
            payouts: dict[int, int] = {}
            settlements: list[dict[str, Any]] = []

            for bet in bets:
                amount = int(bet["amount"])
                win = bet["side"] == winner
                payout = round(amount * float(bet["odds"])) if win else 0
                if win:
                    paid += payout
                    payouts[int(bet["user_id"])] = payouts.get(int(bet["user_id"]), 0) + payout
                settlements.append(
                    {
                        "bet_id": int(bet["id"]),
                        "user_id": int(bet["user_id"]),
                        "chat_id": int(bet["chat_id"]) if bet["chat_id"] else None,
                        "side": str(bet["side"]),
                        "amount": amount,
                        "odds": float(bet["odds"]),
                        "payout": payout,
                        "won": win,
                    }
                )
                await conn.execute(
                    "UPDATE bets SET payout = ? WHERE id = ?", (payout, bet["id"])
                )

            for user_id, total in payouts.items():
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?", (total, user_id)
                )
                await self._ledger(conn, user_id, total, f"выплата · матч #{match_id}")

            await conn.execute(
                """
                UPDATE matches SET status = 'finished', winner = ?, result_source = ?, finished_at = ?
                WHERE id = ?
                """,
                (winner, source, int(time.time()), match_id),
            )

            expresses = await self._resolve_expresses(conn, match_id, winner)

        team = str(match["team_a"] if winner == "a" else match["team_b"])
        return {
            "team": team,
            "paid": paid,
            "stake": total_stake,
            "bets": len(bets),
            "winners": len(payouts),
            "payouts": payouts,
            "settlements": settlements,
            "team_a": str(match["team_a"]),
            "team_b": str(match["team_b"]),
            "expresses": expresses,
        }

    async def refunded_maps_with_score(self) -> list[Any]:
        """Карты, которые вернули как «не сыгранные», хотя счёт раундов у них есть."""
        return await self._fetchall(
            "SELECT * FROM match_maps WHERE status = 'cancelled' "
            "AND score_a IS NOT NULL AND score_b IS NOT NULL ORDER BY id"
        )

    async def recount_map(self, map_id: int, winner: str | None = None) -> dict[str, Any]:
        """Возвращённую карту пересчитываем по факту: откат возвратов + выплаты.

        `winner` необязателен: если не задан, победитель берётся из счёта раундов.
        """
        from app.services.cs2_rules import map_winner

        card = await self.get_map(map_id)
        if card is None:
            raise MatchNotFound("Карта не найдена")
        if str(card["status"]) != "cancelled":
            raise BettingError("Пересчитать можно только возвращённую карту")
        winner = winner or map_winner(card["score_a"], card["score_b"])
        if winner not in ("a", "b"):
            raise BettingError("По счёту победителя карты не видно")

        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM bets WHERE map_id = ? AND market = 'map'", (map_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            reversed_total = 0
            for bet in bets:
                payout = int(bet["payout"] or 0)
                if not payout:
                    continue
                reversed_total += payout
                async with conn.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (int(bet["user_id"]),)
                ) as cur:
                    user = await cur.fetchone()
                balance = int(user["balance"]) if user else 0
                await conn.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (max(0, balance - payout), int(bet["user_id"])),
                )
                await self._ledger(
                    conn,
                    int(bet["user_id"]),
                    -payout,
                    f"откат возврата · карта {int(card['map_number'])}",
                )
                await conn.execute("UPDATE bets SET payout = NULL WHERE id = ?", (int(bet["id"]),))

            await conn.execute(
                "UPDATE match_maps SET status = 'live', winner = NULL, finished_at = NULL "
                "WHERE id = ?",
                (map_id,),
            )

        result = await self.settle_map(map_id, winner, source="recount")
        result["reversed"] = reversed_total
        return result

    async def cancel_map(self, map_id: int, reason: str = "карта не игралась") -> dict[str, Any]:
        """Возврат ставок на карту, которую так и не сыграли (например, серия закончилась 2:0)."""
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM match_maps WHERE id = ?", (map_id,)) as cur:
                card = await cur.fetchone()
            if card is None:
                raise MatchNotFound("Карта не найдена")
            if str(card["status"]) in ("finished", "cancelled"):
                raise MatchAlreadySettled("Карта уже рассчитана")

            async with conn.execute(
                "SELECT * FROM bets WHERE map_id = ? AND market = 'map'", (map_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            refunded = 0
            settlements: list[dict[str, Any]] = []
            for bet in bets:
                amount = int(bet["amount"])
                refunded += amount
                settlements.append(
                    {
                        "bet_id": int(bet["id"]),
                        "user_id": int(bet["user_id"]),
                        "side": str(bet["side"]),
                        "amount": amount,
                        "odds": float(bet["odds"]),
                        "payout": amount,
                        "won": False,
                    }
                )
                await conn.execute(
                    "UPDATE bets SET payout = ? WHERE id = ?", (amount, bet["id"])
                )
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (amount, int(bet["user_id"])),
                )
                await self._ledger(
                    conn, int(bet["user_id"]), amount,
                    f"возврат · карта {int(card['map_number'])}",
                )

            await conn.execute(
                "UPDATE match_maps SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (int(time.time()), map_id),
            )

        return {
            "map_id": map_id,
            "map_number": int(card["map_number"]),
            "match_id": int(card["match_id"]),
            "refunded": refunded,
            "bets": len(bets),
            "settlements": settlements,
            "reason": reason,
        }

    async def cancel_match(self, match_id: int) -> dict[str, Any]:
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if match["status"] in ("finished", "cancelled"):
                raise MatchAlreadySettled("Матч уже рассчитан или отменён")

            # ставки на конкретные карты рассчитываются отдельно, по своей карте
            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ? AND market = 'match'", (match_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            refunded = 0
            settlements: list[dict[str, Any]] = []
            for bet in bets:
                amount = int(bet["amount"])
                refunded += amount
                settlements.append(
                    {
                        "bet_id": int(bet["id"]),
                        "user_id": int(bet["user_id"]),
                        "chat_id": int(bet["chat_id"]) if bet["chat_id"] else None,
                        "side": str(bet["side"]),
                        "amount": amount,
                        "odds": float(bet["odds"]),
                        "payout": amount,
                        "won": False,
                    }
                )
                await conn.execute(
                    "UPDATE bets SET payout = ? WHERE id = ?", (amount, bet["id"])
                )
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (amount, int(bet["user_id"])),
                )
                await self._ledger(conn, int(bet["user_id"]), amount, f"возврат · матч #{match_id}")

            await conn.execute(
                "UPDATE matches SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (int(time.time()), match_id),
            )

            expresses = await self._resolve_expresses(conn, match_id, None)

        return {
            "refunded": refunded,
            "bets": len(bets),
            "settlements": settlements,
            "team_a": str(match["team_a"]),
            "team_b": str(match["team_b"]),
            "expresses": expresses,
        }

    async def resettle_football(
        self, match_id: int, score_a: int, score_b: int, source: str = "manual"
    ) -> dict[str, Any]:
        """Пересчитать УЖЕ рассчитанный футбольный матч по правильному счёту.

        Нужно, когда финальный счёт пришёл позже (гол на 90+4, добавленное время)
        или когда матч посчитали по последнему live-счёту. Возвраты/выплаты
        откатываются, матч считается заново — уже по фактическому счёту.
        """
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")

            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ? AND market <> 'map'", (match_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            reversed_total = 0
            for bet in bets:
                payout = int(bet["payout"] or 0)
                if not payout:
                    continue
                reversed_total += payout
                async with conn.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (int(bet["user_id"]),)
                ) as cur:
                    user = await cur.fetchone()
                balance = int(user["balance"]) if user else 0
                await conn.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (max(0, balance - payout), int(bet["user_id"])),
                )
                await self._ledger(
                    conn, int(bet["user_id"]), -payout, f"сторно расчёта · матч #{match_id}"
                )
                await conn.execute("UPDATE bets SET payout = NULL WHERE id = ?", (int(bet["id"]),))

            await conn.execute(
                "UPDATE matches SET status = 'live', winner = NULL, finished_at = NULL, "
                "result_source = NULL, score_a = ?, score_b = ? WHERE id = ?",
                (score_a, score_b, match_id),
            )

        result = await self.settle_football(match_id, score_a, score_b, source=source)
        result["reversed"] = reversed_total
        return result

    async def cancel_football_match(self, match_id: int, reason: str = "матч не состоялся") -> dict[str, Any]:
        """Матч не состоялся (перенесён/отменён) — возвращаем ставки по всем рынкам.

        У футбола ставки лежат с `market = 'football'`, поэтому `cancel_match`
        (он про CS2) их не видит: возврат уходил бы в пустоту.
        """
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if match["status"] in ("finished", "cancelled"):
                raise MatchAlreadySettled("Матч уже рассчитан или отменён")

            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ? AND market <> 'map'", (match_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            refunded = 0
            settlements: list[dict[str, Any]] = []
            for bet in bets:
                amount = int(bet["amount"])
                refunded += amount
                settlements.append(
                    {
                        "bet_id": int(bet["id"]),
                        "user_id": int(bet["user_id"]),
                        "chat_id": int(bet["chat_id"]) if bet["chat_id"] else None,
                        "side": str(bet["side"]),
                        "amount": amount,
                        "odds": float(bet["odds"]),
                        "payout": amount,
                        "won": False,
                    }
                )
                await conn.execute("UPDATE bets SET payout = ? WHERE id = ?", (amount, bet["id"]))
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (amount, int(bet["user_id"])),
                )
                await self._ledger(
                    conn, int(bet["user_id"]), amount, f"возврат · матч #{match_id} ({reason})"
                )

            await conn.execute(
                "UPDATE matches SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (int(time.time()), match_id),
            )

            expresses = await self._resolve_expresses(conn, match_id, None)

        return {
            "refunded": refunded,
            "bets": len(bets),
            "settlements": settlements,
            "team_a": str(match["team_a"]),
            "team_b": str(match["team_b"]),
            "expresses": expresses,
        }

    async def recount_match(
        self, match_id: int, winner: str, *, allow_express_legs: bool = False
    ) -> dict[str, Any]:
        """Отменённый матч считаем заново по реальному исходу: откат возвратов + выплаты.

        `allow_express_legs` — считать одиночные ставки, даже если матч был ногой
        экспресса. Экспрессы, уже возвращённые при отмене, не пересчитываются: людям
        деньги вернули, забирать их назад нельзя.
        """
        match = await self.get_match(match_id)
        if match is None:
            raise MatchNotFound("Матч не найден")
        if str(match["status"]) != "cancelled":
            raise BettingError("Пересчитать можно только отменённый матч")
        return await self.resettle_match(
            match_id, winner, allow_express_legs=allow_express_legs, source="recount"
        )

    async def resettle_match(
        self,
        match_id: int,
        winner: str,
        *,
        allow_express_legs: bool = False,
        source: str = "recheck",
    ) -> dict[str, Any]:
        """Пересчитать матч по другому исходу: откат прошлых выплат и новый расчёт.

        Нужно, когда исход выяснился позже (или раньше посчитали неверно): выплаты
        откатываются, матч считается заново по факту.
        """
        if winner not in ("a", "b"):
            raise InvalidBet("Победитель должен быть 1 или 2")

        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")

            async with conn.execute(
                "SELECT COUNT(*) AS c FROM express_items WHERE match_id = ?", (match_id,)
            ) as cur:
                in_express = int((await cur.fetchone())["c"])
            if in_express and not allow_express_legs:
                raise BettingError("Матч входит в экспресс — такой пересчёт не поддержан")

            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ? AND market = 'match'", (match_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            reversed_total = 0
            for bet in bets:
                payout = int(bet["payout"] or 0)
                if not payout:
                    continue
                reversed_total += payout
                async with conn.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (int(bet["user_id"]),)
                ) as cur:
                    user = await cur.fetchone()
                balance = int(user["balance"]) if user else 0
                await conn.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (max(0, balance - payout), int(bet["user_id"])),
                )
                await self._ledger(
                    conn, int(bet["user_id"]), -payout, f"откат возврата · матч #{match_id}"
                )
                await conn.execute("UPDATE bets SET payout = NULL WHERE id = ?", (int(bet["id"]),))

            await conn.execute(
                "UPDATE matches SET status = 'live', winner = NULL, finished_at = NULL, "
                "result_source = NULL WHERE id = ?",
                (match_id,),
            )

        result = await self.settle_match(match_id, winner, source=source)
        result["reversed"] = reversed_total
        return result

    # ------------------------------------------------------------------ stats
    async def stats(self) -> dict[str, int]:
        users = await self._fetchone("SELECT COUNT(*) AS c FROM users")
        balance_sum = await self._fetchone("SELECT COALESCE(SUM(balance), 0) AS s FROM users")
        matches = await self._fetchone(
            "SELECT COUNT(*) AS c FROM matches WHERE status IN ('open', 'live', 'awaiting')"
        )
        bets = await self._fetchone(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount), 0) AS s FROM bets"
        )
        return {
            "users": int(users["c"]) if users else 0,
            "balance_sum": int(balance_sum["s"]) if balance_sum else 0,
            "active_matches": int(matches["c"]) if matches else 0,
            "bets": int(bets["c"]) if bets else 0,
            "bets_sum": int(bets["s"]) if bets else 0,
        }

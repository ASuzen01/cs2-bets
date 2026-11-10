"""Слой данных: SQLite (локально) или Postgres/Neon (хостинг).

Модель:
  matches — реальные матчи CS2 из линии Fonbet (один ряд на событие), кэфы на победителя;
  bets    — ставки с зафиксированным на момент ставки кэфом (fixed odds);
  users   — виртуальные балансы;
  ledger  — история операций.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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

from app.services.quests import by_code, quests_of_day  # noqa: E402  (после log — нужен для логов)

STREAK_STEP: float = 0.1      # каждый день серии даёт +10 % к бонусу
STREAK_CAP: float = 1.5       # дальше не растёт: 15 дней — потолок
BET_REQUEST_RETENTION_SECONDS = 7 * 24 * 60 * 60  # сетевые повторы защищаем неделю
BET_REQUEST_PRUNE_INTERVAL_SECONDS = 60 * 60


def bonus_amount(base: int, streak: int) -> int:
    """Бонус с учётом серии: 1 день — база, 5 дней — +40 %, с 6-го — +50 %."""
    multiplier = min(STREAK_CAP, 1 + STREAK_STEP * max(0, streak - 1))
    return round(base * multiplier)

# колонки, добавленные после первых деплоев: дорисовываем их в старых базах
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # билеты: вид (экспресс/ординар) и срок жизни фрибета
    ("tickets", "kind", "ALTER TABLE tickets ADD COLUMN kind TEXT NOT NULL DEFAULT 'express'"),
    ("tickets", "expires_at", "ALTER TABLE tickets ADD COLUMN expires_at INTEGER"),
    ("tickets", "bet_id", "ALTER TABLE tickets ADD COLUMN bet_id INTEGER"),
    ("matches", "score_a", "ALTER TABLE matches ADD COLUMN score_a INTEGER"),
    ("matches", "score_b", "ALTER TABLE matches ADD COLUMN score_b INTEGER"),
    ("bets", "market", "ALTER TABLE bets ADD COLUMN market TEXT NOT NULL DEFAULT 'match'"),
    ("bets", "map_id", "ALTER TABLE bets ADD COLUMN map_id INTEGER"),
    ("users", "last_chat_id", "ALTER TABLE users ADD COLUMN last_chat_id BIGINT"),
    ("matches", "sport", "ALTER TABLE matches ADD COLUMN sport TEXT NOT NULL DEFAULT 'cs2'"),
    ("matches", "odds_x", "ALTER TABLE matches ADD COLUMN odds_x REAL"),
    ("matches", "team_a_en", "ALTER TABLE matches ADD COLUMN team_a_en TEXT"),
    ("matches", "team_b_en", "ALTER TABLE matches ADD COLUMN team_b_en TEXT"),
    # хоккей: score_a/score_b — итоговый счёт (с ОТ и буллитами), reg_* — основное время
    ("users", "streak_days", "ALTER TABLE users ADD COLUMN streak_days INTEGER NOT NULL DEFAULT 0"),
    ("users", "bonus_day", "ALTER TABLE users ADD COLUMN bonus_day INTEGER NOT NULL DEFAULT 0"),
    ("matches", "reg_a", "ALTER TABLE matches ADD COLUMN reg_a INTEGER"),
    ("matches", "reg_b", "ALTER TABLE matches ADD COLUMN reg_b INTEGER"),
    # продажа ставки до старта: признак, что деньги уже вернулись
    ("bets", "cashed_out", "ALTER TABLE bets ADD COLUMN cashed_out INTEGER NOT NULL DEFAULT 0"),
    ("casino_bets", "result_json", "ALTER TABLE casino_bets ADD COLUMN result_json TEXT"),
    ("slot_states", "bonus_json", "ALTER TABLE slot_states ADD COLUMN bonus_json TEXT"),
    ("tease_claims", "tap_count", "ALTER TABLE tease_claims ADD COLUMN tap_count INTEGER NOT NULL DEFAULT 0"),
    ("tease_claims", "tap_at", "ALTER TABLE tease_claims ADD COLUMN tap_at INTEGER NOT NULL DEFAULT 0"),
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
    "casino_bets",
    "drunkard_games",
    "slot_states",
    "ladder_games",
    "blackjack_games",
    "crash_games",
    "mines_games",
    "quests",
    "quest_chain",
    "achievements",
    "referrals",
    "tickets",
    "events",
    "app_meta",
    "bet_requests",
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
    ("casino_bets", "user_id"),
    ("drunkard_games", "creator_id"),
    ("drunkard_games", "opponent_id"),
    ("slot_states", "user_id"),
    ("ladder_games", "user_id"),
    ("blackjack_games", "user_id"),
    ("crash_games", "user_id"),
    ("mines_games", "user_id"),
    ("achievements", "user_id"),
    ("quest_chain", "user_id"),
    ("referrals", "invited_id"),
    ("referrals", "inviter_id"),
    ("tickets", "user_id"),
    ("bet_requests", "user_id"),
    ("events", "user_id"),
)


async def _match_of_card(conn: Any, card: Any) -> Any:
    """Строка матча для текста леджера, когда на руках только карта."""
    async with conn.execute(
        "SELECT team_a, team_b FROM matches WHERE id = ?", (int(card["match_id"]),)
    ) as cur:
        return await cur.fetchone()


def match_tag(match: Any) -> str:
    """Человеческая метка матча для леджера: «Арсенал — Челси» вместо «матч #5».

    Леджер — единственное место, где игрок видит движения денег, и внутренний id
    матча там ничего не значит. Имя команды в истории полезнее.
    """
    if match is None:
        return "матч"
    team_a, team_b = match["team_a"], match["team_b"]
    if team_a and team_b:
        return f"{team_a} — {team_b}"
    return "матч"


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
        telegram_snapshot: Any | None = None,
    ) -> None:
        self._path = Path(path) if path else Path("data/bot.db")
        self._url = (url or "").strip()
        self._schema = (schema or "").strip()
        self._telegram_snapshot = telegram_snapshot
        self.start_balance = start_balance
        self._backend: SQLiteBackend | PostgresBackend | None = None
        # одно соединение на процесс: записи сериализуем (см. transaction())
        self._write_lock = asyncio.Lock()
        self._tx_owner: asyncio.Task[Any] | None = None
        self._tx_depth = 0
        self._last_bet_request_prune = 0

    # ------------------------------------------------------------------ infra
    @property
    def conn(self) -> SQLiteBackend | PostgresBackend:
        if self._backend is None:
            raise RuntimeError("Database.connect() не был вызван")
        return self._backend

    async def connect(self) -> None:
        if self._telegram_snapshot is not None and self._url.startswith(("postgres://", "postgresql://")):
            raise RuntimeError("Telegram snapshot mode and PostgreSQL DATABASE_URL cannot be active together")
        if self._url.startswith(("postgres://", "postgresql://")):
            backend = await PostgresBackend.open(self._url, schema=self._schema)
            await backend.executescript(SCHEMA_POSTGRES)
            await self._migrate(backend)
            await self._widen_ids(backend)
            self._backend = backend
            return

        if self._telegram_snapshot is not None:
            await self._telegram_snapshot.restore_to_path(self._path)
        backend = await SQLiteBackend.open(self._path)
        # миграция старой схемы (ручные матчи) — ДО создания индексов новой схемы
        if await self._has_incompatible_matches(backend):
            await backend.executescript("DROP TABLE IF EXISTS bets; DROP TABLE IF EXISTS matches;")
        await backend.executescript(SCHEMA_SQLITE)
        await self._migrate(backend)
        await backend.commit()
        self._backend = backend
        if self._telegram_snapshot is not None:
            try:
                await self._telegram_snapshot.persist_connection(backend._conn)
                backend.set_commit_hook(self._telegram_snapshot.schedule_persist_connection)
            except Exception:
                await backend.close()
                self._backend = None
                raise

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
            if self._telegram_snapshot is not None:
                await self._telegram_snapshot.flush_pending()
            await self._backend.close()
            self._backend = None

    @asynccontextmanager
    async def transaction(self, *, isolation: str | None = None) -> AsyncIterator[Any]:
        """Транзакция. Записи идут строго по одной.

        На процесс одно соединение с базой, поэтому два одновременных `BEGIN`
        (два игрока жмут «поставить» в одну секунду) роняли запрос с
        «cannot start a transaction within a transaction» — то есть 500 у игрока.
        Замок сериализует записи: очередь в памяти дешевле потерянной ставки.

        Вложенный `transaction()` из той же задачи не открывает второй `BEGIN`:
        он просто продолжает внешнюю транзакцию — так расчёт матча может звать
        общие методы, не думая, кто начал транзакцию.
        """
        task = asyncio.current_task()
        if self._tx_owner is task and self._tx_depth:
            self._tx_depth += 1
            try:
                yield self.conn
            finally:
                self._tx_depth -= 1
            return

        async with self._write_lock:
            conn = self.conn
            self._tx_owner = task
            self._tx_depth = 1
            try:
                raw = getattr(conn, "_conn", None)
                if getattr(raw, "in_transaction", False):
                    # висящая транзакция от прошлого запроса: спасаем соединение
                    log.warning("DB: незакрытая транзакция перед BEGIN — откатываю")
                    await conn.rollback()
                begin_sql = conn.begin_sql
                if getattr(conn, "name", "") == "postgres" and isolation:
                    if isolation.upper() != "REPEATABLE READ":
                        raise ValueError("Поддерживается только REPEATABLE READ")
                    begin_sql = "BEGIN ISOLATION LEVEL REPEATABLE READ"
                await conn.execute(begin_sql)
                if getattr(conn, "name", "") == "postgres":
                    # два инстанса на одном Postgres: замок в памяти не поможет,
                    # берём advisory-замок транзакции — он снимается сам на COMMIT
                    await conn.execute("SELECT pg_advisory_xact_lock(918273645)")
                try:
                    yield conn
                except BaseException:
                    try:
                        await conn.rollback()
                    except Exception:
                        log.exception("Не удалось откатить транзакцию после ошибки")
                    raise
                else:
                    await conn.commit()
            finally:
                self._tx_depth = 0
                self._tx_owner = None

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        """Одиночная запись с тем же замком, что и транзакция: без гонок.

        Возвращает результат запроса (`lastrowid`/`rowcount`), как обычный execute.
        Внутри уже открытой транзакции просто выполняет запрос — коммитит внешняя.
        """
        if self._tx_owner is asyncio.current_task() and self._tx_depth:
            async with self.conn.execute(sql, params) as cur:
                return cur
        async with self._write_lock:
            async with self.conn.execute(sql, params) as cur:
                result = cur
            await self.conn.commit()
            return result

    # ------------------------------------------------- идемпотентность ставок
    @staticmethod
    def _clean_token(token: str | None) -> str:
        """Токен тапа: короткая строка от клиента, всё остальное игнорируем."""
        if not token:
            return ""
        text = str(token).strip()[:80]
        return text if text.isascii() else ""

    async def _claim_token(
        self, conn: Any, user_id: int, channel: str, token: str, now: int
    ) -> int | None:
        """Первый запрос с этим токеном выигрывает; повторный получает id прошлой ставки.

        Нужно, чтобы двойной тап на медленной сети не списывал деньги дважды.
        Возвращает None, если действие новое (можно продолжать), иначе id ставки/раунда.
        """
        if now - self._last_bet_request_prune >= BET_REQUEST_PRUNE_INTERVAL_SECONDS:
            # Токены нужны только для повторов сетевого запроса. Храним неделю;
            # события winback/admin_ticket и прочие idempotency-ключи не трогаем.
            await conn.execute(
                "DELETE FROM bet_requests WHERE created_at < ?",
                (now - BET_REQUEST_RETENTION_SECONDS,),
            )
            self._last_bet_request_prune = now
        if not token:
            return None
        cur = await conn.execute(
            """
            INSERT INTO bet_requests (user_id, channel, token, bet_id, created_at)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT DO NOTHING
            """,
            (user_id, channel, token, now),
        )
        if cur.rowcount:
            return None
        row = await (
            await conn.execute(
                "SELECT bet_id FROM bet_requests WHERE user_id = ? AND channel = ? AND token = ?",
                (user_id, channel, token),
            )
        ).fetchone()
        return int(row["bet_id"]) if row is not None else 0

    async def _remember_token(
        self, conn: Any, user_id: int, channel: str, token: str, bet_id: int
    ) -> None:
        if not token:
            return
        await conn.execute(
            "UPDATE bet_requests SET bet_id = ? WHERE user_id = ? AND channel = ? AND token = ?",
            (int(bet_id), user_id, channel, token),
        )

    async def _previous_id(self, conn: Any, user_id: int, channel: str, token: str) -> int:
        row = await (
            await conn.execute(
                "SELECT bet_id FROM bet_requests WHERE user_id = ? AND channel = ? AND token = ?",
                (user_id, channel, token),
            )
        ).fetchone()
        return int(row["bet_id"]) if row is not None else 0

    async def _repeat_bet(
        self, conn: Any, user_id: int, channel: str, token: str, table: str
    ) -> tuple[int, float]:
        """Ответ на повторный тап: баланс и кэф уже сделанной ставки."""
        previous = await self._previous_id(conn, user_id, channel, token)
        row = await (
            await conn.execute(f"SELECT odds FROM {table} WHERE id = ?", (previous,))
        ).fetchone()
        return await self._balance_in(conn, user_id), float(row["odds"]) if row is not None else 0.0

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
        await self._write(
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

    async def get_user(self, user_id: int) -> Any | None:
        return await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))

    async def get_balance(self, user_id: int) -> int:
        row = await self._fetchone("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        return int(row["balance"]) if row else 0

    async def daily_bonus(
        self, user_id: int, amount: int, cooldown_seconds: int
    ) -> tuple[bool, int, int]:
        """Ежедневный бонус. Чем длиннее серия дней подряд, тем больше начисление.

        Возвращает (получен, ждать_сек, баланс); сумма с множителем — в `bonus_amount`.
        """
        now = int(time.time())
        day = now // 86400
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT bonus_at, balance, streak_days, bonus_day FROM users WHERE user_id = ?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                streak = 1
                credited = bonus_amount(amount, streak)
                balance = self.start_balance + credited
                await conn.execute(
                    "INSERT INTO users (user_id, balance, bonus_at, created_at, last_seen_at,"
                    " streak_days, bonus_day) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, balance, now, now, now, streak, day),
                )
                await self._ledger(conn, user_id, credited, "ежедневный бонус", now)
                await self._bump_quest(conn, user_id, "bonus", now)
                return True, 0, balance

            elapsed = now - int(row["bonus_at"])
            if elapsed < cooldown_seconds:
                return False, cooldown_seconds - elapsed, int(row["balance"])

            # серия: вчера забирал — продолжаем, пропустил день — начинаем заново
            last_day = int(row["bonus_day"] or 0)
            streak = int(row["streak_days"] or 0) + 1 if last_day == day - 1 else 1
            credited = bonus_amount(amount, streak)
            await conn.execute(
                "UPDATE users SET balance = balance + ?, bonus_at = ?, streak_days = ?,"
                " bonus_day = ? WHERE user_id = ?",
                (credited, now, streak, day, user_id),
            )
            await self._ledger(conn, user_id, credited, "ежедневный бонус", now)
            await self._bump_quest(conn, user_id, "bonus", now)
            return True, 0, int(row["balance"]) + credited

    # ----------------------------------------------------------------- бэкапы
    def is_postgres(self) -> bool:
        return self._url.startswith(("postgres://", "postgresql://"))

    async def dump_rows(self) -> dict[str, list[dict[str, Any]]]:
        """Согласованный снимок всех таблиц в одной транзакции."""
        data: dict[str, list[dict[str, Any]]] = {}
        async with self.transaction(isolation="REPEATABLE READ") as conn:
            for table in BACKUP_TABLES:
                async with conn.execute(f"SELECT * FROM {table}") as cur:
                    rows = await cur.fetchall()
                data[table] = [dict(row) for row in rows]
        return data

    async def restore_rows(self, data: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        """Полная замена содержимого базы данными из дампа."""
        unknown = set(data) - set(BACKUP_TABLES)
        if unknown:
            raise BackupError(f"В дампе незнакомые таблицы: {', '.join(sorted(unknown))}")
        missing = set(BACKUP_TABLES) - set(data)
        # Older backups may predate these new game tables; restore those as empty,
        # while keeping every established table omission strict.
        legacy_optional = {"crash_games", "drunkard_games", "mines_games"}
        if missing and missing <= legacy_optional:
            data = {**data, **{table: [] for table in missing}}
            missing = set()
        if missing:
            raise BackupError(
                "Дамп неполный — отсутствуют таблицы " + ", ".join(sorted(missing))
                + ". Восстановление отменено, чтобы не удалить несохранённые данные."
            )

        for table, rows in data.items():
            if not isinstance(rows, list):
                raise BackupError(f"Таблица {table} в дампе должна быть списком строк")
            allowed_columns = await self.conn.table_columns(table)
            for row in rows:
                if not isinstance(row, dict) or not row:
                    raise BackupError(f"В таблице {table} обнаружена некорректная строка")
                columns = set(row)
                if any(not isinstance(column, str) for column in columns):
                    raise BackupError(f"В таблице {table} некорректные имена полей")
                unknown_columns = columns - allowed_columns
                if unknown_columns:
                    raise BackupError(
                        f"В таблице {table} неизвестные поля: {', '.join(sorted(unknown_columns))}"
                    )

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
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"EXISTS(SELECT 1 FROM {table}))"
            )

    async def log_backup(self, size_bytes: int, users: int, bets: int, kind: str) -> int:
        now = int(time.time())
        await self._write(
            "INSERT INTO backup_log (created_at, size_bytes, users, bets, kind) VALUES (?, ?, ?, ?, ?)",
            (now, size_bytes, users, bets, kind),
        )
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

        streak_row = await self._fetchone(
            "SELECT streak_days, bonus_day FROM users WHERE user_id = ?", (user_id,)
        )
        streak = int(streak_row["streak_days"] or 0) if streak_row else 0
        last_day = int(streak_row["bonus_day"] or 0) if streak_row else 0
        today = now // 86400
        # серия жива, только если бонус забирали вчера (или ещё не забирали сегодня)
        streak_alive = last_day >= today - 1
        return {
            "daily": {
                "wait_seconds": wait(int(user["bonus_at"]) if user else 0, daily_cooldown),
                "streak": streak if streak_alive else 0,
                "streak_alive": streak_alive,
            },
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
            await self._bump_quest(conn, user_id, "bonus", now)
            return await self._credit(conn, user_id, prize, "колесо фортуны", now)

    async def add_ticket(
        self,
        user_id: int,
        amount: int,
        source: str = "wheel",
        *,
        kind: str = "express",
        ttl_seconds: int | None = None,
    ) -> int:
        """Кладёт в профиль билет. Возвращает его id.

        `kind` — куда его можно потратить: `express` (приз колеса) или `single`
        (фрибет за возвращение). `ttl_seconds` — срок жизни: у фрибета 24 часа.
        """
        now = int(time.time())
        expires_at = now + int(ttl_seconds) if ttl_seconds else None
        created = await self._write(
            "INSERT INTO tickets (user_id, amount, source, kind, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, int(amount), source[:32], kind, now, expires_at),
        )
        return int(getattr(created, "lastrowid", 0) or 0)

    async def tickets(self, user_id: int, limit: int = 20, kind: str | None = None) -> list[Any]:
        """Живые билеты игрока: не истраченные и не просроченные.

        Просрочку не удаляем, а просто не показываем: история остаётся в базе.
        """
        now = int(time.time())
        sql = (
            "SELECT id, amount, source, kind, created_at, expires_at FROM tickets"
            " WHERE user_id = ? AND used_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > ?)"
        )
        params: list[Any] = [user_id, now]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return await self._fetchall(sql, tuple(params))

    async def _spend_single(self, conn: Any, user_id: int, amount: int, now: int,
                            ticket: Any | None) -> None:
        """Оплата ординара: либо фрибет (монеты не двигаются), либо обычное списание."""
        if ticket is not None:
            spent = await conn.execute(
                "UPDATE tickets SET used_at = ? WHERE id = ? AND used_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (now, int(ticket["id"]), now),
            )
            if spent.rowcount == 0:
                raise InvalidBet("Фрибет уже использован или срок его действия истёк")
            return
        cur = await conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        if cur.rowcount == 0:
            raise InsufficientFunds("Недостаточно монет на балансе")

    async def _settle_single_ledger(self, conn: Any, user_id: int, bet_id: int, amount: int,
                                    reason: str, now: int, ticket: Any | None) -> None:
        """Движение по счёту: по фрибету пишем нулевую строку для истории."""
        if ticket is not None:
            await conn.execute(
                "UPDATE tickets SET bet_id = ? WHERE id = ?", (bet_id, int(ticket["id"]))
            )
            await self._ledger(conn, user_id, 0, f"ставка · фрибет · {reason}", now)
            return
        await self._ledger(conn, user_id, -amount, f"ставка · {reason}", now)

    async def _claim_freebet(self, ticket_id: int, user_id: int, amount: int, kind: str) -> Any:
        """Проверяет билет перед ставкой: свой, нужного вида, живой и нужного номинала."""
        now = int(time.time())
        ticket = await self._fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if ticket is None or int(ticket["user_id"]) != user_id:
            raise InvalidBet("Фрибет или билет не найден")
        if ticket["used_at"] is not None:
            raise InvalidBet("Фрибет уже использован")
        expires_at = ticket["expires_at"]
        if expires_at is not None and int(expires_at) <= now:
            raise InvalidBet("Срок действия фрибета истёк")
        # экспрессный билет нельзя поставить на ординар и наоборот
        if str(ticket["kind"] or "express") not in {kind, "any"}:
            raise InvalidBet("Этот билет — для другого типа ставки")
        if int(ticket["amount"]) != int(amount):
            raise InvalidBet(
                f"Фрибет на {int(ticket['amount'])} 🪙 — сумма ставки должна совпадать"
            )
        return ticket

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
            await self._bump_quest(conn, user_id, "bonus", now)
            return True, 0, await self._balance_in(conn, user_id)

    async def tap_tease(
        self,
        user_id: int,
        amount: int,
        cooldown: int,
        *,
        daily_cooldown: int,
        target: int = 10,
        tap_window_seconds: int = 60,
    ) -> tuple[bool, int, int, int]:
        """Серверно считает отдельные нажатия и платит только по достижении цели."""
        now = int(time.time())
        target = max(1, int(target))
        async with self.transaction() as conn:
            daily = await (
                await conn.execute("SELECT bonus_at FROM users WHERE user_id = ?", (user_id,))
            ).fetchone()
            last_daily = int(daily["bonus_at"]) if daily is not None else 0
            if not last_daily or now - last_daily >= daily_cooldown:
                raise InvalidBet("Сначала забери ежедневный бонус")

            row = await (
                await conn.execute(
                    "SELECT claimed_at, tap_count, tap_at FROM tease_claims WHERE user_id = ?",
                    (user_id,),
                )
            ).fetchone()
            last_claim = int(row["claimed_at"]) if row else 0
            if last_claim and now - last_claim < cooldown:
                return (
                    False,
                    cooldown - (now - last_claim),
                    await self._balance_in(conn, user_id),
                    0,
                )

            taps = int(row["tap_count"]) if row else 0
            last_tap = int(row["tap_at"]) if row else 0
            if not last_tap or now - last_tap > tap_window_seconds:
                taps = 0
            taps += 1

            if taps >= target:
                if row:
                    await conn.execute(
                        "UPDATE tease_claims SET claimed_at = ?, tap_count = 0, tap_at = 0 "
                        "WHERE user_id = ?",
                        (now, user_id),
                    )
                else:
                    await conn.execute(
                        "INSERT INTO tease_claims (user_id, claimed_at, tap_count, tap_at) "
                        "VALUES (?, ?, 0, 0)",
                        (user_id, now),
                    )
                await self._bump_quest(conn, user_id, "bonus", now)
                _, _, balance = await self._credit(conn, user_id, amount, "за настойчивость", now)
                return True, 0, balance, 0

            if row:
                await conn.execute(
                    "UPDATE tease_claims SET tap_count = ?, tap_at = ? WHERE user_id = ?",
                    (taps, now, user_id),
                )
            else:
                await conn.execute(
                    "INSERT INTO tease_claims (user_id, claimed_at, tap_count, tap_at) "
                    "VALUES (?, 0, ?, ?)",
                    (user_id, taps, now),
                )
            return False, 0, await self._balance_in(conn, user_id), taps

    # ---------------------------------------------------------------- задания
    async def quests_state(self, user_id: int) -> list[Any]:
        """Задания сегодняшнего дня: строки создаются при первом обращении."""
        day = int(time.time()) // 86400
        async with self.transaction() as conn:
            for quest in quests_of_day(day):
                await conn.execute(
                    """
                    INSERT INTO quests (user_id, day, code, progress, target, reward)
                    VALUES (?, ?, ?, 0, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (user_id, day, quest["code"], int(quest["target"]), int(quest["reward"])),
                )
            async with conn.execute(
                "SELECT code, progress, target, reward, claimed_at FROM quests"
                " WHERE user_id = ? AND day = ?",
                (user_id, day),
            ) as cur:
                return list(await cur.fetchall())

    async def claim_quest(self, user_id: int, code: str) -> tuple[bool, int, int, str]:
        """Забрать награду за задание. Возвращает (получено, награда, баланс, текст ошибки)."""
        now = int(time.time())
        day = now // 86400
        quest = by_code(day, code)
        if quest is None:
            return False, 0, await self.get_balance(user_id), "Сегодня такого задания нет"
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT progress, target, reward, claimed_at FROM quests"
                " WHERE user_id = ? AND day = ? AND code = ?",
                (user_id, day, code),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return False, 0, await self._balance_in(conn, user_id), "Задание не найдено"
            if row["claimed_at"] is not None:
                return False, 0, await self._balance_in(conn, user_id), "Награда уже получена"
            if int(row["progress"]) < int(row["target"]):
                return False, 0, await self._balance_in(conn, user_id), "Задание ещё не выполнено"

            reward = int(row["reward"])
            await conn.execute(
                "UPDATE quests SET claimed_at = ? WHERE user_id = ? AND day = ? AND code = ?",
                (now, user_id, day, code),
            )
            await self._credit(conn, user_id, reward, f"задание · {quest['title']}", now)

            # все задания дня закрыты — двигаем серию дней под сундук
            async with conn.execute(
                "SELECT COUNT(*) AS left FROM quests"
                " WHERE user_id = ? AND day = ? AND claimed_at IS NULL",
                (user_id, day),
            ) as cur:
                left = await cur.fetchone()
            if not int(left["left"] or 0):
                await self._bump_quest_chain(conn, user_id, day)
            return True, reward, await self._balance_in(conn, user_id), ""

    async def _bump_quest(self, conn: Any, user_id: int, code: str, now: int) -> None:
        """Двигает прогресс задания внутри уже открытой транзакции.

        Строка создаётся здесь же: игрок мог ни разу не открыть экран бонусов,
        а прогресс за ставку всё равно должен засчитаться.
        """
        day = now // 86400
        quest = by_code(day, code)
        if quest is None:
            return
        await conn.execute(
            """
            INSERT INTO quests (user_id, day, code, progress, target, reward)
            VALUES (?, ?, ?, 0, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (user_id, day, code, int(quest["target"]), int(quest["reward"])),
        )
        await conn.execute(
            """
            UPDATE quests
            SET progress = CASE WHEN progress < target THEN progress + 1 ELSE progress END
            WHERE user_id = ? AND day = ? AND code = ? AND claimed_at IS NULL
            """,
            (user_id, day, code),
        )

    async def users_to_remind(self, day: int, event: str, days: int = 30) -> list[Any]:
        """Кому стоит напомнить про бонус: серия есть, бонус не взят, сегодня не писали."""
        since = int(time.time()) - days * 86400
        return await self._fetchall(
            """
            SELECT u.user_id, u.streak_days FROM users u
            WHERE u.streak_days >= 1
              AND u.bonus_day < ?
              AND u.last_seen_at >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM events e
                  WHERE e.user_id = u.user_id AND e.name = ? AND e.created_at >= ?
              )
            ORDER BY u.streak_days DESC
            LIMIT 500
            """,
            (day, since, event, day * 86400),
        )

    async def grant_winback_ticket(
        self, user_id: int, amount: int, now: int | None = None, ttl_seconds: int = 86400
    ) -> int | None:
        """Атомарно выдаёт один фрибет на текущую паузу в активности.

        Повторные/параллельные тики не могут добавить второй билет: проверка
        последнего входа, запись события и создание билета проходят в одной tx.
        """
        moment = int(now if now is not None else time.time())
        async with self.transaction() as conn:
            user = await (
                await conn.execute("SELECT last_seen_at FROM users WHERE user_id = ?", (user_id,))
            ).fetchone()
            if user is None or int(user["last_seen_at"] or 0) > moment - 3 * 86400:
                return None
            previous = await (
                await conn.execute(
                    "SELECT 1 FROM events WHERE user_id = ? AND name = 'winback' "
                    "AND payload = ? LIMIT 1",
                    (user_id, str(int(user["last_seen_at"] or 0))),
                )
            ).fetchone()
            active_freebet = await (
                await conn.execute(
                    "SELECT 1 FROM tickets WHERE user_id = ? AND kind = 'single' "
                    "AND used_at IS NULL AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                    (user_id, moment),
                )
            ).fetchone()
            if previous is not None or active_freebet is not None:
                return None
            created = await conn.execute(
                """
                INSERT INTO tickets (user_id, amount, source, kind, created_at, expires_at)
                VALUES (?, ?, 'возврат', 'single', ?, ?) RETURNING id
                """,
                (user_id, amount, moment, moment + ttl_seconds),
            )
            row = await created.fetchone()
            await conn.execute(
                "INSERT INTO events (user_id, name, payload, created_at) VALUES (?, 'winback', ?, ?)",
                (user_id, str(int(user["last_seen_at"] or 0)), moment),
            )
            return int(row["id"]) if row else int(created.lastrowid or 0)

    async def grant_admin_ticket(
        self,
        user_id: int,
        amount: int,
        kind: str,
        request_key: str,
        now: int | None = None,
        ttl_seconds: int = 86400,
    ) -> tuple[int | None, str]:
        """Атомарно выдать админский билет для ординара или экспресса.

        Несколько активных билетов разрешены. `request_key` защищает от повтора
        одного Telegram-апдейта, а тип/сумма/срок фиксируются в отдельном билете.
        """
        moment = int(now if now is not None else time.time())
        ticket_kind = str(kind).strip().lower()
        if ticket_kind not in {"single", "express"}:
            raise ValueError("kind должен быть single или express")
        if amount <= 0 or amount > 2**31 - 1 or ttl_seconds <= 0:
            raise ValueError("amount должен быть в диапазоне int32, ttl_seconds — положительным")
        key = f"{ticket_kind}:{request_key[:200]}"
        if not request_key.strip():
            raise ValueError("request_key обязателен")

        async with self.transaction() as conn:
            user = await (
                await conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
            ).fetchone()
            if user is None:
                return None, "not_found"

            previous = await (
                await conn.execute(
                    "SELECT 1 FROM events WHERE user_id = ? AND name = 'admin_ticket' "
                    "AND payload = ? LIMIT 1",
                    (user_id, key),
                )
            ).fetchone()
            if previous is not None:
                return None, "duplicate"

            created = await conn.execute(
                """
                INSERT INTO tickets (user_id, amount, source, kind, created_at, expires_at)
                VALUES (?, ?, 'админ', ?, ?, ?) RETURNING id
                """,
                (user_id, int(amount), ticket_kind, moment, moment + ttl_seconds),
            )
            row = await created.fetchone()
            await conn.execute(
                "INSERT INTO events (user_id, name, payload, created_at) VALUES (?, 'admin_ticket', ?, ?)",
                (user_id, key, moment),
            )
            return (int(row["id"]) if row else int(created.lastrowid or 0)), "granted"

    async def users_to_winback(self, now: int, absent_days: int = 3, limit: int = 500) -> list[Any]:
        """Игроки с тишиной от `absent_days` дней, не получавшие бонус в эту тишину.

        Старое событие перестаёт блокировать следующую выдачу после нового входа:
        повторный фрибет возможен только после реального возвращения и новой паузы.
        """
        silent_since = now - absent_days * 86400
        return await self._fetchall(
            """
            SELECT u.user_id, u.last_seen_at FROM users u
            WHERE u.last_seen_at > 0 AND u.last_seen_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM events e
                  WHERE e.user_id = u.user_id AND e.name = 'winback'
                    AND e.payload = CAST(u.last_seen_at AS TEXT)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM tickets t WHERE t.user_id = u.user_id AND t.kind = 'single'
                    AND t.used_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > ?)
              )
            ORDER BY u.last_seen_at
            LIMIT ?
            """,
            (silent_since, now, limit),
        )

    async def best_win_since(self, since: int) -> Any | None:
        """Самый крупный выигрыш за окно: для поста «купон дня»."""
        return await self._fetchone(
            """
            SELECT * FROM (
                SELECT 'bet' AS kind, b.id AS id, b.user_id AS user_id,
                       b.payout AS payout FROM bets b
                JOIN matches m ON m.id = b.match_id
                WHERE b.cashed_out = 0 AND b.payout > b.amount AND m.finished_at >= ?
                UNION ALL
                SELECT 'express' AS kind, e.id AS id, e.user_id AS user_id, e.payout AS payout
                FROM expresses e
                WHERE e.status = 'won' AND e.settled_at >= ?
            ) AS wins
            ORDER BY payout DESC
            LIMIT 1
            """,
            (since, since),
        )

    async def line_totals(self) -> dict[str, int]:
        """Фактические валовые выплаты по рассчитанным спортивным ставкам."""
        bets = await self._fetchone(
            "SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS staked,"
            " COALESCE(SUM(payout), 0) AS paid"
            " FROM bets WHERE payout IS NOT NULL"
        )
        expresses = await self._fetchone(
            "SELECT COUNT(*) AS count, COALESCE(SUM(stake), 0) AS staked,"
            " COALESCE(SUM(payout), 0) AS paid"
            " FROM expresses WHERE status IN ('won', 'lost', 'void')"
        )
        casino = await self._fetchone(
            "SELECT COALESCE(SUM(amount), 0) AS staked, COALESCE(SUM(payout), 0) AS paid"
            " FROM casino_bets"
        )
        wheel = await self._fetchone(
            "SELECT COALESCE(SUM(prize), 0) AS paid, COUNT(*) AS spins FROM wheel_spins"
        )
        return {
            "bets_count": int(bets["count"] or 0),
            "bets_staked": int(bets["staked"] or 0),
            "bets_paid": int(bets["paid"] or 0),
            "express_count": int(expresses["count"] or 0),
            "express_staked": int(expresses["staked"] or 0),
            "express_paid": int(expresses["paid"] or 0),
            "casino_staked": int(casino["staked"] or 0),
            "casino_paid": int(casino["paid"] or 0),
            "wheel_paid": int(wheel["paid"] or 0),
            "wheel_spins": int(wheel["spins"] or 0),
        }

    # ------------------------------------------------------------ достижения
    async def achievement_stats(self, user_id: int) -> dict[str, Any]:
        """Сухие цифры под достижения: считаем их по базе, а не по событиям.

        Так достижение нельзя «потерять»: даже если игрок заработал его месяц
        назад, при следующем заходе оно откроется.
        """
        bets = await self._fetchone(
            """
            SELECT COUNT(*) AS placed,
                   COALESCE(SUM(CASE WHEN payout IS NOT NULL AND payout > amount THEN 1 ELSE 0 END), 0) AS won,
                   COALESCE(MAX(odds), 0) AS max_odds
            FROM bets WHERE user_id = ? AND cashed_out = 0
            """,
            (user_id,),
        )
        expresses = await self._fetchone(
            """
            SELECT COUNT(*) AS placed,
                   COALESCE(SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END), 0) AS won,
                   COALESCE(MAX(odds), 0) AS max_odds
            FROM expresses WHERE user_id = ?
            """,
            (user_id,),
        )
        casino = await self._fetchone(
            "SELECT COUNT(*) AS rounds, COALESCE(SUM(CASE WHEN win = 1 THEN 1 ELSE 0 END), 0) AS won"
            " FROM casino_bets WHERE user_id = ?",
            (user_id,),
        )
        sports = await self._fetchone(
            """
            SELECT COALESCE(MAX(covered), 0) AS covered FROM (
                SELECT COUNT(DISTINCT m.sport) AS covered
                FROM express_items i
                JOIN expresses e ON e.id = i.express_id
                JOIN matches m ON m.id = i.match_id
                WHERE e.user_id = ?
                GROUP BY e.id
            ) AS by_express
            """,
            (user_id,),
        )
        user = await self._fetchone(
            "SELECT streak_days, balance FROM users WHERE user_id = ?", (user_id,)
        )
        placed = int(bets["placed"] or 0) + int(expresses["placed"] or 0)
        return {
            "bets_placed": int(bets["placed"] or 0),
            "bets_won": int(bets["won"] or 0),
            "express_placed": int(expresses["placed"] or 0),
            "express_won": int(expresses["won"] or 0),
            "casino_rounds": int(casino["rounds"] or 0),
            "casino_won": int(casino["won"] or 0),
            "max_odds": max(float(bets["max_odds"] or 0), float(expresses["max_odds"] or 0)),
            "sports_in_express": int(sports["covered"] or 0),
            "streak_days": int(user["streak_days"] or 0) if user else 0,
            "balance": int(user["balance"] or 0) if user else 0,
            "placed": placed,
            "won": int(bets["won"] or 0) + int(expresses["won"] or 0),
        }

    async def unlocked_achievements(self, user_id: int) -> list[Any]:
        return await self._fetchall(
            "SELECT code, unlocked_at FROM achievements WHERE user_id = ?", (user_id,)
        )

    async def unlock_achievements(self, user_id: int, codes: list[str]) -> list[str]:
        """Открывает достижения, которых ещё нет. Возвращает только новые коды."""
        if not codes:
            return []
        now = int(time.time())
        fresh: list[str] = []
        async with self.transaction() as conn:
            for code in codes:
                cur = await conn.execute(
                    "INSERT INTO achievements (user_id, code, unlocked_at) VALUES (?, ?, ?)"
                    " ON CONFLICT DO NOTHING",
                    (user_id, code, now),
                )
                if cur.rowcount:
                    fresh.append(code)
        return fresh

    # --------------------------------------------------------- цепочка заданий
    async def quest_chain(self, user_id: int) -> dict[str, int]:
        """Сколько дней подряд игрок закрывает все задания дня."""
        row = await self._fetchone(
            "SELECT days, last_day, best_days, chest_day FROM quest_chain WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return {"days": 0, "best_days": 0, "chest_day": 0, "last_day": 0}
        return {key: int(row[key] or 0) for key in ("days", "last_day", "best_days", "chest_day")}

    async def _bump_quest_chain(self, conn: Any, user_id: int, day: int) -> None:
        """День закрыт полностью — двигаем цепочку (внутри открытой транзакции)."""
        async with conn.execute(
            "SELECT days, last_day, best_days FROM quest_chain WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await conn.execute(
                "INSERT INTO quest_chain (user_id, days, last_day, best_days, chest_day)"
                " VALUES (?, 1, ?, 1, 0)",
                (user_id, day),
            )
            return
        last_day = int(row["last_day"] or 0)
        days = int(row["days"] or 0) + 1 if last_day == day - 1 else 1
        best = max(int(row["best_days"] or 0), days)
        await conn.execute(
            "UPDATE quest_chain SET days = ?, last_day = ?, best_days = ? WHERE user_id = ?",
            (days, day, best, user_id),
        )

    async def claim_chain_chest(self, user_id: int) -> tuple[bool, int, int, str]:
        """Сундук за 3 или 7 закрытых дней подряд. Возвращает (получено, монеты, баланс, ошибка)."""
        from app.services.quests import chain_chest

        now = int(time.time())
        day = now // 86400
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT days, last_day, chest_day FROM quest_chain WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return False, 0, await self._balance_in(conn, user_id), "Цепочка ещё не начата"
            days = int(row["days"] or 0)
            if int(row["last_day"] or 0) not in (day, day - 1):
                days = 0        # день пропущен — цепочка уже сгорела
            chest = chain_chest(days)
            if chest is None:
                return False, 0, await self._balance_in(conn, user_id), "Пока нечего забирать"
            if int(row["chest_day"] or 0) >= chest["days"]:
                return False, 0, await self._balance_in(conn, user_id), "Сундук уже забрали"
            await conn.execute(
                "UPDATE quest_chain SET chest_day = ? WHERE user_id = ?",
                (chest["days"], user_id),
            )
            await self._credit(conn, user_id, int(chest["reward"]), "сундук за серию дней", now)
            return True, int(chest["reward"]), await self._balance_in(conn, user_id), ""

    # --------------------------------------------------------------- рефералы
    async def attach_referral(self, invited_id: int, inviter_id: int) -> tuple[bool, str]:
        """Запомнить, кто кого позвал. Платим позже — после первой ставки друга."""
        if invited_id == inviter_id:
            return False, "Себя пригласить нельзя"
        inviter = await self._fetchone("SELECT user_id FROM users WHERE user_id = ?", (inviter_id,))
        if inviter is None:
            return False, "Приглашение не найдено"
        already = await self._fetchone(
            "SELECT invited_id FROM referrals WHERE invited_id = ?", (invited_id,)
        )
        if already is not None:
            return False, ""
        played = await self._fetchone(
            "SELECT (SELECT COUNT(*) FROM bets WHERE user_id = ?)"
            " + (SELECT COUNT(*) FROM expresses WHERE user_id = ?)"
            " + (SELECT COUNT(*) FROM casino_bets WHERE user_id = ?)"
            " + (SELECT COUNT(*) FROM ledger WHERE user_id = ?"
            " AND reason LIKE 'ставка%') AS acts",
            (invited_id, invited_id, invited_id, invited_id),
        )
        if played is not None and int(played["acts"] or 0) > 0:
            return False, "Приглашение работает только до первой ставки"
        await self._write(
            "INSERT INTO referrals (invited_id, inviter_id, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (invited_id, inviter_id, int(time.time())),
        )
        return True, ""

    async def referral_state(self, user_id: int, inviter_bonus: int) -> dict[str, Any]:
        """Сколько друзей позвал и сколько за них уже получил."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS invited,"
            " COALESCE(SUM(CASE WHEN paid_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS paid"
            " FROM referrals WHERE inviter_id = ?",
            (user_id,),
        )
        invited = int(row["invited"] or 0) if row else 0
        paid = int(row["paid"] or 0) if row else 0
        return {
            "invited": invited,
            "paid": paid,
            "earned": paid * int(inviter_bonus),
            "pending": invited - paid,
            "bonus": int(inviter_bonus),
        }

    async def pay_referral(self, invited_id: int, inviter_bonus: int, welcome_bonus: int) -> int:
        """Друг сделал первый шаг — платим обоим. Возвращает бонус пригласившему (0 — некому)."""
        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT inviter_id, paid_at FROM referrals WHERE invited_id = ?", (invited_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None or row["paid_at"] is not None:
                return 0
            inviter_id = int(row["inviter_id"])
            await conn.execute(
                "UPDATE referrals SET paid_at = ? WHERE invited_id = ?", (now, invited_id)
            )
            await self._credit(conn, inviter_id, int(inviter_bonus), "друг пришёл по ссылке", now)
            await self._credit(conn, invited_id, int(welcome_bonus), "бонус за приглашение", now)
            return int(inviter_bonus)

    # ----------------------------------------------------------------- кэшаут
    async def cashout_bet(
        self, bet_id: int, user_id: int, rate: float
    ) -> tuple[int, int]:
        """Продать предматчевую ставку до старта: возвращаем часть ставки.

        Это не «кэшаут по модели», а честная отмена с комиссией: игрок получает
        `rate` от ставки, ставка больше не участвует в расчёте матча.
        """
        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT b.*, m.status AS match_status, m.start_time FROM bets b"
                " JOIN matches m ON m.id = b.match_id WHERE b.id = ?",
                (bet_id,),
            ) as cur:
                bet = await cur.fetchone()
            if bet is None or int(bet["user_id"]) != user_id:
                raise MatchNotFound("Ставка не найдена")
            if int(bet["cashed_out"] or 0):
                raise InvalidBet("Ставка уже продана")
            if bet["payout"] is not None:
                raise InvalidBet("Ставка уже рассчитана")
            if str(bet["match_status"]) != "open" or int(bet["start_time"]) <= now:
                raise InvalidBet("Матч уже начался — продать нельзя")
            if str(bet["market"]) != "match":
                raise InvalidBet("Продать можно только ставку на исход матча")
            ticket = await (
                await conn.execute("SELECT 1 FROM tickets WHERE bet_id = ? LIMIT 1", (bet_id,))
            ).fetchone()
            if ticket is not None:
                raise InvalidBet("Ставку, оплаченную фрибетом, продать нельзя")

            refund = round(int(bet["amount"]) * rate)
            await conn.execute(
                "UPDATE bets SET payout = ?, cashed_out = 1 WHERE id = ?", (refund, bet_id)
            )
            await self._credit(conn, user_id, refund, f"продажа ставки #{bet_id}", now)
            return refund, await self._balance_in(conn, user_id)

    # --------------------------------------------------------------- события
    async def has_event(self, user_id: int, name: str, payload: str = "") -> bool:
        """Было ли уже такое событие — чтобы не писать игроку дважды об одном матче."""
        row = await self._fetchone(
            "SELECT 1 AS found FROM events WHERE user_id = ? AND name = ? AND payload = ? LIMIT 1",
            (user_id, name[:64], payload[:500]),
        )
        return row is not None

    async def log_event(self, user_id: int | None, name: str, payload: str = "") -> None:
        """Событие для аналитики. Ошибки логирования не должны ломать ставку."""
        try:
            await self._write(
                "INSERT INTO events (user_id, name, payload, created_at) VALUES (?, ?, ?, ?)",
                (user_id, name[:64], payload[:500], int(time.time())),
            )
        except Exception:
            log.debug("Не смог записать событие %s", name, exc_info=True)

    async def event_counts(self, days: int = 7) -> list[Any]:
        since = int(time.time()) - days * 86400
        return await self._fetchall(
            "SELECT name, COUNT(*) AS n FROM events WHERE created_at >= ?"
            " GROUP BY name ORDER BY n DESC",
            (since,),
        )

    # --------------------------------------------------------------- казино
    async def play_casino(
        self,
        user_id: int,
        *,
        game: str,
        selection: str,
        label: str,
        amount: int,
        odds: float,
        outcome: str,
        won: bool,
        payout: int,
        token: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        """Проводит раунд рулетки/костей: списывает ставку, пишет историю, платит выигрыш.

        Исход и выплата считаются в `app.services.games`; здесь только деньги,
        одной транзакцией — либо списание и выплата проходят вместе, либо ничего.
        Возвращает (новый баланс, id раунда).
        """
        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")
        if payout < 0:
            raise InvalidBet("Выплата не может быть отрицательной")

        tap = self._clean_token(token)
        now = int(time.time())
        async with self.transaction() as conn:
            if await self._claim_token(conn, user_id, "casino", tap, now) is not None:
                previous = await self._previous_id(conn, user_id, "casino", tap)
                return await self._balance_in(conn, user_id), previous
            cur = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (amount, user_id, amount),
            )
            if cur.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")

            reason = f"{game} · {label}"
            await self._ledger(conn, user_id, -amount, f"ставка · {reason}", now)
            await self._bump_quest(conn, user_id, "casino", now)
            if payout:
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, user_id)
                )
                await self._ledger(conn, user_id, payout, f"выигрыш · {reason}", now)

            inserted = await conn.execute(
                """
                INSERT INTO casino_bets
                    (user_id, game, selection, label, amount, odds, outcome, win, payout,
                     result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    game,
                    selection,
                    label,
                    amount,
                    float(odds),
                    outcome,
                    1 if won else 0,
                    payout,
                    json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                    if response is not None else None,
                    now,
                ),
            )
            round_id = int(inserted.lastrowid or 0)
            await self._remember_token(conn, user_id, "casino", tap, round_id)
            return await self._balance_in(conn, user_id), round_id

    @staticmethod
    def _slot_bonus_payload(raw: str | None) -> dict[str, Any] | None:
        if not raw:
            return None
        bonus = json.loads(raw)
        return {
            "status": str(bonus.get("status") or "pending"),
            "total_picks": int(bonus.get("total_picks", 0)),
            "picks_remaining": int(bonus.get("picks_remaining", 0)),
            "wager": int(bonus.get("wager", 0)),
            "scatter_count": int(bonus.get("scatter_count", 0)),
            "opened": [
                {
                    "chest": int(item["chest"]),
                    "multiplier": float(item["multiplier"]),
                    "payout": int(item["payout"]),
                }
                for item in (bonus.get("opened") or [])
            ],
        }

    async def slot_state(self, user_id: int) -> dict[str, Any]:
        """Saved free spins, their locked wager, and any pending chest bonus."""
        row = await self._fetchone(
            "SELECT free_spins, spin_bet, bonus_json FROM slot_states WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return {"free_spins": 0, "spin_bet": 0, "bonus": None}
        return {
            "free_spins": int(row["free_spins"]),
            "spin_bet": int(row["spin_bet"]),
            "bonus": self._slot_bonus_payload(str(row["bonus_json"]) if row["bonus_json"] else None),
        }

    async def play_slot(
        self,
        user_id: int,
        amount: int,
        *,
        spin: Callable[[int, bool], dict[str, Any]],
        token: str | None = None,
    ) -> dict[str, Any]:
        """Commit one paid/free slot spin, balance, result, and bonus state atomically."""
        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")
        tap = self._clean_token(token)
        now = int(time.time())
        async with self.transaction() as conn:
            duplicate = await self._claim_token(conn, user_id, "slot", tap, now)
            if duplicate is not None:
                previous = duplicate or await self._previous_id(conn, user_id, "slot", tap)
                row = await (
                    await conn.execute(
                        "SELECT outcome FROM casino_bets WHERE id = ? AND user_id = ? AND game = 'slots'",
                        (previous, user_id),
                    )
                ).fetchone()
                if row is None:
                    raise InvalidBet("Этот спин уже обработан")
                previous_result = json.loads(str(row["outcome"]))
                previous_result["round"] = previous
                previous_result["balance"] = await self._balance_in(conn, user_id)
                return previous_result

            saved = await (
                await conn.execute(
                    "SELECT free_spins, spin_bet, bonus_json FROM slot_states WHERE user_id = ?",
                    (user_id,),
                )
            ).fetchone()
            available_free_spins = int(saved["free_spins"]) if saved is not None else 0
            raw_bonus = str(saved["bonus_json"]) if saved is not None and saved["bonus_json"] else None
            bonus_state = json.loads(raw_bonus) if raw_bonus else None
            if bonus_state and bonus_state.get("status") == "pending" and int(bonus_state.get("picks_remaining", 0)) > 0:
                raise InvalidBet("Сначала открой все сундуки Scatter-бонуса")
            is_free = available_free_spins > 0
            wager = int(saved["spin_bet"]) if is_free and saved is not None else int(amount)
            if wager <= 0:
                raise InvalidBet("Ставка для фри-спина не найдена")

            if not is_free:
                cur = await conn.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                    (wager, user_id, wager),
                )
                if cur.rowcount == 0:
                    raise InsufficientFunds("Недостаточно монет на балансе")
                await self._ledger(conn, user_id, -wager, "ставка · слот-машина", now)
                await self._bump_quest(conn, user_id, "casino", now)

            result = spin(wager, is_free)
            payout = int(result.get("payout", 0))
            if payout < 0:
                raise InvalidBet("Выплата не может быть отрицательной")
            awarded = int(result.get("awarded_free_spins", 0)) if not is_free else 0
            chest_picks = int(result.get("awarded_chest_picks", 0)) if not is_free else 0
            remaining = max(0, available_free_spins - 1) if is_free else awarded
            stored_bet = wager if remaining else 0
            if chest_picks:
                from app.services.slot_machine import new_chest_board

                bonus_state = {
                    "status": "pending",
                    "total_picks": chest_picks,
                    "picks_remaining": chest_picks,
                    "wager": wager,
                    "scatter_count": int(result.get("scatter_count", 0)),
                    "board": new_chest_board(),
                    "opened": [],
                }
            elif not is_free:
                bonus_state = None
            round_amount = 0 if is_free else wager
            label = "Слот-машина · фри-спин" if is_free else "Слот-машина · платный спин"
            response = {
                **result,
                "game": "slots",
                "selection": "free_spin" if is_free else "paid_spin",
                "label": label,
                "amount": round_amount,
                "spin_bet": wager,
                "odds": round(payout / wager, 4) if wager else 0.0,
                "win": payout > 0 if is_free else payout > wager,
                "free_spin": is_free,
                "free_spins_remaining": remaining,
                "free_spin_bet": stored_bet,
                "slot_state": {
                    "free_spins": remaining,
                    "spin_bet": stored_bet,
                    "bonus": self._slot_bonus_payload(
                        json.dumps(bonus_state, ensure_ascii=False, separators=(",", ":"))
                        if bonus_state is not None else None
                    ),
                },
            }

            if payout:
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (payout, user_id),
                )
                await self._ledger(
                    conn,
                    user_id,
                    payout,
                    "выплата · слот-машина · фри-спин" if is_free else "выигрыш · слот-машина",
                    now,
                )

            await conn.execute(
                """INSERT INTO slot_states (user_id, free_spins, spin_bet, bonus_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     free_spins = excluded.free_spins,
                     spin_bet = excluded.spin_bet,
                     bonus_json = excluded.bonus_json,
                     updated_at = excluded.updated_at""",
                (
                    user_id, remaining, stored_bet,
                    json.dumps(bonus_state, ensure_ascii=False, separators=(",", ":"))
                    if bonus_state is not None else None,
                    now,
                ),
            )
            outcome_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            inserted = await conn.execute(
                """INSERT INTO casino_bets
                   (user_id, game, selection, label, amount, odds, outcome, win, payout, created_at)
                   VALUES (?, 'slots', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    response["selection"],
                    label,
                    round_amount,
                    response["odds"],
                    outcome_json,
                    1 if response["win"] else 0,
                    payout,
                    now,
                ),
            )
            round_id = int(inserted.lastrowid or 0)
            await self._remember_token(conn, user_id, "slot", tap, round_id)
            response["round"] = round_id
            response["balance"] = await self._balance_in(conn, user_id)
            return response

    async def pick_slot_chest(
        self, user_id: int, chest_index: int, token: str | None = None
    ) -> dict[str, Any]:
        """Open one already-generated chest and credit its prize exactly once."""
        tap = self._clean_token(token)
        now = int(time.time())
        async with self.transaction() as conn:
            duplicate = await self._claim_token(conn, user_id, "slot-chest", tap, now)
            if duplicate is not None:
                previous = duplicate or await self._previous_id(conn, user_id, "slot-chest", tap)
                row = await (await conn.execute(
                    "SELECT outcome FROM casino_bets WHERE id = ? AND user_id = ? AND game = 'slots'",
                    (previous, user_id),
                )).fetchone()
                if row is None:
                    raise InvalidBet("Этот выбор сундука уже обработан")
                response = json.loads(str(row["outcome"]))
                response["round"] = previous
                response["balance"] = await self._balance_in(conn, user_id)
                current = await (await conn.execute(
                    "SELECT free_spins, spin_bet, bonus_json FROM slot_states WHERE user_id = ?",
                    (user_id,),
                )).fetchone()
                response["slot_state"] = {
                    "free_spins": int(current["free_spins"]) if current else 0,
                    "spin_bet": int(current["spin_bet"]) if current else 0,
                    "bonus": self._slot_bonus_payload(
                        str(current["bonus_json"]) if current and current["bonus_json"] else None
                    ),
                }
                return response

            saved = await (await conn.execute(
                "SELECT free_spins, spin_bet, bonus_json FROM slot_states WHERE user_id = ?",
                (user_id,),
            )).fetchone()
            bonus = json.loads(str(saved["bonus_json"])) if saved and saved["bonus_json"] else None
            if not bonus or bonus.get("status") != "pending" or int(bonus.get("picks_remaining", 0)) <= 0:
                raise InvalidBet("Нет сундуков для открытия")
            board = bonus.get("board") or []
            if not 0 <= int(chest_index) < len(board):
                raise InvalidBet("Выбери закрытый сундук на поле")
            if any(int(item["chest"]) == int(chest_index) + 1 for item in bonus.get("opened", [])):
                raise InvalidBet("Этот сундук уже открыт")

            multiplier = float(board[int(chest_index)])
            wager = int(bonus["wager"])
            payout = int(wager * multiplier + 0.5)
            if payout:
                await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, user_id))
                await self._ledger(conn, user_id, payout, "выигрыш · слот · сундук Scatter", now)
            bonus.setdefault("opened", []).append({
                "chest": int(chest_index) + 1,
                "multiplier": multiplier,
                "payout": payout,
            })
            bonus["picks_remaining"] = max(0, int(bonus["picks_remaining"]) - 1)
            if bonus["picks_remaining"] == 0:
                bonus["status"] = "complete"
                bonus["board"] = []
            bonus_json = json.dumps(bonus, ensure_ascii=False, separators=(",", ":"))
            await conn.execute(
                "UPDATE slot_states SET bonus_json = ?, updated_at = ? WHERE user_id = ?",
                (bonus_json, now, user_id),
            )
            response = {
                "game": "slots",
                "selection": "chest_bonus",
                "label": "Слот-машина · сундук Scatter",
                "chest": int(chest_index) + 1,
                "multiplier": multiplier,
                "payout": payout,
                "win": payout > 0,
                "picks_remaining": int(bonus["picks_remaining"]),
                "bonus_complete": bonus["status"] == "complete",
                "slot_state": {
                    "free_spins": int(saved["free_spins"]),
                    "spin_bet": int(saved["spin_bet"]),
                    "bonus": self._slot_bonus_payload(bonus_json),
                },
            }
            outcome_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            inserted = await conn.execute(
                """INSERT INTO casino_bets
                   (user_id, game, selection, label, amount, odds, outcome, win, payout, created_at)
                   VALUES (?, 'slots', 'chest_bonus', ?, 0, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    response["label"],
                    multiplier,
                    outcome_json,
                    1 if payout else 0,
                    payout,
                    now,
                ),
            )
            round_id = int(inserted.lastrowid or 0)
            if tap:
                await self._remember_token(conn, user_id, "slot-chest", tap, round_id)
            response["round"] = round_id
            response["balance"] = await self._balance_in(conn, user_id)
            return response

    @staticmethod
    def _drunkard_payload(row: Any, user_id: int) -> dict[str, Any]:
        raw = json.loads(str(row["result_json"])) if row["result_json"] else None
        creator_id = int(row["creator_id"])
        opponent_id = int(row["opponent_id"]) if row["opponent_id"] is not None else None
        creator_side = creator_id == user_id
        side = "player" if creator_side else "opponent"
        opponent_name = str(row["opponent_name"] if creator_side else row["creator_name"])
        active = str(row["status"]) == "active"
        game_state = raw if active else None
        if active and game_state:
            hands = game_state.get("hands") or {}
            public_state = {
                "deck_size": int(row["deck_size"]),
                "rounds": int(game_state.get("rounds", 0)),
                "wars": int(game_state.get("wars", 0)),
                "cards_player": len(hands.get("player") or []),
                "cards_opponent": len(hands.get("opponent") or []),
                "last_battle": game_state.get("last_battle"),
            }
            result = None
        elif active:
            public_state = None
            result = None
        else:
            public_state = None
            result = raw
        outcome = None
        payout = 0
        if result is not None:
            winner = result.get("winner")
            outcome = "draw" if winner in (None, "draw") else "win" if winner == side else "loss"
            payout = int((result.get("payouts") or {}).get(str(user_id), 0))
        return {
            "id": int(row["id"]),
            "mode": str(row["mode"]),
            "status": str(row["status"]),
            "deck_size": int(row["deck_size"]),
            "stake": int(row["stake"]),
            "creator_id": creator_id,
            "opponent_id": opponent_id,
            "creator_name": str(row["creator_name"]),
            "opponent_name": str(row["opponent_name"]),
            "your_side": side,
            "your_outcome": outcome,
            "your_payout": payout,
            "other_name": opponent_name,
            "result": result,
            "game_state": public_state,
            "created_at": int(row["created_at"]),
        }

    async def drunkard_state(self, user_id: int) -> dict[str, Any]:
        """Приглашения и последняя партия игрока в «Пьяницу»."""
        cutoff = int(time.time()) - 24 * 3600
        incoming = await self._fetchall(
            "SELECT * FROM drunkard_games WHERE opponent_id = ? AND status = 'invited' "
            "AND created_at >= ? ORDER BY id DESC LIMIT 10",
            (user_id, cutoff),
        )
        outgoing = await self._fetchall(
            "SELECT * FROM drunkard_games WHERE creator_id = ? AND status = 'invited' "
            "AND created_at >= ? ORDER BY id DESC LIMIT 10",
            (user_id, cutoff),
        )
        active = await self._fetchone(
            "SELECT * FROM drunkard_games WHERE status = 'active' "
            "AND (creator_id = ? OR opponent_id = ?) ORDER BY id DESC LIMIT 1",
            (user_id, user_id),
        )
        last = await self._fetchone(
            "SELECT * FROM drunkard_games WHERE status = 'finished' "
            "AND (creator_id = ? OR opponent_id = ?) ORDER BY id DESC LIMIT 1",
            (user_id, user_id),
        )
        return {
            "incoming": [self._drunkard_payload(row, user_id) for row in incoming],
            "outgoing": [self._drunkard_payload(row, user_id) for row in outgoing],
            "active": self._drunkard_payload(active, user_id) if active is not None else None,
            "last": self._drunkard_payload(last, user_id) if last is not None else None,
        }

    async def _record_drunkard_casino(
        self,
        conn: Any,
        user_id: int,
        opponent_name: str,
        stake: int,
        payout: int,
        outcome: str,
        result: dict[str, Any],
        now: int,
    ) -> None:
        label = {
            "win": f"Победа над {opponent_name}",
            "loss": f"Поражение от {opponent_name}",
            "draw": "Ничья · ставка возвращена",
        }[outcome]
        await conn.execute(
            """INSERT INTO casino_bets
               (user_id, game, selection, label, amount, odds, outcome, win, payout, result_json, created_at)
               VALUES (?, 'drunkard', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                "vs:" + opponent_name,
                label,
                stake,
                payout / stake if stake else 0,
                outcome,
                1 if outcome == "win" else 0,
                payout,
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                now,
            ),
        )
        await self._bump_quest(conn, user_id, "casino", now)

    @staticmethod
    def _validate_drunkard(stake: int, deck_size: int) -> None:
        from app.services.drunkard import VALID_DECK_SIZES

        if stake < 10:
            raise InvalidBet("Минимальная ставка — 10 🪙")
        if int(deck_size) not in VALID_DECK_SIZES:
            raise InvalidBet("Выбери колоду на 24, 36 или 52 карты")

    async def play_drunkard_bot(
        self, user_id: int, stake: int, deck_size: int, token: str | None = None
    ) -> dict[str, Any]:
        """Создаёт интерактивную партию против компьютера; списывает ставку один раз."""
        from app.services.drunkard import new_game_state

        self._validate_drunkard(stake, deck_size)
        now = int(time.time())
        tap = self._clean_token(token)
        async with self.transaction() as conn:
            duplicate = await self._claim_token(conn, user_id, "drunkard-bot", tap, now)
            if duplicate is not None:
                game_id = duplicate or await self._previous_id(conn, user_id, "drunkard-bot", tap)
                row = await (await conn.execute(
                    "SELECT * FROM drunkard_games WHERE id = ? AND creator_id = ?",
                    (game_id, user_id),
                )).fetchone()
                if row is not None:
                    return {"game": self._drunkard_payload(row, user_id), "balance": await self._balance_in(conn, user_id)}
            active = await (await conn.execute(
                "SELECT * FROM drunkard_games WHERE status = 'active' "
                "AND (creator_id = ? OR opponent_id = ?) ORDER BY id DESC LIMIT 1",
                (user_id, user_id),
            )).fetchone()
            if active is not None:
                return {"game": self._drunkard_payload(active, user_id), "balance": await self._balance_in(conn, user_id)}
            debit = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (stake, user_id, stake),
            )
            if debit.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")
            await self._ledger(conn, user_id, -stake, "ставка · Пьяница · компьютер", now)
            state = new_game_state(int(deck_size))
            user = await self.get_user(user_id)
            created = await conn.execute(
                """INSERT INTO drunkard_games
                   (creator_id, opponent_id, creator_name, opponent_name, mode, deck_size, stake,
                    status, result_json, created_at, updated_at)
                   VALUES (?, NULL, ?, 'Компьютер', 'bot', ?, ?, 'active', ?, ?, ?) RETURNING id""",
                (
                    user_id, str(user["full_name"] or user["username"] or "Игрок"), int(deck_size),
                    stake, json.dumps(state, ensure_ascii=False, separators=(",", ":")), now, now,
                ),
            )
            created_row = await created.fetchone()
            game_id = int(created_row["id"]) if created_row else int(created.lastrowid or 0)
            if tap:
                await self._remember_token(conn, user_id, "drunkard-bot", tap, game_id)
            row = await (await conn.execute("SELECT * FROM drunkard_games WHERE id = ?", (game_id,))).fetchone()
            return {"game": self._drunkard_payload(row, user_id), "balance": await self._balance_in(conn, user_id)}

    async def create_drunkard_invite(
        self,
        user_id: int,
        opponent_ident: str,
        stake: int,
        deck_size: int,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Создаёт вызов по Telegram ID или username; ставка пока не списывается."""
        self._validate_drunkard(stake, deck_size)
        opponent = await self.find_user(opponent_ident)
        if opponent is None:
            raise InvalidBet("Игрок не найден. Он должен хотя бы раз открыть бота; проверь username или ID.")
        opponent_id = int(opponent["user_id"])
        if opponent_id == user_id:
            raise InvalidBet("Нельзя вызвать самого себя")
        creator = await self.get_user(user_id)
        creator_name = str(creator["full_name"] or creator["username"] or "Игрок")
        opponent_name = str(opponent["full_name"] or opponent["username"] or f"Игрок {opponent_id}")
        now = int(time.time())
        tap = self._clean_token(token)
        async with self.transaction() as conn:
            duplicate = await self._claim_token(conn, user_id, "drunkard-invite", tap, now)
            if duplicate is not None:
                game_id = duplicate or await self._previous_id(conn, user_id, "drunkard-invite", tap)
                row = await (await conn.execute(
                    "SELECT * FROM drunkard_games WHERE id = ? AND creator_id = ?",
                    (game_id, user_id),
                )).fetchone()
                if row is not None:
                    return self._drunkard_payload(row, user_id)
            existing = await (await conn.execute(
                "SELECT * FROM drunkard_games WHERE creator_id = ? AND opponent_id = ? "
                "AND status = 'invited' ORDER BY id DESC LIMIT 1",
                (user_id, opponent_id),
            )).fetchone()
            if existing is not None:
                return self._drunkard_payload(existing, user_id)
            if await self._balance_in(conn, user_id) < stake:
                raise InsufficientFunds("Недостаточно монет для ставки")
            created = await conn.execute(
                """INSERT INTO drunkard_games
                   (creator_id, opponent_id, creator_name, opponent_name, mode, deck_size, stake,
                    status, result_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'invite', ?, ?, 'invited', NULL, ?, ?) RETURNING id""",
                (user_id, opponent_id, creator_name, opponent_name, int(deck_size), stake, now, now),
            )
            row = await created.fetchone()
            game_id = int(row["id"]) if row else int(created.lastrowid or 0)
            if tap:
                await self._remember_token(conn, user_id, "drunkard-invite", tap, game_id)
            saved = await (await conn.execute("SELECT * FROM drunkard_games WHERE id = ?", (game_id,))).fetchone()
            return self._drunkard_payload(saved, user_id)

    async def accept_drunkard_invite(self, user_id: int, game_id: int) -> dict[str, Any]:
        """Принимает вызов: списывает ставки и запускает интерактивную партию."""
        from app.services.drunkard import new_game_state

        now = int(time.time())
        async with self.transaction() as conn:
            row = await (await conn.execute(
                "SELECT * FROM drunkard_games WHERE id = ?", (game_id,)
            )).fetchone()
            if row is None or int(row["opponent_id"] or 0) != user_id:
                raise InvalidBet("Приглашение не найдено")
            if str(row["status"]) in {"active", "finished"}:
                return {"game": self._drunkard_payload(row, user_id), "balance": await self._balance_in(conn, user_id)}
            if str(row["status"]) != "invited":
                raise InvalidBet("Это приглашение уже закрыто")
            if now - int(row["created_at"]) > 24 * 3600:
                raise InvalidBet("Приглашение просрочено")
            stake, deck_size = int(row["stake"]), int(row["deck_size"])
            creator_id = int(row["creator_id"])
            for player_id in (creator_id, user_id):
                active = await (await conn.execute(
                    "SELECT id FROM drunkard_games WHERE status = 'active' "
                    "AND (creator_id = ? OR opponent_id = ?) LIMIT 1",
                    (player_id, player_id),
                )).fetchone()
                if active is not None:
                    raise InvalidBet("У одного из игроков уже есть незавершённая партия")
                debit = await conn.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                    (stake, player_id, stake),
                )
                if debit.rowcount == 0:
                    raise InsufficientFunds("Одному из игроков не хватает монет для одинаковой ставки")
                await self._ledger(conn, player_id, -stake, "ставка · Пьяница · матч с игроком", now)
            state = new_game_state(deck_size)
            await conn.execute(
                "UPDATE drunkard_games SET status = 'active', result_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(state, ensure_ascii=False, separators=(",", ":")), now, game_id),
            )
            started = await (await conn.execute("SELECT * FROM drunkard_games WHERE id = ?", (game_id,))).fetchone()
            return {"game": self._drunkard_payload(started, user_id), "balance": await self._balance_in(conn, user_id)}

    async def _finish_drunkard_game(
        self, conn: Any, row: Any, state: dict[str, Any], now: int
    ) -> Any:
        """Расчёт выплат и запись в casino history происходят только при завершении."""
        creator_id = int(row["creator_id"])
        opponent_id = int(row["opponent_id"]) if row["opponent_id"] is not None else None
        stake = int(row["stake"])
        winner = state.get("winner")
        def outcome_for(side: str) -> str:
            return "draw" if winner in (None, "draw") else "win" if winner == side else "loss"
        payouts: dict[str, int] = {}
        participants = [("player", creator_id)]
        if opponent_id is not None:
            participants.append(("opponent", opponent_id))
        for side, player_id in participants:
            outcome = outcome_for(side)
            payout = stake if outcome == "draw" else stake * 2 if outcome == "win" else 0
            payouts[str(player_id)] = payout
            if payout:
                await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, player_id))
                await self._ledger(conn, player_id, payout, "выплата · Пьяница", now)
        hands = state["hands"]
        battle = state.get("last_battle") or {}
        pairs = battle.get("pairs") or []
        final_pair = pairs[-1] if pairs else {}
        result = {
            "deck_size": int(row["deck_size"]),
            "winner": winner,
            "rounds": int(state.get("rounds", 0)),
            "wars": int(state.get("wars", 0)),
            "cards_player": len(hands.get("player") or []),
            "cards_opponent": len(hands.get("opponent") or []),
            "last_player_card": final_pair.get("player"),
            "last_opponent_card": final_pair.get("opponent"),
            "last_battle": battle,
            "tie_reason": state.get("tie_reason"),
            "mode": str(row["mode"]),
            "stake": stake,
            "payouts": payouts,
            "creator_name": str(row["creator_name"]),
            "opponent_name": str(row["opponent_name"]),
        }
        for side, player_id in participants:
            other_name = str(row["opponent_name"] if side == "player" else row["creator_name"])
            await self._record_drunkard_casino(
                conn, player_id, other_name, stake, payouts[str(player_id)],
                outcome_for(side), result, now,
            )
        await conn.execute(
            "UPDATE drunkard_games SET status = 'finished', result_json = ?, updated_at = ?, settled_at = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False, separators=(",", ":")), now, now, int(row["id"])),
        )
        return await (await conn.execute("SELECT * FROM drunkard_games WHERE id = ?", (int(row["id"]),))).fetchone()

    async def advance_drunkard(self, user_id: int, game_id: int, token: str | None = None) -> dict[str, Any]:
        """Открывает ровно следующую раздачу; любые карты/исходы выбирает сервер."""
        from app.services.drunkard import advance_round

        now = int(time.time())
        tap = self._clean_token(token)
        async with self.transaction() as conn:
            row = await (await conn.execute(
                "SELECT * FROM drunkard_games WHERE id = ?", (game_id,)
            )).fetchone()
            if row is None or user_id not in {int(row["creator_id"]), int(row["opponent_id"] or 0)}:
                raise InvalidBet("Партия не найдена")
            if str(row["status"]) == "finished":
                return {"game": self._drunkard_payload(row, user_id), "balance": await self._balance_in(conn, user_id)}
            if str(row["status"]) != "active":
                raise InvalidBet("Партия ещё не началась")
            state = json.loads(str(row["result_json"]))
            if tap and tap == state.get("last_action_token"):
                return {"game": self._drunkard_payload(row, user_id), "balance": await self._balance_in(conn, user_id)}
            advance_round(state)
            if tap:
                state["last_action_token"] = tap
            if state.get("winner") is not None:
                saved = await self._finish_drunkard_game(conn, row, state, now)
            else:
                await conn.execute(
                    "UPDATE drunkard_games SET result_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(state, ensure_ascii=False, separators=(",", ":")), now, game_id),
                )
                saved = await (await conn.execute("SELECT * FROM drunkard_games WHERE id = ?", (game_id,))).fetchone()
            return {"game": self._drunkard_payload(saved, user_id), "balance": await self._balance_in(conn, user_id)}

    async def close_drunkard_invite(self, user_id: int, game_id: int) -> dict[str, Any]:
        """Создатель отменяет вызов, получатель отклоняет его; деньги ещё не списаны."""
        now = int(time.time())
        async with self.transaction() as conn:
            row = await (await conn.execute(
                "SELECT * FROM drunkard_games WHERE id = ? AND status = 'invited'", (game_id,)
            )).fetchone()
            if row is None:
                raise InvalidBet("Приглашение уже закрыто")
            if int(row["creator_id"]) == user_id:
                status = "cancelled"
            elif int(row["opponent_id"] or 0) == user_id:
                status = "declined"
            else:
                raise InvalidBet("Это не ваше приглашение")
            await conn.execute(
                "UPDATE drunkard_games SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, game_id),
            )
            saved = await (await conn.execute("SELECT * FROM drunkard_games WHERE id = ?", (game_id,))).fetchone()
            return self._drunkard_payload(saved, user_id)

    @staticmethod
    def _blackjack_payload(game_id: int, state: dict[str, Any], balance: int) -> dict[str, Any]:
        from app.services.blackjack import public_state

        return public_state(game_id, state, balance)

    async def blackjack_state(self, user_id: int) -> dict[str, Any] | None:
        """Последняя партия. Закрытая карта и shoe отдаются только в серверный слой."""
        row = await self._fetchone(
            "SELECT id, state_json FROM blackjack_games WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        if row is None:
            return None
        state = json.loads(str(row["state_json"]))
        return self._blackjack_payload(
            int(row["id"]), state, await self.get_balance(user_id)
        )

    async def _record_blackjack_settlement(
        self, conn: Any, user_id: int, state: dict[str, Any], now: int
    ) -> None:
        payout = int(state["payout"])
        total_wagered = int(state["total_wagered"])
        if payout:
            await conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, user_id)
            )
            await self._ledger(conn, user_id, payout, "выплата · Блэкджек", now)
        result = state.get("result") or {}
        label = str(result.get("label") or "Раунд завершён")
        await conn.execute(
            """
            INSERT INTO casino_bets
                (user_id, game, selection, label, amount, odds, outcome, win, payout, created_at)
            VALUES (?, 'blackjack', 'classic', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                "Блэкджек",
                total_wagered,
                payout / total_wagered if total_wagered else 0,
                label,
                1 if payout > total_wagered else 0,
                payout,
                now,
            ),
        )
        await self._bump_quest(conn, user_id, "casino", now)

    async def start_blackjack(
        self, user_id: int, stake: int, state: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        """Списывает основную ставку и атомарно сохраняет новую раздачу."""
        from app.services import blackjack

        if stake <= 0:
            raise InvalidBet("Сумма должна быть положительной")
        now = int(time.time())
        state["created_at"] = now
        tap = self._clean_token(token)
        async with self.transaction() as conn:
            duplicate = await self._claim_token(conn, user_id, "blackjack-start", tap, now)
            if duplicate is not None:
                previous = duplicate or await self._previous_id(
                    conn, user_id, "blackjack-start", tap
                )
                if previous:
                    row = await (await conn.execute(
                        "SELECT id, state_json FROM blackjack_games WHERE id = ? AND user_id = ?",
                        (previous, user_id),
                    )).fetchone()
                    if row is not None:
                        saved = json.loads(str(row["state_json"]))
                        return self._blackjack_payload(
                            int(row["id"]), saved, await self._balance_in(conn, user_id)
                        )
            async with conn.execute(
                "SELECT id, state_json FROM blackjack_games WHERE user_id = ? "
                "AND status IN ('insurance', 'playing') ORDER BY id DESC LIMIT 1",
                (user_id,),
            ) as cur:
                active = await cur.fetchone()
            if active is not None:
                active_id = int(active["id"])
                saved = json.loads(str(active["state_json"]))
                if tap:
                    await self._remember_token(conn, user_id, "blackjack-start", tap, active_id)
                return self._blackjack_payload(
                    active_id, saved, await self._balance_in(conn, user_id)
                )
            debit = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (stake, user_id, stake),
            )
            if debit.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")
            await self._ledger(conn, user_id, -stake, "ставка · Блэкджек · раздача", now)
            encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            created = await conn.execute(
                """
                INSERT INTO blackjack_games
                    (user_id, stake, status, state_json, payout, created_at, updated_at, settled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
                """,
                (
                    user_id,
                    stake,
                    str(state["status"]),
                    encoded,
                    int(state["payout"]),
                    now,
                    now,
                    now if state["status"] == "settled" else None,
                ),
            )
            created_row = await created.fetchone()
            game_id = int(created_row["id"]) if created_row else int(created.lastrowid or 0)
            if state["status"] == "settled":
                await self._record_blackjack_settlement(conn, user_id, state, now)
            if tap:
                await self._remember_token(conn, user_id, "blackjack-start", tap, game_id)
            balance = await self._balance_in(conn, user_id)
        return blackjack.public_state(game_id, state, balance)

    async def blackjack_action(
        self,
        user_id: int,
        game_id: int,
        action: str,
        token: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Атомарно применяет ход; повтор того же token не может взять вторую карту."""
        from app.services import blackjack

        now = int(time.time())
        tap = self._clean_token(token)
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM blackjack_games WHERE id = ? AND user_id = ?",
                (game_id, user_id),
            ) as cur:
                game = await cur.fetchone()
            if game is None:
                raise InvalidBet("Раздача не найдена")
            channel = f"blackjack-action-{game_id}"
            state = json.loads(str(game["state_json"]))
            if str(game["status"]) == "settled":
                balance = await self._balance_in(conn, user_id)
                return {
                    "game": blackjack.public_state(game_id, state, balance),
                    "balance": balance,
                }
            duplicate = await self._claim_token(conn, user_id, channel, tap, now)
            if duplicate is not None:
                balance = await self._balance_in(conn, user_id)
                return {
                    "game": blackjack.public_state(game_id, state, balance),
                    "balance": balance,
                }
            if expected_revision is not None and int(state.get("revision", 0)) != expected_revision:
                raise InvalidBet("Раздача уже обновилась — посмотри на новые карты и выбери ход ещё раз")
            try:
                extra = blackjack.apply_action(state, action)
            except ValueError as exc:
                raise InvalidBet(str(exc)) from exc
            if extra:
                debited = await conn.execute(
                    "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                    (extra, user_id, extra),
                )
                if debited.rowcount == 0:
                    raise InsufficientFunds("Не хватает монет на удвоение, сплит или страховку")
                reason = {
                    "double": "удвоение",
                    "split": "сплит",
                    "insurance": "страховка",
                }.get(action, action)
                await self._ledger(conn, user_id, -extra, f"ставка · Блэкджек · {reason}", now)
            status = str(state["status"])
            if status == "settled":
                await self._record_blackjack_settlement(conn, user_id, state, now)
            await conn.execute(
                """UPDATE blackjack_games SET status = ?, state_json = ?, payout = ?,
                   updated_at = ?, settled_at = ? WHERE id = ? AND user_id = ?""",
                (
                    status,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    int(state["payout"]),
                    now,
                    now if status == "settled" else None,
                    game_id,
                    user_id,
                ),
            )
            if tap:
                await self._remember_token(conn, user_id, channel, tap, game_id)
            balance = await self._balance_in(conn, user_id)
        return {"game": blackjack.public_state(game_id, state, balance), "balance": balance}

    async def mines_state(self, user_id: int) -> dict[str, Any] | None:
        """Последний раунд; расположение мин скрыто, пока он активен."""
        row = await self._fetchone(
            "SELECT * FROM mines_games WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        return self._mines_payload(row) if row is not None else None

    @staticmethod
    def _mines_payload(game: Any, balance: int | None = None) -> dict[str, Any]:
        from app.services import mines as mines_game

        status = str(game["status"])
        mine_count = int(game["mine_count"])
        opened = [int(cell) for cell in json.loads(str(game["opened_cells"] or "[]"))]
        mine_positions = [int(cell) for cell in json.loads(str(game["mine_positions"]))]
        safe_count = sum(cell not in mine_positions for cell in opened)
        settled = status in {"lost", "cashed", "cleared"}
        potential = (
            int(game["payout"]) if status in {"cashed", "cleared"}
            else mines_game.payout(int(game["stake"]), mine_count, safe_count)
            if status == "active" and safe_count else 0
        )
        payload: dict[str, Any] = {
            "id": int(game["id"]),
            "stake": int(game["stake"]),
            "mine_count": mine_count,
            "opened_cells": opened,
            "safe_count": safe_count,
            "status": status,
            "payout": int(game["payout"]),
            "potential_payout": potential,
            "multiplier": round(
                mines_game.multiplier(mine_count, safe_count) if safe_count else 0.0, 4
            ),
            "updated_at": int(game["updated_at"]),
        }
        if settled:
            payload["mine_positions"] = mine_positions
        if balance is not None:
            payload["balance"] = int(balance)
        return payload

    async def start_mines(
        self,
        user_id: int,
        stake: int,
        mine_count: int,
        mine_positions: list[int],
        token: str | None = None,
    ) -> dict[str, Any]:
        """Списывает ставку и атомарно сохраняет серверный расклад."""
        from app.services import mines as mines_game

        try:
            wager = mines_game.validate_stake(stake)
            mines = mines_game.validate_mine_count(mine_count)
            layout = mines_game.validate_layout(mines, mine_positions)
        except ValueError as exc:
            raise InvalidBet(str(exc)) from exc

        now = int(time.time())
        tap = self._clean_token(token)
        async with self.transaction() as conn:
            duplicate = await self._claim_token(conn, user_id, "mines-start", tap, now)
            if duplicate is not None:
                previous = duplicate or await self._previous_id(conn, user_id, "mines-start", tap)
                if previous:
                    row = await (await conn.execute(
                        "SELECT * FROM mines_games WHERE id = ? AND user_id = ?",
                        (previous, user_id),
                    )).fetchone()
                    if row is not None:
                        return self._mines_payload(row, await self._balance_in(conn, user_id))

            active = await (await conn.execute(
                "SELECT * FROM mines_games WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (user_id,),
            )).fetchone()
            if active is not None:
                game_id = int(active["id"])
                if tap:
                    await self._remember_token(conn, user_id, "mines-start", tap, game_id)
                return self._mines_payload(active, await self._balance_in(conn, user_id))

            debit = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (wager, user_id, wager),
            )
            if debit.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")
            await self._ledger(conn, user_id, -wager, "ставка · Мины · старт", now)
            created = await conn.execute(
                """INSERT INTO mines_games
                    (user_id, stake, mine_count, mine_positions, opened_cells, status,
                     payout, created_at, updated_at)
                   VALUES (?, ?, ?, ?, '[]', 'active', 0, ?, ?) RETURNING id""",
                (user_id, wager, mines, json.dumps(layout), now, now),
            )
            result = await created.fetchone()
            game_id = int(result["id"]) if result else int(created.lastrowid or 0)
            row = await (await conn.execute(
                "SELECT * FROM mines_games WHERE id = ?", (game_id,)
            )).fetchone()
            assert row is not None
            if tap:
                await self._remember_token(conn, user_id, "mines-start", tap, game_id)
            balance = await self._balance_in(conn, user_id)
            return self._mines_payload(row, balance)

    async def pick_mines(
        self,
        user_id: int,
        game_id: int,
        cell: int,
        expected_open_count: int,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Reveals one cell and settles a mine/complete board atomically."""
        from app.services import mines as mines_game

        now = int(time.time())
        tap = self._clean_token(token)
        async with self.transaction() as conn:
            row = await (await conn.execute(
                "SELECT * FROM mines_games WHERE id = ? AND user_id = ?", (game_id, user_id)
            )).fetchone()
            if row is None:
                raise InvalidBet("Раунд «Мины» не найден")
            channel = f"mines-pick-{game_id}"
            duplicate = await self._claim_token(conn, user_id, channel, tap, now)
            if duplicate is not None:
                return self._mines_payload(row, await self._balance_in(conn, user_id))
            if str(row["status"]) != "active":
                return self._mines_payload(row, await self._balance_in(conn, user_id))

            opened = [int(item) for item in json.loads(str(row["opened_cells"] or "[]"))]
            if len(opened) != int(expected_open_count):
                raise InvalidBet("Поле уже обновилось — проверь открытые клетки")
            if not 0 <= int(cell) < mines_game.CELLS or int(cell) in opened:
                raise InvalidBet("Выбери закрытую клетку на поле")
            mines = [int(item) for item in json.loads(str(row["mine_positions"]))]
            opened.append(int(cell))
            mine_count = int(row["mine_count"])
            safe_count = sum(item not in mines for item in opened)
            status = "active"
            payout = 0
            if int(cell) in mines:
                status = "lost"
            elif safe_count == mines_game.CELLS - mine_count:
                status = "cleared"
                payout = mines_game.payout(int(row["stake"]), mine_count, safe_count)
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, user_id)
                )
                await self._ledger(conn, user_id, payout, "выигрыш · Мины · все клетки", now)

            await conn.execute(
                "UPDATE mines_games SET opened_cells = ?, status = ?, payout = ?, updated_at = ? WHERE id = ?",
                (json.dumps(opened), status, payout, now, game_id),
            )
            if status in {"lost", "cleared"}:
                outcome = (
                    f"мина · {safe_count} безопасных" if status == "lost"
                    else f"все клетки · {safe_count} безопасных"
                )
                await self._record_mines_history(conn, row, safe_count, payout, outcome, now)
                await self._bump_quest(conn, user_id, "casino", now)
            updated = await (await conn.execute(
                "SELECT * FROM mines_games WHERE id = ?", (game_id,)
            )).fetchone()
            assert updated is not None
            if tap:
                await self._remember_token(conn, user_id, channel, tap, game_id)
            return self._mines_payload(updated, await self._balance_in(conn, user_id))

    async def cashout_mines(
        self,
        user_id: int,
        game_id: int,
        expected_open_count: int,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Cash out the server-calculated gross return exactly once."""
        from app.services import mines as mines_game

        now = int(time.time())
        tap = self._clean_token(token)
        async with self.transaction() as conn:
            row = await (await conn.execute(
                "SELECT * FROM mines_games WHERE id = ? AND user_id = ?", (game_id, user_id)
            )).fetchone()
            if row is None:
                raise InvalidBet("Раунд «Мины» не найден")
            channel = f"mines-cashout-{game_id}"
            duplicate = await self._claim_token(conn, user_id, channel, tap, now)
            if duplicate is not None or str(row["status"]) in {"cashed", "lost", "cleared"}:
                return self._mines_payload(row, await self._balance_in(conn, user_id))
            if str(row["status"]) != "active":
                raise InvalidBet("Раунд больше нельзя продолжить")
            opened = [int(item) for item in json.loads(str(row["opened_cells"] or "[]"))]
            if len(opened) != int(expected_open_count):
                raise InvalidBet("Поле уже обновилось — проверь открытые клетки")
            mines = [int(item) for item in json.loads(str(row["mine_positions"]))]
            safe_count = sum(item not in mines for item in opened)
            if safe_count < 1:
                raise InvalidBet("Сначала открой хотя бы одну безопасную клетку")
            payout = mines_game.payout(int(row["stake"]), int(row["mine_count"]), safe_count)
            await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, user_id))
            await self._ledger(
                conn, user_id, payout,
                f"выигрыш · Мины · забрано ×{payout / int(row['stake']):.2f}", now,
            )
            await conn.execute(
                "UPDATE mines_games SET status = 'cashed', payout = ?, updated_at = ? WHERE id = ?",
                (payout, now, game_id),
            )
            await self._record_mines_history(
                conn, row, safe_count, payout, f"забрано · {safe_count} безопасных", now
            )
            await self._bump_quest(conn, user_id, "casino", now)
            updated = await (await conn.execute(
                "SELECT * FROM mines_games WHERE id = ?", (game_id,)
            )).fetchone()
            assert updated is not None
            if tap:
                await self._remember_token(conn, user_id, channel, tap, game_id)
            return self._mines_payload(updated, await self._balance_in(conn, user_id))

    async def _record_mines_history(
        self, conn: Any, game: Any, safe_count: int, payout: int, outcome: str, now: int
    ) -> None:
        from app.services import mines as mines_game

        stake = int(game["stake"])
        multiplier = mines_game.multiplier(int(game["mine_count"]), safe_count) if safe_count else 1.0
        label = f"Мины · {int(game['mine_count'])} мин · {outcome}"
        await conn.execute(
            """INSERT INTO casino_bets
                (user_id, game, selection, label, amount, odds, outcome, win, payout, created_at)
                VALUES (?, 'mines', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(game["user_id"]), f"mines:{int(game['mine_count'])}", label, stake,
                multiplier, outcome, 1 if payout > stake else 0, int(payout), now,
            ),
        )

    async def ladder_state(self, user_id: int) -> dict[str, Any] | None:
        """Последняя партия: текущую дверь никогда не раскрываем до выбора игрока."""
        row = await self._fetchone(
            "SELECT * FROM ladder_games WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        if row is None:
            return None
        status = str(row["status"])
        round_no = int(row["round"])
        revealed = None
        if status in {"won", "lost", "cashed", "complete"}:
            revealed = (
                int(row["picked_index"])
                if status in {"won", "cashed", "complete"} and row["picked_index"] is not None
                else int(row["prize_index"])
            )
        return {
            "id": int(row["id"]),
            "stake": int(row["stake"]),
            "round": round_no,
            "windows": round_no + 1,
            "status": status,
            "picked_index": int(row["picked_index"]) if row["picked_index"] is not None else None,
            "revealed_index": revealed,
            "payout": int(row["payout"]),
            "updated_at": int(row["updated_at"]),
        }

    async def start_ladder(
        self, user_id: int, stake: int, prize_index: int
    ) -> dict[str, Any]:
        """Списывает ставку и создаёт партию; активную партию нельзя задвоить."""
        from app.services.ladder import MAX_ROUNDS, MIN_STAKE

        if stake < MIN_STAKE:
            raise InvalidBet(f"Минимальная ставка в «Лесенке» — {MIN_STAKE} 🪙")
        if not 0 <= prize_index < 2:
            raise InvalidBet("Неверный игровой раунд")
        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM ladder_games WHERE user_id = ? "
                "AND status IN ('choosing', 'won') ORDER BY id DESC LIMIT 1",
                (user_id,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None:
                return self._ladder_payload(existing, balance=await self._balance_in(conn, user_id))
            debit = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (stake, user_id, stake),
            )
            if debit.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")
            await self._ledger(conn, user_id, -stake, "ставка · Лесенка · старт", now)
            created = await conn.execute(
                """
                INSERT INTO ladder_games
                    (user_id, stake, round, prize_index, status, payout, created_at, updated_at)
                VALUES (?, ?, 1, ?, 'choosing', 0, ?, ?) RETURNING id
                """,
                (user_id, stake, prize_index, now, now),
            )
            row = await created.fetchone()
            game_id = int(row["id"]) if row else int(created.lastrowid or 0)
            async with conn.execute(
                "SELECT * FROM ladder_games WHERE id = ?", (game_id,)
            ) as cur:
                game = await cur.fetchone()
            assert game is not None
            balance = await self._balance_in(conn, user_id)
        return {**self._ladder_payload(game), "balance": balance, "max_rounds": MAX_ROUNDS}

    async def pick_ladder(
        self, user_id: int, game_id: int, picked_index: int, payout: int
    ) -> dict[str, Any]:
        """Выбирает дверь. Промах завершает игру; успех открывает cashout или продолжение."""
        from app.services.ladder import MAX_ROUNDS

        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM ladder_games WHERE id = ? AND user_id = ?", (game_id, user_id)
            ) as cur:
                game = await cur.fetchone()
            if game is None:
                raise InvalidBet("Партия «Лесенка» не найдена")
            if str(game["status"]) != "choosing":
                # Повторный тап безопасен: отдаём уже вычисленный результат без выплаты.
                return self._ladder_payload(game, balance=await self._balance_in(conn, user_id))
            round_no = int(game["round"])
            windows = round_no + 1
            if not 0 <= picked_index < windows:
                raise InvalidBet("Выбери одну из закрытых дверей")
            correct = picked_index == int(game["prize_index"])
            status = "won" if correct else "lost"
            final = correct and round_no == MAX_ROUNDS
            if final:
                status = "complete"
            await conn.execute(
                "UPDATE ladder_games SET picked_index = ?, status = ?, payout = ?, updated_at = ? WHERE id = ?",
                (picked_index, status, payout if correct else 0, now, game_id),
            )
            if not correct:
                await self._record_ladder_history(conn, game, round_no, 0, False, "промах", now)
            elif final:
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, user_id)
                )
                await self._ledger(
                    conn, user_id, payout, f"выигрыш · Лесенка · этаж {round_no}", now
                )
                await self._record_ladder_history(conn, game, round_no, payout, True, "полный проход", now)
                await self._bump_quest(conn, user_id, "casino", now)
            async with conn.execute(
                "SELECT * FROM ladder_games WHERE id = ?", (game_id,)
            ) as cur:
                updated = await cur.fetchone()
            assert updated is not None
            balance = await self._balance_in(conn, user_id)
        return {
            **self._ladder_payload(updated, balance=balance),
            "correct": correct,
            "revealed_index": int(game["prize_index"]),
            "final": final,
        }

    async def continue_ladder(
        self, user_id: int, game_id: int, prize_index: int
    ) -> dict[str, Any]:
        """Переходит на следующий этаж и прячет новый приз среди добавившихся дверей."""
        from app.services.ladder import MAX_ROUNDS

        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM ladder_games WHERE id = ? AND user_id = ?", (game_id, user_id)
            ) as cur:
                game = await cur.fetchone()
            if game is None or str(game["status"]) != "won":
                raise InvalidBet("Сначала нужно найти приз на текущем этаже")
            round_no = int(game["round"]) + 1
            if round_no > MAX_ROUNDS or not 0 <= prize_index < round_no + 1:
                raise InvalidBet("Лесенка уже закончилась")
            await conn.execute(
                "UPDATE ladder_games SET round = ?, prize_index = ?, picked_index = NULL, "
                "status = 'choosing', payout = 0, updated_at = ? WHERE id = ?",
                (round_no, prize_index, now, game_id),
            )
            async with conn.execute(
                "SELECT * FROM ladder_games WHERE id = ?", (game_id,)
            ) as cur:
                updated = await cur.fetchone()
            assert updated is not None
            balance = await self._balance_in(conn, user_id)
        return self._ladder_payload(updated, balance=balance)

    async def cashout_ladder(self, user_id: int, game_id: int) -> dict[str, Any]:
        """Забирает выплату после найденного приза — ровно один раз."""
        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM ladder_games WHERE id = ? AND user_id = ?", (game_id, user_id)
            ) as cur:
                game = await cur.fetchone()
            if game is None or str(game["status"]) != "won":
                raise InvalidBet("Сначала найди приз, чтобы забрать выплату")
            payout = int(game["payout"])
            round_no = int(game["round"])
            await conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, user_id)
            )
            await self._ledger(
                conn, user_id, payout, f"выигрыш · Лесенка · этаж {round_no}", now
            )
            await conn.execute(
                "UPDATE ladder_games SET status = 'cashed', updated_at = ? WHERE id = ?",
                (now, game_id),
            )
            await self._record_ladder_history(
                conn, game, round_no, payout, True, f"забрано на этаже {round_no}", now
            )
            await self._bump_quest(conn, user_id, "casino", now)
            async with conn.execute(
                "SELECT * FROM ladder_games WHERE id = ?", (game_id,)
            ) as cur:
                updated = await cur.fetchone()
            assert updated is not None
            balance = await self._balance_in(conn, user_id)
        return self._ladder_payload(updated, balance=balance)

    @staticmethod
    def _ladder_payload(game: Any, balance: int | None = None) -> dict[str, Any]:
        status = str(game["status"])
        payload = {
            "id": int(game["id"]),
            "stake": int(game["stake"]),
            "round": int(game["round"]),
            "windows": int(game["round"]) + 1,
            "status": status,
            "picked_index": int(game["picked_index"]) if game["picked_index"] is not None else None,
            "payout": int(game["payout"]),
            # скрываем случайное положение, пока раунд не сделан
            "revealed_index": (
                int(game["picked_index"])
                if status in {"won", "cashed", "complete"} and game["picked_index"] is not None
                else int(game["prize_index"]) if status == "lost" else None
            ),
        }
        if balance is not None:
            payload["balance"] = balance
        return payload

    async def _record_ladder_history(
        self, conn: Any, game: Any, round_no: int, payout: int, won: bool, outcome: str, now: int
    ) -> None:
        stake = int(game["stake"])
        from app.services.ladder import multiplier

        await conn.execute(
            """
            INSERT INTO casino_bets
                (user_id, game, selection, label, amount, odds, outcome, win, payout, created_at)
            VALUES (?, 'ladder', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(game["user_id"]),
                f"floor:{round_no}",
                f"Лесенка · этаж {round_no}/8 · {outcome}",
                stake,
                multiplier(round_no),
                outcome,
                1 if won else 0,
                payout,
                now,
            ),
        )

    async def crash_state(self, user_id: int, *, now: float | None = None) -> dict[str, Any] | None:
        """Return latest flight; automatically settle it if its hidden crash point passed."""
        from app.services.crash import multiplier_at

        moment = float(time.time() if now is None else now)
        game = await self._fetchone(
            "SELECT * FROM crash_games WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        if game is None:
            return None
        if str(game["status"]) == "flying":
            current = multiplier_at(float(game["started_at"]), moment)
            if current < float(game["crash_multiplier"]):
                return self._crash_payload(
                    game, current=current, balance=await self.get_balance(user_id), server_time=moment
                )
            # Only lock the database on settlement, not for every live multiplier poll.
            async with self.transaction() as conn:
                async with conn.execute(
                    "SELECT * FROM crash_games WHERE id = ? AND user_id = ?", (game["id"], user_id)
                ) as cur:
                    game = await cur.fetchone()
                if game is not None and str(game["status"]) == "flying":
                    await self._settle_crash_loss(conn, game, int(moment))
                async with conn.execute("SELECT * FROM crash_games WHERE id = ?", (int(game["id"]),)) as cur:
                    game = await cur.fetchone()
                    assert game is not None
        return self._crash_payload(game, balance=await self.get_balance(user_id), server_time=moment)

    async def start_crash(
        self, user_id: int, stake: int, crash_multiplier: float, *, now: float | None = None
    ) -> dict[str, Any]:
        """Debit stake and commit a private crash point in one transaction."""
        from app.services.crash import MIN_STAKE, multiplier_at

        if int(stake) < MIN_STAKE:
            raise InvalidBet(f"Минимальная ставка в «Самолётике» — {MIN_STAKE} 🪙")
        if not 1.0 <= float(crash_multiplier) <= 1_000_000.0:
            raise InvalidBet("Некорректная точка вылета")
        async with self.transaction() as conn:
            # Start the game clock only once its transaction has acquired the
            # write lock; queueing behind another write must not burn flight time.
            moment = float(time.time() if now is None else now)
            timestamp = int(moment)
            async with conn.execute(
                "SELECT * FROM crash_games WHERE user_id = ? AND status = 'flying' ORDER BY id DESC LIMIT 1",
                (user_id,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None:
                current = multiplier_at(float(existing["started_at"]), moment)
                if current >= float(existing["crash_multiplier"]):
                    await self._settle_crash_loss(conn, existing, timestamp)
                else:
                    return self._crash_payload(
                        existing, current=current, balance=await self._balance_in(conn, user_id),
                        server_time=moment,
                    )

            debit = await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (stake, user_id, stake),
            )
            if debit.rowcount == 0:
                raise InsufficientFunds("Недостаточно монет на балансе")
            await self._ledger(conn, user_id, -stake, "ставка · Самолётик", timestamp)
            await self._bump_quest(conn, user_id, "casino", timestamp)
            inserted = await conn.execute(
                """INSERT INTO crash_games
                    (user_id, stake, crash_multiplier, started_at, status, payout, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'flying', 0, ?, ?) RETURNING id""",
                (user_id, stake, float(crash_multiplier), moment, timestamp, timestamp),
            )
            created = await inserted.fetchone()
            game_id = int(created["id"]) if created else int(inserted.lastrowid or 0)
            async with conn.execute("SELECT * FROM crash_games WHERE id = ?", (game_id,)) as cur:
                game = await cur.fetchone()
                assert game is not None
            return self._crash_payload(
                game, current=1.0, balance=await self._balance_in(conn, user_id), server_time=moment
            )

    async def cashout_crash(
        self, user_id: int, game_id: int, *, now: float | None = None
    ) -> dict[str, Any]:
        """Cash out at current multiplier, or settle loss if the flight already ended."""
        from app.services.crash import multiplier_at
        from app.services.crash import payout as crash_payout

        # Cash-out is evaluated at request arrival, not after waiting for a write lock.
        moment = float(time.time() if now is None else now)
        timestamp = int(moment)
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM crash_games WHERE id = ? AND user_id = ?", (game_id, user_id)
            ) as cur:
                game = await cur.fetchone()
            if game is None:
                raise InvalidBet("Партия «Самолётик» не найдена")
            if str(game["status"]) != "flying":
                return self._crash_payload(
                    game, balance=await self._balance_in(conn, user_id), server_time=moment
                )
            current = multiplier_at(float(game["started_at"]), moment)
            if current >= float(game["crash_multiplier"]):
                await self._settle_crash_loss(conn, game, timestamp)
            else:
                payout = crash_payout(int(game["stake"]), current)
                await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, user_id))
                await self._ledger(conn, user_id, payout, f"выигрыш · Самолётик · ×{current:.2f}", timestamp)
                await conn.execute(
                    "UPDATE crash_games SET status = 'cashed', cashout_multiplier = ?, payout = ?, updated_at = ? WHERE id = ?",
                    (current, payout, timestamp, game_id),
                )
                await self._record_crash_history(conn, game, current, payout, timestamp, won=True)
            async with conn.execute("SELECT * FROM crash_games WHERE id = ?", (game_id,)) as cur:
                updated = await cur.fetchone()
                assert updated is not None
            return self._crash_payload(
                updated, balance=await self._balance_in(conn, user_id), server_time=moment
            )

    async def _settle_crash_loss(self, conn: Any, game: Any, now: int) -> None:
        """Idempotent settlement of an active flight that passed its crash point."""
        if str(game["status"]) != "flying":
            return
        crash_multiplier = float(game["crash_multiplier"])
        await conn.execute(
            "UPDATE crash_games SET status = 'crashed', payout = 0, updated_at = ? WHERE id = ?",
            (now, int(game["id"])),
        )
        await self._record_crash_history(conn, game, crash_multiplier, 0, now, won=False)

    async def _record_crash_history(
        self, conn: Any, game: Any, multiplier: float, payout: int, now: int, *, won: bool
    ) -> None:
        label = f"Самолётик · {'забрано' if won else 'вылет'} ×{multiplier:.2f}"
        await conn.execute(
            """INSERT INTO casino_bets
                (user_id, game, selection, label, amount, odds, outcome, win, payout, created_at)
                VALUES (?, 'crash', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(game["user_id"]), "cashout" if won else "crash", label,
                int(game["stake"]), float(multiplier), f"×{multiplier:.2f}",
                1 if won else 0, int(payout), now,
            ),
        )

    @staticmethod
    def _crash_payload(
        game: Any, *, current: float | None = None, balance: int | None = None,
        server_time: float | None = None,
    ) -> dict[str, Any]:
        status = str(game["status"])
        payload: dict[str, Any] = {
            "id": int(game["id"]),
            "stake": int(game["stake"]),
            "status": status,
            "started_at": float(game["started_at"]),
            "server_time": float(time.time() if server_time is None else server_time),
            "cashout_multiplier": float(game["cashout_multiplier"]) if game["cashout_multiplier"] is not None else None,
            "payout": int(game["payout"]),
        }
        if status == "flying":
            payload["current_multiplier"] = float(current if current is not None else 1.0)
        else:
            # Reveal the committed result only after the flight has settled.
            payload["crash_multiplier"] = float(game["crash_multiplier"])
        if balance is not None:
            payload["balance"] = int(balance)
        return payload

    async def casino_history(self, user_id: int, limit: int = 20) -> list[Any]:
        """Последние раунды игрока — для ленты «недавние ставки»."""
        return await self._fetchall(
            "SELECT * FROM casino_bets WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    async def casino_round_response(self, user_id: int, round_id: int) -> dict[str, Any] | None:
        """Полный исход обычного казино-раунда для идемпотентного HTTP-повтора."""
        row = await self._fetchone(
            "SELECT result_json FROM casino_bets WHERE id = ? AND user_id = ?",
            (round_id, user_id),
        )
        if row is None or not row["result_json"]:
            return None
        try:
            result = json.loads(str(row["result_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return result if isinstance(result, dict) else None

    async def casino_summary(self, user_id: int) -> Any:
        """Сводка по играм: раундов, поставлено, выиграно, чистый результат."""
        return await self._fetchone(
            """
            SELECT COUNT(*)                        AS rounds,
                   COALESCE(SUM(amount), 0)        AS staked,
                   COALESCE(SUM(payout), 0)        AS paid,
                   COALESCE(SUM(win), 0)           AS wins,
                   COALESCE(SUM(payout - amount), 0) AS net
            FROM casino_bets WHERE user_id = ?
            """,
            (user_id,),
        )

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
        await self._write(
            "UPDATE users SET last_chat_id = ? WHERE user_id = ?", (chat_id, user_id)
        )

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

    # В зачёт сезона идёт только игра: ставки, экспрессы, казино, возвраты.
    # Бонусы, задания и реферальные начисления — вне счёта недели.
    SEASON_REASONS = (
        "ставка ·",
        "выплата ·",
        "возврат ·",
        "выигрыш ·",
        "экспресс #",
        "колесо фортуны",
    )

    @classmethod
    def _season_filter(cls, column: str = "reason") -> tuple[str, tuple[Any, ...]]:
        clauses = " OR ".join(f"{column} LIKE ?" for _ in cls.SEASON_REASONS)
        return f"({clauses})", tuple(prefix + "%" for prefix in cls.SEASON_REASONS)

    @staticmethod
    def week_start(now: int | None = None) -> int:
        """Начало текущей недели (понедельник 00:00 UTC) — окно сезона."""
        moment = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
        monday = (moment - timedelta(days=moment.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return int(monday.timestamp())

    async def season_top(self, limit: int = 10, since: int | None = None) -> list[Any]:
        """Топ недели: чистая прибыль со ставок и экспрессов за текущую неделю."""
        start = self.week_start() if since is None else since
        where, args = self._season_filter("l.reason")
        return await self._fetchall(
            f"""
            SELECT l.user_id AS user_id, u.username AS username, u.full_name AS full_name,
                   SUM(l.delta) AS score, COUNT(*) AS moves
            FROM ledger l JOIN users u ON u.user_id = l.user_id
            WHERE l.created_at >= ? AND {where}
            GROUP BY l.user_id, u.username, u.full_name
            ORDER BY score DESC, l.user_id
            LIMIT ?
            """,
            (start, *args, limit),
        )

    async def season_rank(self, user_id: int, since: int | None = None) -> dict[str, Any] | None:
        """Место игрока в недельном зачёте — даже если он вне десятки."""
        start = self.week_start() if since is None else since
        where, args = self._season_filter("reason")
        mine = await self._fetchone(
            f"""
            SELECT COALESCE(SUM(delta), 0) AS score, COUNT(*) AS moves FROM ledger
            WHERE user_id = ? AND created_at >= ? AND {where}
            """,
            (user_id, start, *args),
        )
        score = int(mine["score"]) if mine is not None else 0
        better = await self._fetchone(
            f"""
            SELECT COUNT(*) AS n FROM (
                SELECT user_id, SUM(delta) AS score FROM ledger
                WHERE created_at >= ? AND {where}
                GROUP BY user_id
                HAVING SUM(delta) > ?
            ) AS ahead
            """,
            (start, *args, score),
        )
        return {
            "rank": (int(better["n"]) + 1) if better is not None else 1,
            "score": score,
            "moves": int(mine["moves"]) if mine is not None else 0,
        }

    async def player_stats(self, user_id: int) -> dict[str, Any]:
        """Итоги игрока с раздельным учётом списаний и бонусных ставок."""
        bets = await self._fetchall(
            """
            SELECT b.id, b.amount, b.payout, b.cashed_out,
                   m.status AS match_status, m.tournament,
                   EXISTS (SELECT 1 FROM tickets t WHERE t.bet_id = b.id) AS promo
            FROM bets b LEFT JOIN matches m ON m.id = b.match_id
            WHERE b.user_id = ?
            """,
            (user_id,),
        )
        expresses = await self._fetchall(
            """
            SELECT e.id, e.stake, e.payout, e.status,
                   EXISTS (SELECT 1 FROM tickets t WHERE t.express_id = e.id) AS promo
            FROM expresses e WHERE e.user_id = ?
            """,
            (user_id,),
        )
        casino = await self._fetchone(
            "SELECT COUNT(*) AS rounds, COALESCE(SUM(amount), 0) AS staked,"
            " COALESCE(SUM(payout), 0) AS paid FROM casino_bets WHERE user_id = ?",
            (user_id,),
        )

        cash_staked = promo_staked = 0
        settled_cash_staked = settled_promo_staked = 0
        cash_returned = promo_returned = 0
        won = settled = biggest = 0
        tournaments: dict[str, int] = {}

        for row in bets:
            promo = bool(row["promo"])
            amount = int(row["amount"])
            paid = int(row["payout"] or 0)
            if promo:
                promo_staked += amount
                if row["payout"] is not None:
                    settled_promo_staked += amount
                    promo_returned += paid
            else:
                cash_staked += amount
                if row["payout"] is not None:
                    settled_cash_staked += amount
                    cash_returned += paid
            if not bool(row["cashed_out"]):
                if row["payout"] is not None:
                    biggest = max(biggest, paid)
                match_status = str(row["match_status"] or "")
                if match_status in ("finished", "cancelled"):
                    settled += 1
                    if match_status == "finished" and paid > 0:
                        won += 1
                if row["tournament"]:
                    name = str(row["tournament"])
                    tournaments[name] = tournaments.get(name, 0) + 1

        for row in expresses:
            promo = bool(row["promo"])
            stake = int(row["stake"])
            paid = int(row["payout"] or 0)
            if promo:
                promo_staked += stake
                if row["status"] in ("won", "lost", "void"):
                    settled_promo_staked += stake
                    promo_returned += paid
            else:
                cash_staked += stake
                if row["status"] in ("won", "lost", "void"):
                    settled_cash_staked += stake
                    cash_returned += paid
            if row["payout"] is not None:
                biggest = max(biggest, paid)
            status = str(row["status"] or "")
            if status in ("won", "lost", "void"):
                settled += 1
                if status == "won":
                    won += 1

        casino_rounds = int(casino["rounds"]) if casino is not None else 0
        casino_staked = int(casino["staked"]) if casino is not None else 0
        casino_paid = int(casino["paid"]) if casino is not None else 0
        cash_staked += casino_staked
        settled_cash_staked += casino_staked
        cash_returned += casino_paid

        favourite = max(tournaments, key=lambda key: tournaments[key]) if tournaments else ""
        staked = cash_staked + promo_staked
        returned = cash_returned + promo_returned
        return {
            "bets": sum(not bool(row["cashed_out"]) for row in bets) + len(expresses),
            "settled": settled,
            "won": won,
            "winrate": round(won * 100 / settled) if settled else 0,
            # Combined totals kept for existing integrations; ROI uses only funds
            # actually staked from the player's balance (including casino rounds).
            "staked": staked,
            "returned": returned,
            "cash_staked": cash_staked,
            "cash_returned": cash_returned,
            "settled_cash_staked": settled_cash_staked,
            "promo_staked": promo_staked,
            "promo_returned": promo_returned,
            "settled_promo_staked": settled_promo_staked,
            "roi": round(
                (cash_returned - settled_cash_staked) * 100 / settled_cash_staked
            ) if settled_cash_staked
            else 0,
            "biggest_win": biggest,
            "favourite_tournament": favourite,
            "casino_rounds": casino_rounds,
            "casino_net": casino_paid - casino_staked,
        }

    async def rank_of_user(self, user_id: int) -> dict[str, Any] | None:
        """Место игрока в общем зачёте и его баланс — даже если он не попал в топ-10."""
        row = await self._fetchone("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            return None
        better = await self._fetchone(
            "SELECT COUNT(*) AS n FROM users WHERE balance > ?", (int(row["balance"]),)
        )
        return {
            "rank": int(better["n"]) + 1 if better is not None else 1,
            "balance": int(row["balance"]),
        }

    async def history(self, user_id: int, limit: int = 10) -> list[Any]:
        return await self._fetchall(
            "SELECT delta, reason, created_at FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    async def user_recent_events(self, user_id: int, limit: int = 10) -> list[Any]:
        return await self._fetchall(
            "SELECT name, payload, created_at FROM events WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 50))),
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
        await self._write(
            """
            INSERT INTO chats (chat_id, title, added_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
            """,
            (chat_id, title, int(time.time())),
        )

    async def set_autopost(self, chat_id: int, enabled: bool) -> None:
        await self._write(
            """
            INSERT INTO chats (chat_id, autopost, added_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET autopost = excluded.autopost
            """,
            (chat_id, 1 if enabled else 0, int(time.time())),
        )

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
            await self._write(
                """
                INSERT INTO matches (fonbet_id, tournament, team_a, team_b, start_time, sport,
                                     odds_a, odds_x, odds_b, score_a, score_b, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (fonbet_id, tournament, team_a, team_b, start_time, sport, odds_a, odds_x, odds_b,
                 score_a, score_b, now),
            )
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
            await self._write(
                """
                UPDATE matches SET odds_a = COALESCE(?, odds_a),
                                   odds_x = COALESCE(?, odds_x),
                                   odds_b = COALESCE(?, odds_b),
                                   start_time = ?, score_a = ?, score_b = ?
                WHERE id = ?
                """,
                (odds_a, odds_x, odds_b, start_time, new_score_a, new_score_b, existing["id"]),
            )
            refreshed = await self._fetchone("SELECT * FROM matches WHERE id = ?", (existing["id"],))
            return refreshed or existing, False

        if existing["status"] == "awaiting" and (score_a is not None or score_b is not None):
            # матч доигран, ждём подтверждения исхода — обновляем хотя бы счёт
            await self._write(
                "UPDATE matches SET score_a = ?, score_b = ? WHERE id = ?",
                (new_score_a, new_score_b, existing["id"]),
            )
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
        """Только матчи, которые идут прямо сейчас; ожидающие расчёта живут в админской очереди."""
        return await self._fetchall(
            """
            SELECT * FROM matches WHERE status = 'live'
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
        await self._write("UPDATE matches SET status = ? WHERE id = ?", (status, match_id))

    async def close_started_matches(self, now: int | None = None) -> list[Any]:
        """Закрывает приём ставок у матчей, которые уже начались. Возвращает затронутые."""
        now = now or int(time.time())
        started = await self._fetchall(
            "SELECT * FROM matches WHERE status = 'open' AND start_time <= ?", (now,)
        )
        async with self.transaction() as conn:
            for match in started:
                await conn.execute(
                    "UPDATE matches SET status = 'live' WHERE id = ?", (match["id"],)
                )
        return started

    async def set_card(self, match_id: int, chat_id: int, message_id: int) -> None:
        await self._write(
            "UPDATE matches SET posted_chat_id = ?, posted_message_id = ? WHERE id = ?",
            (chat_id, message_id, match_id),
        )

    async def mark_result_check(self, match_id: int, when: int | None = None) -> None:
        await self._write(
            "UPDATE matches SET last_result_check = ? WHERE id = ?",
            (int(time.time()) if when is None else when, match_id),
        )

    async def recent_matches(self, limit: int = 500) -> list[Any]:
        """Последние матчи — для поиска по названиям команд (без SQL-lower: кириллица)."""
        return await self._fetchall(
            "SELECT * FROM matches ORDER BY start_time DESC LIMIT ?", (limit,)
        )

    async def set_match_names_en(self, match_id: int, team_a_en: str, team_b_en: str) -> None:
        """Английские названия команд: по ним ищем финальный результат во внешнем фиде."""
        await self._write(
            "UPDATE matches SET team_a_en = ?, team_b_en = ? WHERE id = ?",
            (team_a_en, team_b_en, match_id),
        )

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
        """Матчи, которые давно идут (или ждут), а результата так и нет.

        `open` тоже здесь: событие могло пропасть из линии сразу после старта, и
        тогда приём ставок никто не закрыл — такой матч нельзя оставлять в линии.
        """
        threshold = int(time.time()) - hours * 3600
        return await self._fetchall(
            "SELECT * FROM matches WHERE status IN ('open', 'live', 'awaiting') "
            "AND start_time < ? ORDER BY start_time",
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
            await self._write(
                """
                INSERT INTO match_maps (match_id, fonbet_id, map_number, odds_a, odds_b,
                                        score_a, score_b, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (match_id, fonbet_id, map_number, odds_a, odds_b, score_a, score_b, now),
            )
            created = await self._fetchone(
                "SELECT * FROM match_maps WHERE fonbet_id = ?", (fonbet_id,)
            )
            assert created is not None
            return created, True

        if existing["status"] in ("open", "live"):
            await self._write(
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

    # ------------------------------------------------------- служебные значения
    async def get_meta(self, key: str) -> str | None:
        """Служебное значение приложения (переживает перезапуск)."""
        row = await self._fetchone("SELECT value FROM app_meta WHERE key = ?", (key,))
        return str(row["value"]) if row is not None else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._write(
            """
            INSERT INTO app_meta (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, str(value), int(time.time())),
        )

    async def get_meta_int(self, key: str, default: int = 0) -> int:
        raw = await self.get_meta(key)
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    async def set_map_score(self, map_id: int, score_a: int | None, score_b: int | None) -> None:
        """Счёт раундов карты (из внешнего фида): нужен для расчёта и для карточки."""
        await self._write(
            "UPDATE match_maps SET score_a = ?, score_b = ? WHERE id = ?",
            (score_a, score_b, map_id),
        )

    async def set_map_status(self, map_id: int, status: str) -> None:
        await self._write("UPDATE match_maps SET status = ? WHERE id = ?", (status, map_id))

    async def start_maps_of_match(self, match_id: int) -> list[Any]:
        """Матч начался — карты переезжают из линии в live (ставки при live_betting остаются)."""
        rows = await self._fetchall(
            "SELECT * FROM match_maps WHERE match_id = ? AND status = 'open'", (match_id,)
        )
        async with self.transaction() as conn:
            for row in rows:
                await conn.execute(
                    "UPDATE match_maps SET status = 'live' WHERE id = ?", (row["id"],)
                )
        return rows

    async def place_map_bet(
        self, map_id: int, user_id: int, chat_id: int | None, side: str, amount: int,
        *, token: str | None = None, ticket_id: int | None = None,
    ) -> tuple[int, float]:
        """Ставка на победителя конкретной карты. Возвращает (новый баланс, кэф)."""
        if side not in ("a", "b", "x"):
            raise InvalidBet("Сторона должна быть 1, X или 2")
        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        tap = self._clean_token(token)
        now = int(time.time())
        async with self.transaction() as conn:
            if await self._claim_token(conn, user_id, "bet", tap, now) is not None:
                return await self._repeat_bet(conn, user_id, "bet", tap, "bets")
            ticket = (
                await self._claim_freebet(int(ticket_id), user_id, amount, "single")
                if ticket_id else None
            )
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

            await self._spend_single(conn, user_id, amount, now, ticket)
            placed = await conn.execute(
                """
                INSERT INTO bets (match_id, user_id, chat_id, side, amount, odds,
                                  market, map_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'map', ?, ?) RETURNING id
                """,
                (int(match["id"]), user_id, chat_id, side, amount, float(odds), map_id, now),
            )
            created = await placed.fetchone()
            bet_id = int(created["id"]) if created else int(placed.lastrowid or 0)
            await self._remember_token(conn, user_id, "bet", tap, bet_id)
            await self._settle_single_ledger(
                conn, user_id, bet_id, amount,
                f"карта {int(card['map_number'])} · {match_tag(match)}", now, ticket,
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
                    conn, user_id, total, f"выплата · карта {int(card['map_number'])} · {match_tag(match)}"
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
        started = int(match["start_time"]) <= now
        if status == "live":
            return None if settings.live_betting else "Матч уже начался — ставки не принимаются"
        if status == "open":
            # матч уже начался, а фид ещё не перевёл его в live: принимать нельзя,
            # иначе игрок ставит по предматчевому кэфу на идущую игру
            if started:
                return "Матч уже начался — ставки не принимаются"
            return None
        return "Ставки по этому матчу закрыты"

    async def place_bet(
        self,
        match_id: int,
        user_id: int,
        chat_id: int | None,
        side: str,
        amount: int,
        *,
        token: str | None = None,
        ticket_id: int | None = None,
    ) -> tuple[int, float]:
        """Списывает ставку по текущему кэфу матча. Возвращает (новый баланс, кэф).

        `token` — метка тапа от клиента: повторный запрос с тем же токеном
        не списывает деньги второй раз, а отдаёт результат первой ставки.
        `ticket_id` — фрибет: ставка за счёт заведения, монеты не списываются.
        """
        if side not in ("a", "b", "x"):
            raise InvalidBet("Сторона должна быть 1, X или 2")
        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        tap = self._clean_token(token)
        now = int(time.time())
        ticket = None
        async with self.transaction() as conn:
            if await self._claim_token(conn, user_id, "bet", tap, now) is not None:
                return await self._repeat_bet(conn, user_id, "bet", tap, "bets")
            if ticket_id:
                ticket = await self._claim_freebet(int(ticket_id), user_id, amount, "single")
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if side == "x" and str(match["sport"]) not in {"football", "hockey"}:
                raise InvalidBet("Ничья доступна только в футболе или в основное время хоккея")
            refusal = self._betting_refusal(match, now)
            if refusal:
                raise MatchNotOpen(refusal)

            odds = {"a": match["odds_a"], "x": match["odds_x"], "b": match["odds_b"]}[side]
            if not odds:
                raise InvalidBet("На этот исход нет кэфа")

            await self._spend_single(conn, user_id, amount, now, ticket)

            inserted = await conn.execute(
                """
                INSERT INTO bets (match_id, user_id, chat_id, side, amount, odds, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (match_id, user_id, chat_id, side, amount, float(odds), now),
            )
            bet_id = int(inserted.lastrowid or 0)
            await self._remember_token(conn, user_id, "bet", tap, bet_id)
            await self._settle_single_ledger(
                conn, user_id, bet_id, amount, match_tag(match), now, ticket
            )
            await self._bump_quest(conn, user_id, "bet", now)
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
        *,
        token: str | None = None,
        ticket_id: int | None = None,
    ) -> tuple[int, float]:
        """Ставка на рынок футбольного матча (тотал, точный счёт, форы и т.д.).

        Кэф берётся из той же модели, что видит игрок, и фиксируется в ставке —
        дальнейшие пересчёты линии на уже сделанную ставку не влияют.
        """
        from app.services.football import markets_of, side_code

        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        tap = self._clean_token(token)
        now = int(time.time())
        async with self.transaction() as conn:
            if await self._claim_token(conn, user_id, "bet", tap, now) is not None:
                return await self._repeat_bet(conn, user_id, "bet", tap, "bets")
            ticket = (
                await self._claim_freebet(int(ticket_id), user_id, amount, "single")
                if ticket_id else None
            )
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

            await self._spend_single(conn, user_id, amount, now, ticket)
            placed = await conn.execute(
                """
                INSERT INTO bets (match_id, user_id, chat_id, side, amount, odds, market, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'football', ?) RETURNING id
                """,
                (match_id, user_id, chat_id, side_code(key, param), amount, odds, now),
            )
            created = await placed.fetchone()
            bet_id = int(created["id"]) if created else int(placed.lastrowid or 0)
            await self._remember_token(conn, user_id, "bet", tap, bet_id)
            await self._settle_single_ledger(
                conn, user_id, bet_id, amount,
                f"{market['label']} · {match_tag(match)}", now, ticket,
            )
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

            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ? AND cashed_out = 0", (match_id,)
            ) as cur:
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
                await self._ledger(conn, user_id, total, f"выплата · {match_tag(match)}")

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

    # --------------------------------------------------------------- хоккей
    async def place_hockey_bet(
        self,
        match_id: int,
        user_id: int,
        chat_id: int | None,
        key: str,
        param: str,
        amount: int,
        *,
        token: str | None = None,
        ticket_id: int | None = None,
    ) -> tuple[int, float]:
        """Ставка на рынок хоккейного матча (основное время, победа в матче, тотал)."""
        from app.services.hockey import markets_of, side_code

        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        tap = self._clean_token(token)
        now = int(time.time())
        async with self.transaction() as conn:
            if await self._claim_token(conn, user_id, "bet", tap, now) is not None:
                return await self._repeat_bet(conn, user_id, "bet", tap, "bets")
            ticket = (
                await self._claim_freebet(int(ticket_id), user_id, amount, "single")
                if ticket_id else None
            )
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if str(match["sport"]) != "hockey":
                raise InvalidBet("Это не хоккейный матч")
            if str(match["status"]) != "open" or int(match["start_time"]) <= now:
                raise MatchNotOpen("Матч начался — роспись закрыта")

            market = next(
                (m for m in markets_of(match) if m["key"] == key and m["param"] == param), None
            )
            if market is None:
                raise InvalidBet("Такого рынка нет в линии")
            odds = float(market["odds"])

            await self._spend_single(conn, user_id, amount, now, ticket)
            placed = await conn.execute(
                """
                INSERT INTO bets (match_id, user_id, chat_id, side, amount, odds, market, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'hockey', ?) RETURNING id
                """,
                (match_id, user_id, chat_id, side_code(key, param), amount, odds, now),
            )
            created = await placed.fetchone()
            bet_id = int(created["id"]) if created else int(placed.lastrowid or 0)
            await self._remember_token(conn, user_id, "bet", tap, bet_id)
            from app.services.hockey import market_label

            await self._settle_single_ledger(
                conn, user_id, bet_id, amount,
                f"{market_label(key, param)} · {match_tag(match)}", now, ticket,
            )
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur2:
                row = await cur2.fetchone()
        return (int(row["balance"]) if row else 0), odds

    async def settle_hockey(
        self,
        match_id: int,
        reg_a: int,
        reg_b: int,
        final_a: int,
        final_b: int,
        source: str = "auto",
    ) -> dict[str, Any]:
        """Расчёт хоккея: основное время — по счёту трёх периодов, победа в матче — с ОТ.

        Два счёта нужны потому, что это разные рынки: 2:2 в основное время — это
        ничья для рынка «Основное время» и чья-то победа для рынка «Победа в матче»
        (после овертайма или буллитов).
        """
        from app.services.hockey import market_label, market_outcome, parse_side

        if final_a == final_b:
            raise InvalidBet("Хоккей не заканчивается вничью: нужен счёт с овертаймом")

        reg_winner = "a" if reg_a > reg_b else ("b" if reg_b > reg_a else "x")
        winner = "a" if final_a > final_b else "b"

        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if str(match["status"]) in ("finished", "cancelled"):
                raise MatchAlreadySettled("Матч уже рассчитан или отменён")

            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ? AND cashed_out = 0", (match_id,)
            ) as cur:
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
                    # ставка на исход матча из экспресса/старого интерфейса: считаем по основному времени
                    outcome = "win" if side == reg_winner else "lose"
                    label = {"a": "П1 (основное время)", "x": "X (основное время)",
                             "b": "П2 (основное время)"}.get(side, side)
                else:
                    outcome = market_outcome(parsed[0], parsed[1], reg_a, reg_b, final_a, final_b)
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
                await self._ledger(conn, user_id, total, f"выплата · {match_tag(match)}")

            await conn.execute(
                """
                UPDATE matches SET status = 'finished', winner = ?, result_source = ?,
                                   score_a = ?, score_b = ?, reg_a = ?, reg_b = ?, finished_at = ?
                WHERE id = ?
                """,
                (winner, source, final_a, final_b, reg_a, reg_b, int(time.time()), match_id),
            )
            # ноги экспресса — это «победа команды» без варианта ничьей, поэтому
            # считаем их по итоговому счёту: иначе команда, выигравшая в овертайме,
            # считалась бы проигравшей ногой экспресса
            expresses = await self._resolve_expresses(conn, match_id, winner)

        return {
            "team": str(match["team_a"] if winner == "a" else match["team_b"]),
            "score": f"{final_a}:{final_b}",
            "reg_score": f"{reg_a}:{reg_b}",
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
            "sport": "hockey",
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
        token: str | None = None,
        ticket_id: int | None = None,
    ) -> tuple[int, float, int]:
        """Ставит экспресс. legs: [{'match_id': int, 'side': 'a'|'b'}].

        Возвращает (express_id, итоговый кэф, новый баланс).
        `token` защищает от двойного списания при повторном тапе.
        `ticket_id` — оплата билетом с колеса: монеты со счёта не списываются,
        но и сумма ставки должна совпадать с номиналом билета.
        """
        from app.services.legs import odds as leg_odds

        if len(legs) < 2:
            raise InvalidBet("В экспрессе должно быть минимум 2 исхода")
        if stake <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        match_ids = [int(leg["match_id"]) for leg in legs]
        if len(set(match_ids)) != len(match_ids):
            raise InvalidBet("Нельзя брать два исхода одного матча в один экспресс")

        tap = self._clean_token(token)
        now = int(time.time())
        # билет проверяем до списания: он должен быть свой, живой и нужного вида
        ticket = None
        if ticket_id:
            ticket = await self._claim_freebet(int(ticket_id), user_id, stake, "express")
        async with self.transaction() as conn:
            if await self._claim_token(conn, user_id, "express", tap, now) is not None:
                previous = await self._previous_id(conn, user_id, "express", tap)
                row = await (
                    await conn.execute("SELECT odds FROM expresses WHERE id = ?", (previous,))
                ).fetchone()
                return previous, float(row["odds"]) if row is not None else 0.0, await self._balance_in(conn, user_id)
            total_odds = 1.0
            prepared: list[tuple[int, str, float]] = []
            for leg in legs:
                # исход матча (a/x/b) или рынок (f:… футбол, h:… хоккей):
                # кэф берём из базы, а не из запроса клиента
                side = str(leg["side"] or "")
                if not side:
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
                odds = leg_odds(match, side)
                if not odds:
                    raise InvalidBet(f"На матч #{match['id']} нет кэфа на этот исход")
                total_odds *= float(odds)
                prepared.append((int(match["id"]), side, float(odds)))

            if max_odds and total_odds > max_odds:  # 0/None — потолка нет
                raise InvalidBet(
                    f"Итоговый кэф {total_odds:.2f} выше лимита {max_odds:.0f} — убери лишние исходы"
                )

            if ticket is not None:
                # билет гасим атомарно: два тапа не могут потратить его дважды
                spent = await conn.execute(
                    "UPDATE tickets SET used_at = ? WHERE id = ? AND used_at IS NULL",
                    (now, int(ticket["id"])),
                )
                if spent.rowcount == 0:
                    raise InvalidBet("Билет уже использован")
            else:
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
            await self._remember_token(conn, user_id, "express", tap, express_id)

            for match_id, side, odds in prepared:
                await conn.execute(
                    "INSERT INTO express_items (express_id, match_id, side, odds, result) VALUES (?, ?, ?, ?, 'pending')",
                    (express_id, match_id, side, odds),
                )

            if ticket is not None:
                await conn.execute(
                    "UPDATE tickets SET express_id = ? WHERE id = ?",
                    (express_id, int(ticket["id"])),
                )
                # движение нулевое, но в истории видно, что ставка оплачена билетом
                await self._ledger(conn, user_id, 0, f"экспресс #{express_id} · билет", now)
            else:
                await self._ledger(conn, user_id, -stake, f"экспресс #{express_id}", now)
            await self._bump_quest(conn, user_id, "express", now)
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
            SELECT i.*, m.team_a, m.team_b, m.sport, m.status AS match_status, m.winner AS match_winner
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
        """Обновляет ноги экспрессов после расчёта матча (winner=None — матч отменён).

        Нога — это не обязательно «победа команды»: в экспрессе может стоять рынок
        (тотал, фора, победа с ОТ), поэтому исход ноги определяет `app.services.legs`
        по счёту уже рассчитанного матча.
        """
        from app.services.legs import outcome as leg_outcome

        async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
            match_row = await cur.fetchone()

        async with conn.execute(
            """
            SELECT i.*, e.status AS express_status FROM express_items i
            JOIN expresses e ON e.id = i.express_id
            WHERE i.match_id = ?
            """,
            (match_id,),
        ) as cur:
            items = list(await cur.fetchall())

        affected: set[int] = set()
        for item in items:
            result = (
                leg_outcome(match_row, str(item["side"]), winner)
                if match_row is not None
                else ("void" if winner is None else "lost")
            )
            # ногу помечаем всегда — даже если экспресс уже рассчитан по другой ноге:
            # иначе в истории у него навсегда висит «ждёт»
            await conn.execute(
                "UPDATE express_items SET result = ? WHERE id = ?", (result, item["id"])
            )
            if str(item["express_status"]) == "open":
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
            WHERE b.user_id = ? AND b.cashed_out = 0 AND (
                -- ставка на исход матча: CS2, футбол и хоккей (у каждого свой market)
                (b.market IN ('match', 'football', 'hockey')
                 AND m.status IN ('open', 'live', 'awaiting'))
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
                   b.market, b.cashed_out, mm.map_number,
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

    async def trace_bet(self, bet_id: int) -> list[dict[str, Any]]:
        """Read-only trace for sports singles/maps, expresses and casino rounds."""
        traces: list[dict[str, Any]] = []
        single = await self._fetchone(
            """
            SELECT b.id, b.user_id, b.match_id, b.market, b.side, b.amount, b.odds,
                   b.payout, b.cashed_out, b.created_at,
                   m.tournament, m.team_a, m.team_b, m.status AS match_status,
                   m.winner AS match_winner, m.start_time,
                   mm.map_number, mm.status AS map_status, mm.winner AS map_winner,
                   t.id AS ticket_id, t.amount AS ticket_amount,
                   t.source AS ticket_source, t.used_at AS ticket_used_at
            FROM bets b
            JOIN matches m ON m.id = b.match_id
            LEFT JOIN match_maps mm ON mm.id = b.map_id
            LEFT JOIN tickets t ON t.id = (
                SELECT MIN(t2.id) FROM tickets t2 WHERE t2.bet_id = b.id
            )
            WHERE b.id = ?
            """,
            (bet_id,),
        )
        if single is not None:
            traces.append({"kind": "single", "row": dict(single)})

        express = await self._fetchone(
            "SELECT id, user_id, stake, odds, status, payout, created_at, settled_at "
            "FROM expresses WHERE id = ?",
            (bet_id,),
        )
        if express is not None:
            legs = await self._fetchall(
                """
                SELECT i.match_id, i.side, i.odds, i.result,
                       m.tournament, m.team_a, m.team_b, m.status AS match_status,
                       m.winner AS match_winner
                FROM express_items i JOIN matches m ON m.id = i.match_id
                WHERE i.express_id = ? ORDER BY i.id
                """,
                (bet_id,),
            )
            traces.append({"kind": "express", "row": dict(express), "legs": [dict(x) for x in legs]})

        casino = await self._fetchone(
            """
            SELECT id, user_id, game, selection, label, amount, odds, outcome, win,
                   payout, created_at
            FROM casino_bets WHERE id = ?
            """,
            (bet_id,),
        )
        if casino is not None:
            traces.append({"kind": "casino", "row": dict(casino)})
        return traces

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
                "SELECT * FROM bets WHERE match_id = ? AND market = 'match' AND cashed_out = 0",
                (match_id,)
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
                await self._ledger(conn, user_id, total, f"выплата · {match_tag(match)}")

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
            match = await _match_of_card(conn, card)

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
                    f"откат возврата · карта {int(card['map_number'])} · {match_tag(match)}",
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

            match = await _match_of_card(conn, card)

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
                    f"возврат · карта {int(card['map_number'])} · {match_tag(match)}",
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
                "SELECT * FROM bets WHERE match_id = ? AND market = 'match' AND cashed_out = 0",
                (match_id,)
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
                await self._ledger(conn, int(bet["user_id"]), amount, f"возврат · {match_tag(match)}")

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
                "SELECT * FROM bets WHERE match_id = ? AND market <> 'map' AND cashed_out = 0",
                (match_id,)
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
                    conn, int(bet["user_id"]), -payout, f"сторно расчёта · {match_tag(match)}"
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
                "SELECT * FROM bets WHERE match_id = ? AND market <> 'map' AND cashed_out = 0",
                (match_id,)
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
                    conn, int(bet["user_id"]), amount, f"возврат · {match_tag(match)} ({reason})"
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
                "SELECT * FROM bets WHERE match_id = ? AND market = 'match' AND cashed_out = 0",
                (match_id,)
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
                    conn, int(bet["user_id"]), -payout, f"откат возврата · {match_tag(match)}"
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

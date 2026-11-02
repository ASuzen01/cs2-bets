"""Слой данных: SQLite (локально) или Postgres/Neon (хостинг).

Модель:
  matches — реальные матчи CS2 из линии Fonbet (один ряд на событие), кэфы на победителя;
  bets    — ставки с зафиксированным на момент ставки кэфом (fixed odds);
  users   — виртуальные балансы;
  ledger  — история операций.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app.db.backends import (
    SCHEMA_POSTGRES,
    SCHEMA_SQLITE,
    PostgresBackend,
    SQLiteBackend,
)


class BettingError(Exception):
    """Игровая ошибка, текст можно показывать пользователю."""


class MatchNotFound(BettingError):
    pass


class MatchNotOpen(BettingError):
    pass


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
            self._backend = backend
            return

        backend = await SQLiteBackend.open(self._path)
        # миграция старой схемы (ручные матчи) — ДО создания индексов новой схемы
        if await self._has_incompatible_matches(backend):
            await backend.executescript("DROP TABLE IF EXISTS bets; DROP TABLE IF EXISTS matches;")
        await backend.executescript(SCHEMA_SQLITE)
        await backend.commit()
        self._backend = backend

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
    ) -> tuple[Any, bool]:
        """Возвращает (матч, создан_ли_сейчас)."""
        now = int(time.time())
        existing = await self._fetchone("SELECT * FROM matches WHERE fonbet_id = ?", (fonbet_id,))
        if existing is None:
            await self.conn.execute(
                """
                INSERT INTO matches (fonbet_id, tournament, team_a, team_b, start_time,
                                     odds_a, odds_b, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (fonbet_id, tournament, team_a, team_b, start_time, odds_a, odds_b, now),
            )
            await self.conn.commit()
            created = await self._fetchone(
                "SELECT * FROM matches WHERE fonbet_id = ?", (fonbet_id,)
            )
            assert created is not None
            return created, True

        # у открытого матча обновляем кэфы (ставки уже сделанные сохраняют свой кэф)
        if existing["status"] == "open":
            await self.conn.execute(
                "UPDATE matches SET odds_a = ?, odds_b = ?, start_time = ? WHERE id = ?",
                (odds_a, odds_b, start_time, existing["id"]),
            )
            await self.conn.commit()
            refreshed = await self._fetchone("SELECT * FROM matches WHERE id = ?", (existing["id"],))
            return refreshed or existing, False
        return existing, False

    async def get_match(self, match_id: int) -> Any | None:
        return await self._fetchone("SELECT * FROM matches WHERE id = ?", (match_id,))

    async def open_matches(self, limit: int = 10) -> list[Any]:
        return await self._fetchall(
            "SELECT * FROM matches WHERE status = 'open' ORDER BY start_time LIMIT ?", (limit,)
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
            "UPDATE matches SET last_result_check = ? WHERE id = ?", (when or int(time.time()), match_id)
        )
        await self.conn.commit()

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
    async def place_bet(
        self, match_id: int, user_id: int, chat_id: int | None, side: str, amount: int
    ) -> tuple[int, float]:
        """Списывает ставку по текущему кэфу матча. Возвращает (новый баланс, кэф)."""
        if side not in ("a", "b"):
            raise InvalidBet("Сторона должна быть 1 или 2")
        if amount <= 0:
            raise InvalidBet("Сумма должна быть положительной")

        now = int(time.time())
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if match["status"] != "open":
                raise MatchNotOpen("Ставки по этому матчу закрыты")
            if int(match["start_time"]) <= now:
                raise MatchNotOpen("Матч уже начался — ставки не принимаются")

            odds = match["odds_a"] if side == "a" else match["odds_b"]
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
                if match["status"] != "open":
                    raise MatchNotOpen(f"Ставки на матч #{match['id']} закрыты")
                if int(match["start_time"]) <= now:
                    raise MatchNotOpen(f"Матч #{match['id']} уже начался")
                odds = match["odds_a"] if side == "a" else match["odds_b"]
                if not odds:
                    raise InvalidBet(f"На матч #{match['id']} нет кэфа на этот исход")
                total_odds *= float(odds)
                prepared.append((int(match["id"]), side, float(odds)))

            if max_odds is not None and total_odds > max_odds:
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
            SELECT b.*, m.team_a, m.team_b, m.start_time, m.status, m.fonbet_id
            FROM bets b JOIN matches m ON m.id = b.match_id
            WHERE b.user_id = ? AND m.status IN ('open', 'live', 'awaiting')
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
                   m.tournament, m.team_a, m.team_b, m.start_time,
                   m.status AS match_status, m.winner, m.finished_at
            FROM bets b
            JOIN matches m ON m.id = b.match_id
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

            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ?", (match_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            total_stake = sum(int(bet["amount"]) for bet in bets)
            paid = 0
            payouts: dict[int, int] = {}

            for bet in bets:
                amount = int(bet["amount"])
                if bet["side"] == winner:
                    payout = round(amount * float(bet["odds"]))
                    paid += payout
                    payouts[int(bet["user_id"])] = payouts.get(int(bet["user_id"]), 0) + payout
                else:
                    payout = 0
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
            "expresses": expresses,
        }

    async def cancel_match(self, match_id: int) -> dict[str, Any]:
        async with self.transaction() as conn:
            async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
                match = await cur.fetchone()
            if match is None:
                raise MatchNotFound("Матч не найден")
            if match["status"] in ("finished", "cancelled"):
                raise MatchAlreadySettled("Матч уже рассчитан или отменён")

            async with conn.execute(
                "SELECT * FROM bets WHERE match_id = ?", (match_id,)
            ) as cur:
                bets = list(await cur.fetchall())

            refunded = 0
            for bet in bets:
                amount = int(bet["amount"])
                refunded += amount
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

        return {"refunded": refunded, "bets": len(bets), "expresses": expresses}

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

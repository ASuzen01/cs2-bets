"""Одновременные записи: два игрока (или один в двух вкладках) жмут «поставить» в одну секунду.

На процесс одно соединение с базой, поэтому параллельные запросы раньше роняли
`BEGIN` с «cannot start a transaction within a transaction» — игрок видел 500.
Теперь записи идут через замок `Database.transaction()` / `Database._write()`,
а лишние ставки отбиваются нормальной ошибкой «Недостаточно монет».
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.db import Database
from app.db.database import InsufficientFunds


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "concurrency.db", start_balance=150)
    await database.connect()
    await database.upsert_user(1, "user1", "User 1")
    yield database
    await database.close()


async def _match(db: Database) -> int:
    row, _ = await db.upsert_match(
        fonbet_id=777,
        tournament="Counter-Strike. IEM. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + 3600,
        odds_a=2.0,
        odds_b=2.0,
    )
    return int(row["id"])


async def test_parallel_bets_serialize_without_500(db: Database) -> None:
    """4 ставки по 50 при балансе 150: три проходят, четвёртая — «мало монет», не исключение БД."""
    match_id = await _match(db)

    results = await asyncio.gather(
        *(db.place_bet(match_id, 1, None, "a", 50) for _ in range(4)),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], InsufficientFunds)
    assert await db.get_balance(1) == 0
    assert len(await db.user_bets_all(1)) == 3
    assert db.conn._conn.in_transaction is False


async def test_parallel_casino_rounds_serialize_without_500(db: Database) -> None:
    """Те же 4 запроса, но в рулетку: баланс не уходит в минус, висящей транзакции нет."""
    rounds = [
        db.play_casino(
            1, game="roulette", selection="red", label="Красное", amount=50, odds=2.0,
            outcome="17 — чёрное", won=False, payout=0,
        )
        for _ in range(4)
    ]

    results = await asyncio.gather(*rounds, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], InsufficientFunds)
    # 150 - 3*50 = 0, четвёртый раунд не состоялся
    assert await db.get_balance(1) == 0
    assert len(await db.casino_history(1, limit=10)) == 3
    assert db.conn._conn.in_transaction is False


async def test_nested_transaction_joins_outer(db: Database) -> None:
    """Вложенный transaction() из той же задачи не открывает второй BEGIN."""
    match_id = await _match(db)
    await db.set_status(match_id, "live")

    async with db.transaction() as conn:
        await conn.execute("UPDATE matches SET score_a = 5 WHERE id = ?", (match_id,))
        # внутренний вызов, который сам открывает транзакцию
        await db.set_map_score(1, 1, 0)

    match = await db.get_match(match_id)
    assert int(match["score_a"]) == 5
    assert db.conn._conn.in_transaction is False

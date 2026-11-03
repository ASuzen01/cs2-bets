"""Резервные копии: дамп, проверка целостности и полное восстановление."""

from __future__ import annotations

import gzip
import json

import pytest

from app.db import Database
from app.db.database import BackupError
from app.services.backup import decode, encode, make_dump, restore


async def _seed(db: Database) -> None:
    await db.upsert_user(1, "sap", "Антон")
    await db.upsert_user(2, "vasya", "Вася")
    row, _ = await db.upsert_match(
        fonbet_id=555,
        tournament="Counter-Strike. IEM. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=1_800_000_000,
        odds_a=1.65,
        odds_b=2.25,
    )
    await db.set_status(int(row["id"]), "live")
    await db.place_bet(int(row["id"]), 1, None, "a", 250)
    card, _ = await db.upsert_map(
        match_id=int(row["id"]), fonbet_id=9001, map_number=1, odds_a=1.7, odds_b=2.1
    )
    await db.place_map_bet(int(card["id"]), 2, None, "b", 100)
    await db.claim_wheel(1, 200, 24 * 3600)
    await db.claim_tease(1, 50, 24 * 3600)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "src.db", start_balance=1000)
    await database.connect()
    await _seed(database)
    yield database
    await database.close()


async def test_dump_roundtrip_restores_everything(db, tmp_path) -> None:
    blob, payload = await make_dump(db)
    assert payload["counts"]["users"] == 2
    assert payload["counts"]["bets"] == 2

    fresh = Database(tmp_path / "restored.db", start_balance=1000)
    await fresh.connect()
    try:
        counts = await restore(fresh, blob)
        assert counts["users"] == 2 and counts["bets"] == 2

        assert await fresh.get_balance(1) == await db.get_balance(1)
        assert await fresh.get_balance(2) == await db.get_balance(2)
        restored = await fresh.dump_rows()
        for table, rows in payload["tables"].items():
            assert restored[table] == rows, table
    finally:
        await fresh.close()


async def test_restore_replaces_current_data(db, tmp_path) -> None:
    blob, _ = await make_dump(db)
    await db.upsert_user(3, "third", "Третий")
    assert len((await db.dump_rows())["users"]) == 3

    await restore(db, blob)                     # возвращает состояние на момент дампа
    users = (await db.dump_rows())["users"]
    assert len(users) == 2
    assert {row["user_id"] for row in users} == {1, 2}


async def test_broken_dump_is_rejected(db) -> None:
    blob, payload = await make_dump(db)

    with pytest.raises(BackupError, match="не похож"):
        decode("просто текст".encode())

    # испорченный gzip
    with pytest.raises(BackupError):
        decode(blob[:-20])

    # файл другого приложения
    foreign = gzip.compress(json.dumps({"app": "other", "version": 1, "tables": {}}).encode())
    with pytest.raises(BackupError, match="другого приложения"):
        decode(foreign)

    # данные подменили — не сходится контрольное число строк
    payload["tables"]["users"].pop()
    tampered = gzip.compress(json.dumps(payload, ensure_ascii=False).encode())
    with pytest.raises(BackupError, match="повреждена"):
        decode(tampered)


async def test_dump_logged_and_throttled(db) -> None:
    await make_dump(db, kind="auto")
    last = await db.last_backup()
    assert last is not None and last["kind"] == "auto"
    assert int(last["users"]) == 2 and int(last["bets"]) == 2
    assert int(last["size_bytes"]) > 0


async def test_auto_backup_respects_interval(db) -> None:
    from app.services.backup import BackupService

    class NullBot:
        async def send_document(self, **kwargs):
            return None

    service = BackupService(NullBot(), db)      # type: ignore[arg-type]
    assert await service.run_if_due() is True   # копий ещё не было
    assert await service.run_if_due() is False  # только что сняли


def test_encode_is_compact_gzip() -> None:
    blob = encode({"users": [{"user_id": 1}]})
    assert blob[:2] == b"\x1f\x8b"              # gzip-магия
    assert decode(blob)["counts"] == {"users": 1}

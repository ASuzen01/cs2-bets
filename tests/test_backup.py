"""Резервные копии: дамп, проверка целостности и полное восстановление."""

from __future__ import annotations

import gzip
import json
import re

import pytest

from app.db import Database
from app.db.backends import SCHEMA_POSTGRES, SCHEMA_SQLITE
from app.db.database import BACKUP_TABLES, BackupError
from app.services.backup import decode, encode, make_dump, restore, summary


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
    await db.place_bet(int(row["id"]), 1, None, "a", 250, token="backup-bet")
    card, _ = await db.upsert_map(
        match_id=int(row["id"]), fonbet_id=9001, map_number=1, odds_a=1.7, odds_b=2.1
    )
    await db.place_map_bet(int(card["id"]), 2, None, "b", 100)
    await db.claim_wheel(1, 200, 24 * 3600)
    await db.daily_bonus(1, 250, 24 * 3600)
    for _ in range(10):
        await db.tap_tease(1, 50, 24 * 3600, daily_cooldown=24 * 3600)
    await db.add_ticket(1, 100, kind="express")
    await db.set_meta("last_sync_at", "1780000000")

    from app.services import blackjack

    await db.start_blackjack(1, 10, blackjack.new_game(10), token="backup-blackjack")


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "src.db", start_balance=1000)
    await database.connect()
    await _seed(database)
    yield database
    await database.close()


def test_backup_manifest_covers_every_application_table() -> None:
    pattern = r"CREATE TABLE IF NOT EXISTS\s+(\w+)"
    sqlite_tables = set(re.findall(pattern, SCHEMA_SQLITE))
    postgres_tables = set(re.findall(pattern, SCHEMA_POSTGRES))
    assert set(BACKUP_TABLES) == sqlite_tables == postgres_tables


def test_summary_warns_for_legacy_archives_missing_tables() -> None:
    assert "Неполный архив" in summary({"counts": {}, "tables": {"users": []}, "created_at": 0})


async def test_dump_roundtrip_restores_everything(db, tmp_path) -> None:
    blob, payload = await make_dump(db)
    assert payload["version"] == 2
    assert payload["counts"]["users"] == 2
    assert payload["counts"]["bets"] == 2
    assert payload["counts"]["tickets"] == 1
    assert payload["counts"]["app_meta"] == 1
    assert payload["counts"]["bet_requests"] == 2
    assert payload["counts"]["blackjack_games"] == 1

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


async def test_restore_accepts_backup_without_new_game_tables(db) -> None:
    data = await db.dump_rows()
    data.pop("crash_games")
    data.pop("drunkard_games")

    counts = await db.restore_rows(data)

    assert counts["crash_games"] == 0
    assert counts["drunkard_games"] == 0


async def test_restore_replaces_current_data(db, tmp_path) -> None:
    blob, _ = await make_dump(db)
    await db.upsert_user(3, "third", "Третий")
    assert len((await db.dump_rows())["users"]) == 3

    await restore(db, blob)                     # возвращает состояние на момент дампа
    users = (await db.dump_rows())["users"]
    assert len(users) == 2
    assert {row["user_id"] for row in users} == {1, 2}


async def test_incomplete_restore_is_rejected_before_any_table_is_wiped(db) -> None:
    data = await db.dump_rows()
    before_balance = await db.get_balance(1)
    data.pop("tickets")

    with pytest.raises(BackupError, match="Дамп неполный"):
        await db.restore_rows(data)

    assert await db.get_balance(1) == before_balance
    ticket_count = await db._fetchone("SELECT COUNT(*) AS n FROM tickets WHERE user_id = ?", (1,))
    assert int(ticket_count["n"]) == 1


async def test_restore_rejects_unknown_columns_before_wiping(db) -> None:
    data = await db.dump_rows()
    before_balance = await db.get_balance(1)
    data["users"][0]["user_id) VALUES (0)"] = 99

    with pytest.raises(BackupError, match="неизвестные поля"):
        await db.restore_rows(data)

    assert await db.get_balance(1) == before_balance


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


async def test_dump_content_checksum_catches_edits_with_same_row_counts(db) -> None:
    _, payload = await make_dump(db)
    payload["tables"]["users"][0]["full_name"] = "tampered"
    edited = gzip.compress(json.dumps(payload, ensure_ascii=False).encode())

    with pytest.raises(BackupError, match="Контрольная сумма"):
        decode(edited)


async def test_dump_logged_and_throttled(db) -> None:
    await make_dump(db, kind="auto")
    last = await db.last_backup()
    assert last is not None and last["kind"] == "auto"
    assert int(last["users"]) == 2 and int(last["bets"]) == 2
    assert int(last["size_bytes"]) > 0


async def test_auto_backup_respects_interval(db, monkeypatch) -> None:
    from app.config import settings
    from app.services.backup import BackupService

    monkeypatch.setattr(settings, "admin_ids", "123")

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


async def test_hourly_backup_fires_after_interval(db, monkeypatch) -> None:
    """Копии раз в час: через 61 минуту сервис снимает новую."""
    from app.config import settings
    from app.services.backup import BackupService

    monkeypatch.setattr(settings, "admin_ids", "123")

    class NullBot:
        async def send_document(self, **kwargs):
            return None

    service = BackupService(NullBot(), db)      # type: ignore[arg-type]
    await db.log_backup(100, 2, 1, "auto")
    # состариваем отметку на час с небольшим
    await db.conn.execute("UPDATE backup_log SET created_at = created_at - 3660")
    await db.conn.commit()

    assert settings.backup_interval_hours == 1
    assert await service.run_if_due() is True
    assert await service.run_if_due() is False


async def test_backup_target_chat_prefers_dedicated_chat(db) -> None:
    from app.config import settings
    from app.services.backup import target_chat_id

    old_admins, old_chat = settings.admin_ids, settings.backup_chat_id
    try:
        settings.admin_ids = "5004062791,777"
        settings.backup_chat_id = 0
        assert target_chat_id() == 5004062791      # первый указанный в ADMIN_IDS

        settings.backup_chat_id = -100200300
        assert target_chat_id() == -100200300      # отдельный канал важнее
    finally:
        settings.admin_ids, settings.backup_chat_id = old_admins, old_chat


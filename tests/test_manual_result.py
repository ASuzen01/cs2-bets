"""Ручные результаты: сыгранный матч считается по факту, а не возвратом."""

from __future__ import annotations

import json
import time

import pytest

from app.db import Database
from app.services.manual_result import apply_manual_results


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.manual_result.notify_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "manual.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    await database.upsert_user(2, "vitalik", "Виталик")
    yield database
    await database.close()


async def _match(db: Database, fonbet_id: int = 701) -> int:
    row, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Team Liquid",
        team_b="TYLOO",
        start_time=int(time.time()) - 3 * 3600,
        odds_a=1.6,
        odds_b=2.2,
    )
    return int(row["id"])


def _write(tmp_path, items) -> str:
    path = tmp_path / "manual_results.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return str(path)


async def test_cancelled_match_is_recounted_by_real_winner(db: Database, tmp_path) -> None:
    """Отменённый (ошибочно возвращённый) матч пересчитывается по факту победы."""
    match_id = await _match(db)
    await db.place_bet(match_id, 1, None, "a", 100)
    await db.place_bet(match_id, 2, None, "b", 100)
    await db.cancel_match(match_id)
    assert await db.get_balance(1) == 1000  # возврат уже случился

    path = _write(
        tmp_path,
        [{"teams": ["Team Liquid", "TYLOO"], "score": "2:0", "winner": "a"}],
    )
    done = await apply_manual_results(db, None, path=path)

    assert len(done) == 1
    match = await db.get_match(match_id)
    assert str(match["status"]) == "finished"
    assert str(match["winner"]) == "a"
    assert (match["score_a"], match["score_b"]) == (2, 0)
    # возврат снят, выигрыш по кэфу 1.6 выплачен: 1000 - 100 + 160
    assert await db.get_balance(1) == 1060
    # проигравшая ставка просто теряет возврат
    assert await db.get_balance(2) == 900


async def test_live_match_is_settled_not_voided(db: Database, tmp_path) -> None:
    """Матч в awaiting закрывается по результату, а не возвратом."""
    match_id = await _match(db, fonbet_id=702)
    await db.place_bet(match_id, 1, None, "b", 100)
    await db.set_status(match_id, "awaiting")

    path = _write(tmp_path, [{"teams": ["TYLOO", "Team Liquid"], "winner": "b"}])
    await apply_manual_results(db, None, path=path)

    match = await db.get_match(match_id)
    assert str(match["status"]) == "finished"
    assert str(match["winner"]) == "b"
    assert await db.get_balance(1) == 1120  # 1000 - 100 + 220 (кэф 2.2)


async def test_manual_result_is_idempotent(db: Database, tmp_path) -> None:
    """Повторный запуск (после рестарта/деплоя) ничего не выплачивает второй раз."""
    match_id = await _match(db, fonbet_id=703)
    await db.place_bet(match_id, 1, None, "a", 100)
    path = _write(tmp_path, [{"teams": ["Team Liquid", "TYLOO"], "score": "2:0", "winner": "a"}])

    await apply_manual_results(db, None, path=path)
    balance = await db.get_balance(1)
    done = await apply_manual_results(db, None, path=path)

    assert done == []
    assert await db.get_balance(1) == balance == 1060

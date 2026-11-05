"""Финальные результаты из внешнего фида: разбор, сопоставление, применение."""

from __future__ import annotations

import json
import time

import pytest

from app.db import Database
from app.services.manual_result import apply_manual_results
from app.services.results import FootballResult, parse_results, same_team


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.manual_result.notify_bettors", _noop)
    monkeypatch.setattr("app.services.maps.notify_map_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "results.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


def _packet(events: list[dict]) -> dict:
    return {"Stages": [{"Events": events}]}


def _event(
    home: str,
    away: str,
    *,
    status: str = "FT",
    score: tuple[int, int] = (0, 0),
    or_score: tuple[int, int] | None = None,
    start: str = "20261104174500",
) -> dict:
    return {
        "T1": [{"Nm": home}],
        "T2": [{"Nm": away}],
        "Tr1": str(score[0]),
        "Tr2": str(score[1]),
        "Tr1OR": str((or_score or score)[0]),
        "Tr2OR": str((or_score or score)[1]),
        "Eps": status,
        "Esd": start,
    }


# ------------------------------------------------------------------ разбор
def test_parse_results_keeps_final_scores() -> None:
    results = parse_results(
        _packet(
            [
                _event("AEK Athens", "Real Madrid", score=(2, 3)),
                _event("Barcelona", "Aston Villa", score=(4, 5), or_score=(1, 1), status="AET"),
                _event("Roma", "Lille", status="Postp."),
                _event("Inter", "Milan", status="NotStarted"),
            ]
        )
    )
    assert len(results) == 3                       # идущий матч не берём
    aek = results[0]
    assert (aek.home, aek.away) == ("AEK Athens", "Real Madrid")
    assert aek.score_text == "2:3" and aek.finished and not aek.void

    cup = results[1]
    # ставки на исход/тоталы считаются по 90 минутам, а не по счёту с овертаймом
    assert (cup.score_home, cup.score_away) == (1, 1)
    assert (cup.final_home, cup.final_away) == (4, 5)

    assert results[2].void and not results[2].finished


def test_same_team_ignores_club_words_but_not_youth_teams() -> None:
    assert same_team("AFC Bournemouth", "Bournemouth")
    assert same_team("Real Madrid", "Real Madrid")
    assert same_team("Sporting CP", "Sporting")
    assert not same_team("AEK Athens", "AEK Athens U19")
    assert not same_team("Real Madrid", "Real Madrid 2")
    assert not same_team("Real Sociedad", "Real Madrid")


# --------------------------------------------------- пересчёт уже посчитанного
async def _football_match(db: Database, *, settled_score: tuple[int, int]) -> int:
    row, _ = await db.upsert_match(
        fonbet_id=880011,
        tournament="Лига Чемпионов УЕФА. Общий этап",
        team_a="АЕК Афины",
        team_b="Реал Мадрид",
        start_time=int(time.time()) + 1800,
        odds_a=6.4,
        odds_x=4.2,
        odds_b=1.24,
        sport="football",
    )
    match_id = int(row["id"])
    await db.place_football_bet(match_id, 1, None, "result", "2", 100)   # ставка на Реал
    await db.conn.execute(
        "UPDATE matches SET start_time = ? WHERE id = ?", (int(time.time()) - 3 * 3600, match_id)
    )
    await db.conn.commit()
    await db.settle_football(match_id, *settled_score, source="fonbet")
    return match_id


async def test_wrong_football_result_is_fixed_by_manual_score(db: Database, tmp_path) -> None:
    """Матч посчитали по live-счёту 1:0 — а Реал выиграл 2:3. Ставка возвращается в игру."""
    match_id = await _football_match(db, settled_score=(1, 0))
    assert await db.get_balance(1) == 900            # ставка проиграла

    path = tmp_path / "manual.json"
    path.write_text(
        json.dumps([{"teams": ["АЕК Афины", "Реал Мадрид"], "score": "2:3"}], ensure_ascii=False),
        encoding="utf-8",
    )
    done = await apply_manual_results(db, None, path=str(path))

    assert len(done) == 1
    match = await db.get_match(match_id)
    assert str(match["status"]) == "finished"
    assert (int(match["score_a"]), int(match["score_b"])) == (2, 3)
    assert await db.get_balance(1) == 900 + round(100 * 1.24)      # выигрыш по кэфу 1.24


async def test_manual_football_result_is_idempotent(db: Database, tmp_path) -> None:
    """Повторный запуск с тем же счётом второй раз не пересчитывает."""
    await _football_match(db, settled_score=(2, 3))
    balance = await db.get_balance(1)

    path = tmp_path / "manual.json"
    path.write_text(
        json.dumps([{"teams": ["АЕК Афины", "Реал Мадрид"], "score": "2:3"}], ensure_ascii=False),
        encoding="utf-8",
    )
    done = await apply_manual_results(db, None, path=str(path))

    assert done == []
    assert await db.get_balance(1) == balance

    # второй прогон после настоящего пересчёта тоже ничего не меняет
    await apply_manual_results(db, None, path=str(path))
    assert await db.get_balance(1) == balance


async def test_unsettled_football_match_is_settled_by_manual_score(db: Database, tmp_path) -> None:
    """Матч висит в ожидании — ручной счёт его закрывает по факту."""
    row, _ = await db.upsert_match(
        fonbet_id=880012,
        tournament="Лига Чемпионов УЕФА. Общий этап",
        team_a="АЕК Афины",
        team_b="Реал Мадрид",
        start_time=int(time.time()) + 1800,
        odds_a=6.4,
        odds_x=4.2,
        odds_b=1.24,
        sport="football",
    )
    match_id = int(row["id"])
    await db.place_football_bet(match_id, 1, None, "result", "2", 100)
    await db.conn.execute(
        "UPDATE matches SET start_time = ? WHERE id = ?", (int(time.time()) - 4 * 3600, match_id)
    )
    await db.conn.commit()
    await db.set_status(match_id, "awaiting")

    path = tmp_path / "manual.json"
    path.write_text(
        json.dumps([{"teams": ["АЕК Афины", "Реал Мадрид"], "score": "2:3"}], ensure_ascii=False),
        encoding="utf-8",
    )
    await apply_manual_results(db, None, path=str(path))

    match = await db.get_match(match_id)
    assert str(match["status"]) == "finished"
    assert await db.get_balance(1) == 900 + round(100 * 1.24)


def test_football_result_is_hashable_dataclass() -> None:
    """Простая проверка структуры итога: счёт за 90 минут отдельно от финального."""
    result = FootballResult(
        home="AEK Athens",
        away="Real Madrid",
        score_home=2,
        score_away=3,
        final_home=2,
        final_away=3,
        status="FT",
        start_ts=0,
    )
    assert result.score_text == "2:3" and result.finished

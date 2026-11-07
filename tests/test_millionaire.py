"""Economics, persistence and API behavior for «Кто хочет стать миллионером?»"""

from __future__ import annotations

import asyncio

import pytest

from app.db import Database, InsufficientFunds, InvalidBet
from app.services import millionaire


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "millionaire.db", start_balance=1_000)
    await database.connect()
    await database.upsert_user(7101, "quizzer", "Quiz Player")
    yield database
    await database.close()


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import settings
    from app.web.server import build_app

    monkeypatch.setattr(settings, "miniapp_dev_user", 7101)
    database = Database(tmp_path / "millionaire-api.db", start_balance=1_000)
    await database.connect()
    await database.upsert_user(7101, "quizzer", "Quiz Player")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


def _fixed_question_ids() -> list[str]:
    return [f"e{i:02}" for i in range(1, 6)] + [f"m{i:02}" for i in range(1, 6)] + [f"h{i:02}" for i in range(1, 6)]


def test_payout_table_fixed_stakes_and_half_up_rounding() -> None:
    assert millionaire.STAKES == (10, 50, 100, 250, 500, 1_000)
    assert [millionaire.payout(100, level) for level in range(1, 16)] == [
        100, 100, 100, 110, 120, 135, 150, 170, 190, 215, 245, 280, 320, 370, 430,
    ]
    assert millionaire.payout(10, 6) == 14  # 13.5 rounds half-up
    assert millionaire.payout(50, 6) == 68  # 67.5 rounds half-up
    assert millionaire.payout(250, 15) == 1_075
    for stake in (0, 9, 101, 10_001):
        with pytest.raises(ValueError):
            millionaire.payout(stake, 1)


def test_questions_are_diverse_and_have_four_unique_options() -> None:
    assert len(millionaire.QUESTIONS) == 30
    assert len({item.id for item in millionaire.QUESTIONS}) == len(millionaire.QUESTIONS)
    for item in millionaire.QUESTIONS:
        assert len(item.options) == 4
        assert len(set(item.options)) == 4
        assert 0 <= item.answer < 4
    for band in ("easy", "medium", "hard"):
        assert sum(item.band == band for item in millionaire.QUESTIONS) == 10


def test_random_selection_is_fifteen_unique_questions_with_five_per_band() -> None:
    ids = millionaire.pick_question_ids()
    assert len(ids) == len(set(ids)) == 15
    bands = [millionaire.question(qid).band for qid in ids]
    assert bands.count("easy") == bands.count("medium") == bands.count("hard") == 5


def test_checkpoint_payout_uses_prior_correct_answers() -> None:
    assert millionaire.checkpoint_payout(100, 4) == 0
    assert millionaire.checkpoint_payout(100, 5) == 120
    assert millionaire.checkpoint_payout(250, 5) == 300
    assert millionaire.checkpoint_payout(100, 9) == 120
    assert millionaire.checkpoint_payout(100, 10) == 215
    assert millionaire.checkpoint_payout(250, 10) == 538
    assert millionaire.checkpoint_payout(50, 15) == 108


async def test_start_deducts_stake_and_is_idempotent(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)
    first, replay = await asyncio.gather(
        db.start_millionaire(7101, 50, "start-once"),
        db.start_millionaire(7101, 50, "start-once"),
    )
    assert first["id"] == replay["id"]
    assert first["question"]["level"] == 1
    assert "answer" not in first["question"]
    assert first["balance"] == replay["balance"] == 950
    assert await db.get_balance(7101) == 950


async def test_only_one_active_game_and_stakes_are_validated(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)
    game = await db.start_millionaire(7101, 100, "first-start")
    second = await db.start_millionaire(7101, 500, "second-start")
    assert second["id"] == game["id"]
    assert second["stake"] == 100
    assert await db.get_balance(7101) == 900
    with pytest.raises(InvalidBet):
        await db.start_millionaire(7101, 101, "bad-start")


async def test_insufficient_balance_does_not_create_game_or_debit(db: Database) -> None:
    await db.add_coins(7101, -500, "подготовка теста")
    with pytest.raises(InsufficientFunds):
        await db.start_millionaire(7101, 1_000, "too-expensive")
    assert await db.get_balance(7101) == 500
    assert await db.millionaire_state(7101) is None


async def test_correct_answers_and_cashout_are_persistent_and_recorded_once(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)
    game = await db.start_millionaire(7101, 50, "cashout-start")
    first_question = game["question"]
    correct = millionaire.question(_fixed_question_ids()[0]).answer
    result, replay = await asyncio.gather(
        db.answer_millionaire(7101, game["id"], correct, "answer-q1"),
        db.answer_millionaire(7101, game["id"], correct, "answer-q1"),
    )
    assert result["correct_count"] == replay["correct_count"] == 1
    assert result["cashout_payout"] == millionaire.payout(50, 1)
    assert result["question"]["level"] == 2
    assert "answer" not in result["question"]
    assert first_question["level"] == 1

    cashed = await db.cashout_millionaire(7101, game["id"], "cashout-once")
    assert cashed["status"] == "cashed"
    assert cashed["payout"] == 50
    assert cashed["balance"] == 1_000
    replay_cashout = await db.cashout_millionaire(7101, game["id"], "another-cashout")
    assert replay_cashout["balance"] == 1_000
    assert await db.get_balance(7101) == 1_000
    history = await db.casino_history(7101)
    assert len(history) == 1
    assert history[0]["game"] == "millionaire"
    assert history[0]["payout"] == 50


async def test_miss_before_checkpoint_loses_stake_and_ends_game(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)
    game = await db.start_millionaire(7101, 100, "loss-start")
    question = millionaire.question(_fixed_question_ids()[0])
    lost = await db.answer_millionaire(7101, game["id"], (question.answer + 1) % 4, "loss-q1")
    assert lost["status"] == "lost"
    assert lost["correct_count"] == 0
    assert lost["payout"] == 0
    assert lost["last_result"]["correct"] is False
    assert lost["last_result"]["answer_text"] == question.options[question.answer]
    assert lost["balance"] == 900
    assert await db.get_balance(7101) == 900
    history = await db.casino_history(7101)
    assert len(history) == 1 and history[0]["payout"] == 0


async def test_wrong_answer_after_five_correct_gets_checkpoint_gross_payout(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)
    ids = _fixed_question_ids()
    game = await db.start_millionaire(7101, 250, "checkpoint-start")
    for level in range(5):
        item = millionaire.question(ids[level])
        game = await db.answer_millionaire(7101, game["id"], item.answer, f"checkpoint-q{level + 1}")
    assert game["correct_count"] == 5
    assert game["checkpoint_payout"] == 300
    assert game["cashout_payout"] == 300
    wrong = millionaire.question(ids[5])
    game = await db.answer_millionaire(7101, game["id"], (wrong.answer + 1) % 4, "checkpoint-miss")
    assert game["status"] == "lost"
    assert game["payout"] == 300
    assert game["balance"] == 1_000 - 250 + 300
    assert await db.get_balance(7101) == 1_050
    history = await db.casino_history(7101)
    assert len(history) == 1 and history[0]["payout"] == 300


async def test_wrong_answer_after_ten_correct_gets_second_checkpoint(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)
    ids = _fixed_question_ids()
    game = await db.start_millionaire(7101, 250, "checkpoint10-start")
    for level in range(10):
        item = millionaire.question(ids[level])
        game = await db.answer_millionaire(7101, game["id"], item.answer, f"checkpoint10-q{level + 1}")
    assert game["checkpoint_level"] == 10
    wrong = millionaire.question(ids[10])
    game = await db.answer_millionaire(7101, game["id"], (wrong.answer + 1) % 4, "checkpoint10-miss")
    assert game["status"] == "lost"
    assert game["payout"] == 538
    assert game["balance"] == 1_288
    assert await db.get_balance(7101) == 1_288


async def test_full_run_pays_top_prize_and_records_round(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)
    ids = _fixed_question_ids()
    game = await db.start_millionaire(7101, 100, "full-run-start")
    for level, qid in enumerate(ids):
        item = millionaire.question(qid)
        game = await db.answer_millionaire(7101, game["id"], item.answer, f"full-run-q{level + 1}")
    assert game["status"] == "complete"
    assert game["correct_count"] == 15
    assert game["payout"] == 430
    assert game["balance"] == 1_330
    assert await db.get_balance(7101) == 1_330
    history = await db.casino_history(7101)
    assert len(history) == 1 and history[0]["payout"] == 430


async def test_api_start_answer_state_and_validation(api_client, monkeypatch) -> None:
    client, database = api_client
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)

    invalid = await client.post(
        "/api/millionaire/start", json={"amount": 11, "token": "api-invalid-start"}
    )
    assert invalid.status == 400
    assert await database.get_balance(7101) == 1_000

    started_response = await client.post(
        "/api/millionaire/start", json={"amount": 10, "token": "api-start"}
    )
    assert started_response.status == 200
    started = await started_response.json()
    assert started["game"]["question"]["level"] == 1
    assert "answer" not in started["game"]["question"]

    question = millionaire.question(_fixed_question_ids()[0])
    answered_response = await client.post(
        "/api/millionaire/answer",
        json={"game_id": started["game"]["id"], "answer": question.answer, "token": "api-q1"},
    )
    assert answered_response.status == 200
    answered = await answered_response.json()
    assert answered["game"]["correct_count"] == 1
    assert answered["game"]["last_result"]["correct"] is True
    assert answered["game"]["question"]["level"] == 2

    state_response = await client.get("/api/state")
    assert state_response.status == 200
    state = await state_response.json()
    assert state["millionaire"]["id"] == started["game"]["id"]

    bad_response = await client.post(
        "/api/millionaire/answer",
        json={"game_id": started["game"]["id"], "answer": True, "token": "api-bad"},
    )
    assert bad_response.status == 400
    assert await database.get_balance(7101) == 990

    cashout_response = await client.post(
        "/api/millionaire/cashout",
        json={"game_id": started["game"]["id"], "token": "api-cashout"},
    )
    assert cashout_response.status == 200
    cashed = await cashout_response.json()
    assert cashed["game"]["payout"] == 10
    assert cashed["balance"] == 1_000


async def test_parallel_start_attempts_charge_only_one_stake(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(millionaire, "pick_question_ids", _fixed_question_ids)
    games = await asyncio.gather(*(
        db.start_millionaire(7101, 10, f"parallel-start-{n}") for n in range(5)
    ))
    assert len({game["id"] for game in games}) == 1
    assert await db.get_balance(7101) == 990

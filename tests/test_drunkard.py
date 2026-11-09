"""Пьяница: серверная колода, приглашения и атомарные расчёты ставок."""

from __future__ import annotations

import pytest

from app.db import Database
from app.db.database import InsufficientFunds
from app.services import drunkard


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "drunkard.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(101, "alice", "Алиса")
    await database.upsert_user(202, "boris", "Борис")
    yield database
    await database.close()


def test_deck_sizes_use_expected_rank_ranges() -> None:
    expected = {24: (9, 14), 36: (6, 14), 52: (2, 14)}
    for size, (low, high) in expected.items():
        cards = drunkard._deck(size)
        assert len(cards) == size
        assert {card["value"] for card in cards} == set(range(low, high + 1))


def card(value: int, text: str) -> dict[str, object]:
    return {"value": value, "rank": text[:-1], "suit": text[-1], "text": text}


def controlled_state(size: int, player_wins: bool = True) -> dict[str, object]:
    high = card(14 if player_wins else 10, "Т♠" if player_wins else "10♠")
    low = card(10 if player_wins else 14, "10♥" if player_wins else "Т♥")
    return {
        "version": 1, "deck_size": size,
        "hands": {"player": [high], "opponent": [low]},
        "rounds": 0, "wars": 0, "last_battle": None,
        "winner": None, "tie_reason": None,
    }


def test_a_normal_user_action_reveals_one_pair_not_the_whole_game() -> None:
    state = drunkard.new_game_state(24)
    state["hands"]["player"][0] = card(14, "Т♠")
    state["hands"]["opponent"][0] = card(13, "К♠")

    drunkard.advance_round(state)

    assert state["winner"] is None
    assert state["rounds"] == 1
    assert len(state["last_battle"]["pairs"]) == 1
    assert sum(len(hand) for hand in state["hands"].values()) == 24


@pytest.mark.parametrize(
    ("player_value", "opponent_value", "expected"),
    [(2, 14, "player"), (14, 2, "opponent"), (3, 2, "player"), (14, 13, "player")],
)
def test_two_beats_ace_and_other_ranks_keep_normal_order(
    player_value: int, opponent_value: int, expected: str,
) -> None:
    state = {
        "version": 1, "deck_size": 52,
        "hands": {
            "player": [card(player_value, f"{player_value}♠")],
            "opponent": [card(opponent_value, f"{opponent_value}♥")],
        },
        "rounds": 0, "wars": 0, "last_battle": None, "winner": None, "tie_reason": None,
    }

    drunkard.advance_round(state)

    assert state["last_battle"]["winner"] == expected
    assert state["winner"] == expected
    assert len(state["hands"][expected]) == 2


def test_two_beats_ace_as_the_face_up_war_tiebreak() -> None:
    state = {
        "version": 1, "deck_size": 8,
        "hands": {
            "player": [card(13, "К♠"), card(7, "7♠"), card(2, "2♠"), card(6, "6♠")],
            "opponent": [card(13, "К♥"), card(8, "8♥"), card(14, "Т♥"), card(5, "5♥")],
        },
        "rounds": 0, "wars": 0, "last_battle": None, "winner": None, "tie_reason": None,
    }

    drunkard.advance_round(state)

    assert state["last_battle"]["hidden_pairs"] == 1
    assert state["last_battle"]["pairs"] == [
        {"player": "К♠", "opponent": "К♥"},
        {"player": "2♠", "opponent": "Т♥"},
    ]
    assert state["last_battle"]["winner"] == "player"


def test_tie_enters_war_and_repeats_until_a_winner() -> None:
    state = {
        "version": 1, "deck_size": 6,
        "hands": {
            "player": [card(12, "Д♠"), card(8, "8♠"), card(9, "9♠")],
            "opponent": [card(12, "Д♥"), card(7, "7♥"), card(14, "Т♥")],
        },
        "rounds": 0, "wars": 0, "last_battle": None, "winner": None, "tie_reason": None,
    }

    drunkard.advance_round(state)

    assert state["last_battle"]["hidden_pairs"] == 1
    assert len(state["last_battle"]["pairs"]) == 2
    assert state["winner"] == "opponent"
    assert len(state["hands"]["opponent"]) == 6
    assert not state["hands"]["player"]


@pytest.mark.parametrize("size", [24, 36, 52])
def test_full_game_ends_with_one_winner_and_preserves_deck(size: int) -> None:
    result = drunkard.simulate_game(size)
    assert result["deck_size"] == size
    assert result["winner"] in {"player", "opponent", None}
    assert result["rounds"] > 0
    assert result["wars"] >= 0
    if result["winner"] == "player":
        assert result["cards_player"] == size and result["cards_opponent"] == 0
    elif result["winner"] == "opponent":
        assert result["cards_opponent"] == size and result["cards_player"] == 0


async def test_bot_game_is_interactive_and_pays_twice_stake_at_end(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(drunkard, "new_game_state", lambda size: controlled_state(size))
    started = await db.play_drunkard_bot(101, 50, 36, "tap-1")
    again = await db.play_drunkard_bot(101, 50, 36, "tap-1")

    assert started["game"]["status"] == "active"
    assert started["game"]["result"] is None
    assert started["game"]["game_state"]["cards_player"] == 1
    assert "hands" not in started["game"]["game_state"]
    assert again["game"]["id"] == started["game"]["id"]
    assert await db.get_balance(101) == 950
    assert await db.casino_history(101, 10) == []

    finished = await db.advance_drunkard(101, started["game"]["id"], "turn-1")
    repeated = await db.advance_drunkard(101, started["game"]["id"], "turn-1")
    assert finished["game"]["status"] == "finished"
    assert finished["game"]["your_outcome"] == "win"
    assert finished["game"]["your_payout"] == 100
    assert repeated["game"]["id"] == started["game"]["id"]
    assert await db.get_balance(101) == 1050
    rows = await db.casino_history(101, 10)
    assert len(rows) == 1 and str(rows[0]["game"]) == "drunkard"


async def test_private_invite_starts_live_match_and_pays_only_at_end(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(drunkard, "new_game_state", lambda size: controlled_state(size))
    invite = await db.create_drunkard_invite(101, "@boris", 100, 52, "invite-1")
    assert invite["status"] == "invited"
    assert await db.get_balance(101) == 1000
    inbox = await db.drunkard_state(202)
    assert [item["id"] for item in inbox["incoming"]] == [invite["id"]]

    started = await db.accept_drunkard_invite(202, invite["id"])
    again = await db.accept_drunkard_invite(202, invite["id"])

    assert started["game"]["status"] == "active"
    assert started["game"]["result"] is None
    assert again["game"]["id"] == invite["id"]
    assert await db.get_balance(101) == 900
    assert await db.get_balance(202) == 900
    assert await db.casino_history(101, 10) == []
    result = await db.advance_drunkard(202, invite["id"], "match-turn-1")
    assert result["game"]["your_outcome"] == "loss"
    assert await db.get_balance(101) == 1100
    assert await db.get_balance(202) == 900
    assert len(await db.casino_history(101, 10)) == 1
    assert len(await db.casino_history(202, 10)) == 1


async def test_invite_requires_creator_to_have_the_stake(db: Database) -> None:
    await db._write("UPDATE users SET balance = 40 WHERE user_id = 101")

    with pytest.raises(InsufficientFunds, match="Недостаточно монет"):
        await db.create_drunkard_invite(101, "boris", 50, 24)

    assert await db.get_balance(101) == 40
    assert await db.drunkard_state(202) == {"incoming": [], "outgoing": [], "active": None, "last": None}


async def test_declining_invite_never_changes_balance(db: Database) -> None:
    invite = await db.create_drunkard_invite(101, "boris", 250, 24)
    closed = await db.close_drunkard_invite(202, invite["id"])

    assert closed["status"] == "declined"
    assert await db.get_balance(101) == 1000
    assert await db.get_balance(202) == 1000


async def test_insufficient_balance_cannot_partially_charge_invite(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(
        drunkard,
        "simulate_game",
        lambda size: {"deck_size": size, "winner": "player", "rounds": 1, "wars": 0,
                      "cards_player": size, "cards_opponent": 0, "last_player_card": "Т♠",
                      "last_opponent_card": "Д♠", "tie_reason": None},
    )
    await db._write("UPDATE users SET balance = 40 WHERE user_id = 202")
    invite = await db.create_drunkard_invite(101, "boris", 50, 24)

    with pytest.raises(InsufficientFunds, match="не хватает монет"):
        await db.accept_drunkard_invite(202, invite["id"])

    assert await db.get_balance(101) == 1000
    assert await db.get_balance(202) == 40
    pending = await db.drunkard_state(202)
    assert pending["incoming"][0]["status"] == "invited"

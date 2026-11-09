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


async def test_bot_game_debits_and_pays_twice_stake_idempotently(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(
        drunkard,
        "simulate_game",
        lambda size: {
            "deck_size": size, "winner": "player", "rounds": 12, "wars": 2,
            "cards_player": size, "cards_opponent": 0,
            "last_player_card": "Т♠", "last_opponent_card": "Д♣", "tie_reason": None,
        },
    )
    first = await db.play_drunkard_bot(101, 50, 36, "tap-1")
    again = await db.play_drunkard_bot(101, 50, 36, "tap-1")

    assert first["game"]["id"] == again["game"]["id"]
    assert first["game"]["your_outcome"] == "win"
    assert first["game"]["your_payout"] == 100
    assert await db.get_balance(101) == 1050
    rows = await db.casino_history(101, 10)
    assert len(rows) == 1 and str(rows[0]["game"]) == "drunkard"


async def test_private_invite_is_free_until_accept_and_pays_winner(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(
        drunkard,
        "simulate_game",
        lambda size: {
            "deck_size": size, "winner": "player", "rounds": 30, "wars": 4,
            "cards_player": size, "cards_opponent": 0,
            "last_player_card": "Т♠", "last_opponent_card": "К♣", "tie_reason": None,
        },
    )
    invite = await db.create_drunkard_invite(101, "@boris", 100, 52, "invite-1")
    assert invite["status"] == "invited"
    assert await db.get_balance(101) == 1000
    inbox = await db.drunkard_state(202)
    assert [item["id"] for item in inbox["incoming"]] == [invite["id"]]

    result = await db.accept_drunkard_invite(202, invite["id"])
    again = await db.accept_drunkard_invite(202, invite["id"])

    assert result["game"]["your_outcome"] == "loss"
    assert again["game"]["id"] == invite["id"]
    assert await db.get_balance(101) == 1100
    assert await db.get_balance(202) == 900
    assert len(await db.casino_history(101, 10)) == 1
    assert len(await db.casino_history(202, 10)) == 1


async def test_invite_requires_creator_to_have_the_stake(db: Database) -> None:
    await db._write("UPDATE users SET balance = 40 WHERE user_id = 101")

    with pytest.raises(InsufficientFunds, match="Недостаточно монет"):
        await db.create_drunkard_invite(101, "boris", 50, 24)

    assert await db.get_balance(101) == 40
    assert await db.drunkard_state(202) == {"incoming": [], "outgoing": [], "last": None}


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

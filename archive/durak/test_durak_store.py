from __future__ import annotations

import pytest

from app.db import Database
from app.db.database import InvalidBet
from app.services import durak_store


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "durak-store.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(101, "alice", "Alice")
    await database.upsert_user(202, "bob", "Bob")
    await database.upsert_user(303, "carol", "Carol")
    yield database
    await database.close()


async def test_private_online_room_holds_stakes_only_when_started(db: Database) -> None:
    room = await durak_store.create(
        db, user_id=101, name="Alice", table_type="online", visibility="private",
        mode="transfer", deck_size=36, stake=100, player_count=2,
    )
    assert room["status"] == "waiting" and room["players_count"] == 1
    assert await db.get_balance(101) == 1000

    joined = await durak_store.join(db, user_id=202, name="Bob", code=room["code"])
    assert joined["status"] == "waiting" and joined["players_count"] == 2
    started = await durak_store.start(db, user_id=101, game_id=room["id"])
    assert started["status"] == "active" and started["players_count"] == 2
    assert await db.get_balance(101) == 900
    assert await db.get_balance(202) == 900

    state = await durak_store.state(db, 202)
    assert state["game"]["id"] == room["id"]
    assert state["game"]["is_owner"] is False
    assert len(state["game"]["hand"]) > 0
    assert all("hand" not in seat for seat in state["game"]["players"])


async def test_public_matchmaking_joins_matching_room_and_autostarts(db: Database) -> None:
    first = await durak_store.create(
        db, user_id=101, name="Alice", table_type="online", visibility="public",
        mode="neighbors", deck_size=24, stake=10, player_count=2,
    )
    second = await durak_store.create(
        db, user_id=202, name="Bob", table_type="online", visibility="public",
        mode="neighbors", deck_size=24, stake=10, player_count=2,
    )
    assert first["id"] == second["id"]
    assert second["status"] == "active"
    assert await db.get_balance(101) == 990
    assert await db.get_balance(202) == 990
    assert (await durak_store.state(db, 303))["lobbies"] == []


async def test_bots_game_pays_by_final_place_and_records_history(db: Database) -> None:
    game = await durak_store.create(
        db, user_id=101, name="Alice", table_type="bots", visibility="private",
        mode="throwin", deck_size=24, stake=25, player_count=2,
    )
    assert game["status"] == "active" and game["players_count"] == 2
    assert await db.get_balance(101) == 975
    token_no = 0
    for _ in range(10_000):
        if game["status"] == "finished":
            break
        assert game["is_your_turn"]
        actions = game["actions"]
        if game["phase"] == "lead":
            action, index = "attack", actions["attack_indices"][0]
        elif game["phase"] == "throw":
            if actions["attack_indices"]:
                action, index = "throw", actions["attack_indices"][0]
            else:
                action, index = "pass", None
        elif game["phase"] == "defend":
            if actions["defend"]:
                action, index = "defend", actions["defend"][0]["hand_index"]
            else:
                action, index = "take", None
        else:
            pytest.fail(f"Unexpected phase: {game['phase']}")
        token_no += 1
        result = await durak_store.action(
            db, user_id=101, game_id=game["id"], action_name=action,
            card_index=index, token=f"step-{token_no}",
        )
        game = result["game"]
    else:
        pytest.fail("Bot game did not finish within 10,000 human turns")

    assert game["your_place"] in (1, 2)
    assert game["payout"] in (0, 50)
    assert await db.get_balance(101) == 975 + game["payout"]
    history = await db._fetchall("SELECT * FROM casino_bets WHERE user_id = ? AND game = 'durak'", (101,))
    assert len(history) == 1 and int(history[0]["payout"]) == game["payout"]


async def test_abandoned_active_game_refunds_stakes_after_a_day(db: Database) -> None:
    game = await durak_store.create(
        db, user_id=101, name="Alice", table_type="bots", visibility="private",
        mode="throwin", deck_size=24, stake=100, player_count=2,
    )
    assert await db.get_balance(101) == 900
    await db._write("UPDATE durak_games SET updated_at = ? WHERE id = ?", (1, game["id"]))

    current = await durak_store.state(db, 101)

    assert current["game"] is None
    assert await db.get_balance(101) == 1000
    row = await db._fetchone("SELECT status FROM durak_games WHERE id = ?", (game["id"],))
    assert row["status"] == "cancelled"


async def test_player_cannot_join_active_table_by_private_code(db: Database) -> None:
    room = await durak_store.create(
        db, user_id=101, name="Alice", table_type="online", visibility="private",
        mode="throwin", deck_size=24, stake=10, player_count=2,
    )
    await durak_store.join(db, user_id=202, name="Bob", code=room["code"])
    await durak_store.start(db, user_id=101, game_id=room["id"])
    with pytest.raises(InvalidBet):
        await durak_store.join(db, user_id=303, name="Carol", code=room["code"])

from __future__ import annotations

import time

import pytest

from app.db import Database

USER_ID = 9123456


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "admin-diagnostics.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(USER_ID, "debug_user", "Debug User")
    yield database
    await database.close()


async def make_match(db: Database, fonbet_id: int, offset: int):
    match, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament="Debug Cup",
        team_a=f"Team {offset}A",
        team_b=f"Team {offset}B",
        start_time=int(time.time()) + 3600,
        odds_a=1.8,
        odds_b=2.0,
    )
    return match


async def test_trace_single_includes_ticket_and_match(db: Database) -> None:
    match = await make_match(db, 99001, 1)
    ticket_id = await db.add_ticket(USER_ID, 321, "админ", kind="single", ttl_seconds=86400)
    await db.place_bet(int(match["id"]), USER_ID, None, "a", 321, ticket_id=ticket_id)

    traces = await db.trace_bet(1)

    assert len(traces) == 1
    assert traces[0]["kind"] == "single"
    row = traces[0]["row"]
    assert int(row["user_id"]) == USER_ID
    assert row["team_a"] == "Team 1A"
    assert int(row["ticket_id"]) == ticket_id
    assert int(row["ticket_amount"]) == 321
    assert row["ticket_used_at"] is not None


async def test_trace_express_includes_legs(db: Database) -> None:
    first = await make_match(db, 99002, 2)
    second = await make_match(db, 99003, 3)
    express_id, _, _ = await db.place_express(
        USER_ID,
        None,
        100,
        [
            {"match_id": int(first["id"]), "side": "a"},
            {"match_id": int(second["id"]), "side": "b"},
        ],
    )

    traces = await db.trace_bet(express_id)
    express = next(item for item in traces if item["kind"] == "express")

    assert int(express["row"]["user_id"]) == USER_ID
    assert len(express["legs"]) == 2
    assert {leg["team_a"] for leg in express["legs"]} == {"Team 2A", "Team 3A"}


async def test_user_recent_events_is_newest_first_and_limited(db: Database) -> None:
    await db.log_event(USER_ID, "one", "first")
    await db.log_event(USER_ID, "two", "second")
    await db.log_event(USER_ID, "three", "third")

    events = await db.user_recent_events(USER_ID, limit=2)

    assert [event["name"] for event in events] == ["three", "two"]


def test_admin_menu_lists_registered_diagnostics_and_no_old_winback_command() -> None:
    from bot import ADMIN_COMMANDS

    names = {name for name, _ in ADMIN_COMMANDS}
    assert {
        "results", "maps", "hockey", "cleanup", "recount", "recheck", "players",
        "stats", "grant", "freebet", "best", "backup", "version", "services",
        "player", "bettrace", "feed",
    } <= names
    assert "winback" not in names

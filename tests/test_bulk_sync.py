from __future__ import annotations

import pytest

from app.db import Database
from app.services.bulk_sync import (
    MAX_BATCH_ROWS,
    bulk_upsert_maps,
    bulk_upsert_matches,
    load_matches_by_fonbet_ids,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bulk-sync.db")
    await database.connect()
    yield database
    await database.close()


def match_payload(event_id: int, **overrides):
    payload = {
        "fonbet_id": event_id,
        "tournament": "League",
        "team_a": f"Team {event_id} A",
        "team_b": f"Team {event_id} B",
        "start_time": 1_900_000_000,
        "sport": "football",
        "odds_a": 1.7,
        "odds_x": 3.3,
        "odds_b": 2.2,
        "score_a": None,
        "score_b": None,
    }
    payload.update(overrides)
    return payload


async def test_bulk_match_upsert_chunks_inserts_and_updates(db: Database) -> None:
    items = [match_payload(50_000 + i) for i in range(MAX_BATCH_ROWS + 7)]
    rows, created = await bulk_upsert_matches(db, items)
    assert created == {item["fonbet_id"] for item in items}
    assert len(rows) == len(items)

    changed = [match_payload(50_000 + i, odds_a=1.91) for i in range(len(items))]
    existing = await load_matches_by_fonbet_ids(db, [item["fonbet_id"] for item in changed])
    rows, created_again = await bulk_upsert_matches(db, changed, existing=existing)

    assert created_again == set()
    assert len(rows) == len(items)
    assert all(float(rows[item["fonbet_id"]]["odds_a"]) == 1.91 for item in changed)
    assert all(rows[item["fonbet_id"]]["id"] == existing[item["fonbet_id"]]["id"] for item in changed)


async def test_bulk_upsert_preserves_awaiting_and_finished_semantics(db: Database) -> None:
    row, _ = await db.upsert_match(**match_payload(60_001))
    await db.set_status(int(row["id"]), "awaiting")
    existing = await load_matches_by_fonbet_ids(db, [60_001])

    updated, created = await bulk_upsert_matches(
        db,
        [match_payload(60_001, odds_a=9.9, odds_x=9.8, odds_b=9.7, score_a=2, score_b=1)],
        existing=existing,
    )
    awaiting = updated[60_001]
    assert created == set()
    assert float(awaiting["odds_a"]) == 1.7
    assert (awaiting["score_a"], awaiting["score_b"]) == (2, 1)

    await db.set_status(int(row["id"]), "finished")
    existing = await load_matches_by_fonbet_ids(db, [60_001])
    unchanged, _ = await bulk_upsert_matches(
        db,
        [match_payload(60_001, odds_a=4.4, score_a=3, score_b=2)],
        existing=existing,
    )
    finished = unchanged[60_001]
    assert float(finished["odds_a"]) == 1.7
    assert (finished["score_a"], finished["score_b"]) == (2, 1)


async def test_bulk_map_upsert_skips_closed_rows_and_preserves_existing_values(db: Database) -> None:
    match, _ = await db.upsert_match(**match_payload(70_001, sport="cs2"))
    map_row, _ = await db.upsert_map(
        match_id=int(match["id"]),
        fonbet_id=70_101,
        map_number=1,
        odds_a=1.8,
        odds_b=2.0,
        score_a=4,
        score_b=None,
    )

    await bulk_upsert_maps(
        db,
        [
            {
                "match_id": int(match["id"]),
                "fonbet_id": 70_101,
                "map_number": 1,
                "odds_a": 1.9,
                "odds_b": None,
                "score_a": None,
                "score_b": 5,
            }
        ],
    )
    updated = await db.get_map(int(map_row["id"]))
    assert float(updated["odds_a"]) == 1.9
    assert float(updated["odds_b"]) == 2.0
    assert (updated["score_a"], updated["score_b"]) == (4, 5)

    await db.set_map_status(int(map_row["id"]), "finished")
    await bulk_upsert_maps(
        db,
        [
            {
                "match_id": int(match["id"]),
                "fonbet_id": 70_101,
                "map_number": 1,
                "odds_a": 9.0,
                "odds_b": 9.0,
                "score_a": 9,
                "score_b": 9,
            }
        ],
    )
    finished = await db.get_map(int(map_row["id"]))
    assert float(finished["odds_a"]) == 1.9
    assert (finished["score_a"], finished["score_b"]) == (4, 5)

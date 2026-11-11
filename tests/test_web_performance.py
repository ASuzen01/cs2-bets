from __future__ import annotations

import pytest

from app.db import Database
from app.web import server
from app.web.limits import Metrics


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "web-performance.db")
    await database.connect()
    yield database
    await database.close()


async def test_miniapp_matches_combines_sports_and_live_groups(db: Database) -> None:
    rows = {}
    for index, sport in enumerate(("cs2", "football", "hockey"), start=1):
        row, _ = await db.upsert_match(
            fonbet_id=81_000 + index,
            tournament=f"League {sport}",
            team_a=f"A {sport}",
            team_b=f"B {sport}",
            start_time=1_900_000_000 + index,
            odds_a=1.8,
            odds_b=2.0,
            sport=sport,
        )
        rows[sport] = row
    await db.set_status(int(rows["hockey"]["id"]), "live")

    groups = await db.miniapp_matches()

    # Exercise the single-query path on SQLite too, to syntax-check the SQL
    # used by PostgreSQL without requiring a separate PostgreSQL service.
    db.is_postgres = lambda: True
    combined_groups = await db.miniapp_matches()
    assert {
        key: [int(row["id"]) for row in values]
        for key, values in combined_groups.items()
    } == {
        key: [int(row["id"]) for row in values]
        for key, values in groups.items()
    }

    assert [row["sport"] for row in groups["matches"]] == ["cs2"]
    assert [row["sport"] for row in groups["football"]] == ["football"]
    assert [row["sport"] for row in groups["hockey"]] == []
    assert [row["sport"] for row in groups["live"]] == ["hockey"]


async def test_miniapp_line_payload_batches_map_query_and_stays_fresh(db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=80_001,
        tournament="IEM",
        team_a="A",
        team_b="B",
        start_time=1_900_000_000,
        odds_a=1.8,
        odds_b=2.1,
    )
    await db.upsert_map(
        match_id=int(match["id"]),
        fonbet_id=80_101,
        map_number=1,
        odds_a=1.9,
        odds_b=2.0,
    )

    calls = {"lines": 0, "maps": 0}
    original_lines = db.miniapp_matches
    original_maps = db.maps_for_matches

    async def miniapp_matches():
        calls["lines"] += 1
        return await original_lines()

    async def maps_for_matches(match_ids):
        calls["maps"] += 1
        return await original_maps(match_ids)

    db.miniapp_matches = miniapp_matches
    db.maps_for_matches = maps_for_matches
    first = await server._miniapp_lines(db)

    assert calls == {"lines": 1, "maps": 1}
    assert first["matches"][0]["id"] == int(match["id"])
    assert first["matches"][0]["maps"][0]["number"] == 1


def test_metrics_exports_route_duration_count_sum_and_max() -> None:
    metrics = Metrics()
    metrics.observe_request_duration("/api/state", 0.125)
    metrics.observe_request_duration("/api/state", 0.375)

    text = metrics.text()
    assert 'app_request_duration_seconds_count{path="/api/state"} 2' in text
    assert 'app_request_duration_seconds_sum{path="/api/state"} 0.500000' in text
    assert 'app_request_duration_seconds_max{path="/api/state"} 0.375000' in text
    assert 'app_request_duration_seconds_window_samples{path="/api/state"} 2' in text
    assert 'app_request_duration_seconds_p50{path="/api/state"} 0.250000' in text
    assert 'app_request_duration_seconds_p95{path="/api/state"} 0.362500' in text

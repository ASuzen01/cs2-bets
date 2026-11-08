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

    calls = {"open": 0, "live": 0, "maps": 0}
    original_open = db.open_matches
    original_live = db.live_matches
    original_maps = db.maps_for_matches

    async def open_matches(*, limit: int, sport: str):
        calls["open"] += 1
        return await original_open(limit=limit, sport=sport)

    async def live_matches(*, limit: int):
        calls["live"] += 1
        return await original_live(limit=limit)

    async def maps_for_matches(match_ids):
        calls["maps"] += 1
        return await original_maps(match_ids)

    db.open_matches = open_matches
    db.live_matches = live_matches
    db.maps_for_matches = maps_for_matches
    first = await server._miniapp_lines(db)

    assert calls == {"open": 3, "live": 1, "maps": 1}
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

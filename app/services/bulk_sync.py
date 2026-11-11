"""Пакетная запись спортивной линии с минимальным числом DB round-trips.

Синхронизация выполняется на одном соединении с БД. Старый путь делал для
каждого события SELECT + UPDATE + SELECT и отдельный COMMIT. Здесь текущие
матчи записываются multi-row upsert-ом небольшими пакетами, а неизменившиеся
строки не трогаются.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

# SQLite historically has a 999-bind-variable limit. At 13 parameters per match,
# 40 rows stay comfortably below it and still cut hundreds of statements to a few.
MAX_BATCH_ROWS = 40
MAX_POSTGRES_BATCH_ROWS = 500


def _batch_rows(db: Any) -> int:
    """Use SQLite's conservative bind limit, but amortize Postgres round-trips."""
    checker = getattr(db, "is_postgres", None)
    return MAX_POSTGRES_BATCH_ROWS if callable(checker) and checker() else MAX_BATCH_ROWS


MATCH_COLUMNS = (
    "fonbet_id",
    "tournament",
    "team_a",
    "team_b",
    "start_time",
    "sport",
    "odds_a",
    "odds_x",
    "odds_b",
    "score_a",
    "score_b",
    "status",
    "created_at",
)
MAP_COLUMNS = (
    "match_id",
    "fonbet_id",
    "map_number",
    "odds_a",
    "odds_b",
    "score_a",
    "score_b",
    "status",
    "created_at",
)


def _chunks(items: Sequence[Any], size: int = MAX_BATCH_ROWS):
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _unique_by_id(items: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    # Feed packets normally contain unique ids; de-duplicate defensively so a
    # multi-row PostgreSQL upsert never tries to update the same key twice.
    unique: dict[int, dict[str, Any]] = {}
    for item in items:
        unique[int(item[key])] = dict(item)
    return list(unique.values())


async def load_matches_by_fonbet_ids(db: Any, fonbet_ids: Sequence[int]) -> dict[int, Any]:
    """Load the existing rows needed for old-line filtering in a few queries."""
    ids = list(dict.fromkeys(int(value) for value in fonbet_ids))
    found: dict[int, Any] = {}
    for batch in _chunks(ids, _batch_rows(db)):
        placeholders = ",".join("?" for _ in batch)
        async with db.conn.execute(
            f"SELECT * FROM matches WHERE fonbet_id IN ({placeholders})", tuple(batch)
        ) as cursor:
            for row in await cursor.fetchall():
                found[int(row["fonbet_id"])] = row
    return found


async def bulk_upsert_matches(
    db: Any,
    items: Sequence[Mapping[str, Any]],
    *,
    existing: Mapping[int, Any] | None = None,
) -> tuple[dict[int, Any], set[int]]:
    """Insert/update line matches; return rows by provider id and newly created ids.

    Existing-row semantics match ``Database.upsert_match``: open/live rows refresh
    odds, start time and non-null scores; awaiting rows refresh only scores; settled
    and cancelled rows stay unchanged.
    """
    prepared = _unique_by_id(items, "fonbet_id")
    if not prepared:
        return {}, set()

    by_id = dict(existing) if existing is not None else await load_matches_by_fonbet_ids(
        db, [int(item["fonbet_id"]) for item in prepared]
    )
    rows_by_id: dict[int, Any] = dict(by_id)
    created: set[int] = set()
    columns = ",".join(MATCH_COLUMNS)
    updates = """
        odds_a = CASE WHEN matches.status IN ('open', 'live')
                      THEN COALESCE(excluded.odds_a, matches.odds_a) ELSE matches.odds_a END,
        odds_x = CASE WHEN matches.status IN ('open', 'live')
                      THEN COALESCE(excluded.odds_x, matches.odds_x) ELSE matches.odds_x END,
        odds_b = CASE WHEN matches.status IN ('open', 'live')
                      THEN COALESCE(excluded.odds_b, matches.odds_b) ELSE matches.odds_b END,
        start_time = CASE WHEN matches.status IN ('open', 'live')
                          THEN excluded.start_time ELSE matches.start_time END,
        score_a = CASE WHEN matches.status IN ('open', 'live', 'awaiting')
                       THEN COALESCE(excluded.score_a, matches.score_a) ELSE matches.score_a END,
        score_b = CASE WHEN matches.status IN ('open', 'live', 'awaiting')
                       THEN COALESCE(excluded.score_b, matches.score_b) ELSE matches.score_b END
    """
    changed = """
        (
            matches.status IN ('open', 'live') AND (
                matches.odds_a IS DISTINCT FROM COALESCE(excluded.odds_a, matches.odds_a) OR
                matches.odds_x IS DISTINCT FROM COALESCE(excluded.odds_x, matches.odds_x) OR
                matches.odds_b IS DISTINCT FROM COALESCE(excluded.odds_b, matches.odds_b) OR
                matches.start_time IS DISTINCT FROM excluded.start_time OR
                matches.score_a IS DISTINCT FROM COALESCE(excluded.score_a, matches.score_a) OR
                matches.score_b IS DISTINCT FROM COALESCE(excluded.score_b, matches.score_b)
            )
        ) OR (
            matches.status = 'awaiting' AND (
                matches.score_a IS DISTINCT FROM COALESCE(excluded.score_a, matches.score_a) OR
                matches.score_b IS DISTINCT FROM COALESCE(excluded.score_b, matches.score_b)
            )
        )
    """

    for batch in _chunks(prepared, _batch_rows(db)):
        values = [
            (
                int(item["fonbet_id"]),
                str(item["tournament"]),
                str(item["team_a"]),
                str(item["team_b"]),
                int(item["start_time"]),
                str(item.get("sport") or "cs2"),
                item.get("odds_a"),
                item.get("odds_x"),
                item.get("odds_b"),
                item.get("score_a"),
                item.get("score_b"),
                "open",
                int(item.get("created_at") or time.time()),
            )
            for item in batch
        ]
        value_sql = ",".join("(" + ",".join("?" for _ in MATCH_COLUMNS) + ")" for _ in batch)
        sql = (
            f"INSERT INTO matches ({columns}) VALUES {value_sql} "
            f"ON CONFLICT(fonbet_id) DO UPDATE SET {updates} "
            f"WHERE {changed} RETURNING *"
        )
        params = tuple(value for row in values for value in row)
        async with db.transaction(), db.conn.execute(sql, params) as cursor:
            returned = await cursor.fetchall()

        for row in returned:
            event_id = int(row["fonbet_id"])
            rows_by_id[event_id] = row
            if event_id not in by_id:
                created.add(event_id)

    # Defensive race fallback: a second writer may have inserted a key after the
    # prefetch, causing the upsert to be a no-op. Resolve only those rare ids.
    missing = [int(item["fonbet_id"]) for item in prepared if int(item["fonbet_id"]) not in rows_by_id]
    if missing:
        rows_by_id.update(await load_matches_by_fonbet_ids(db, missing))
    return rows_by_id, created


async def bulk_upsert_maps(db: Any, items: Sequence[Mapping[str, Any]]) -> None:
    """Batch-upsert map odds/scores and skip no-op writes."""
    prepared = _unique_by_id(items, "fonbet_id")
    if not prepared:
        return

    columns = ",".join(MAP_COLUMNS)
    updates = """
        odds_a = CASE WHEN match_maps.status IN ('open', 'live')
                      THEN COALESCE(excluded.odds_a, match_maps.odds_a) ELSE match_maps.odds_a END,
        odds_b = CASE WHEN match_maps.status IN ('open', 'live')
                      THEN COALESCE(excluded.odds_b, match_maps.odds_b) ELSE match_maps.odds_b END,
        score_a = CASE WHEN match_maps.status IN ('open', 'live', 'awaiting')
                       THEN COALESCE(excluded.score_a, match_maps.score_a) ELSE match_maps.score_a END,
        score_b = CASE WHEN match_maps.status IN ('open', 'live', 'awaiting')
                       THEN COALESCE(excluded.score_b, match_maps.score_b) ELSE match_maps.score_b END
    """
    changed = """
        (
            match_maps.status IN ('open', 'live') AND (
                match_maps.odds_a IS DISTINCT FROM COALESCE(excluded.odds_a, match_maps.odds_a) OR
                match_maps.odds_b IS DISTINCT FROM COALESCE(excluded.odds_b, match_maps.odds_b) OR
                match_maps.score_a IS DISTINCT FROM COALESCE(excluded.score_a, match_maps.score_a) OR
                match_maps.score_b IS DISTINCT FROM COALESCE(excluded.score_b, match_maps.score_b)
            )
        ) OR (
            match_maps.status = 'awaiting' AND (
                match_maps.score_a IS DISTINCT FROM COALESCE(excluded.score_a, match_maps.score_a) OR
                match_maps.score_b IS DISTINCT FROM COALESCE(excluded.score_b, match_maps.score_b)
            )
        )
    """

    for batch in _chunks(prepared, _batch_rows(db)):
        values = [
            (
                int(item["match_id"]),
                int(item["fonbet_id"]),
                int(item["map_number"]),
                item.get("odds_a"),
                item.get("odds_b"),
                item.get("score_a"),
                item.get("score_b"),
                "open",
                int(item.get("created_at") or time.time()),
            )
            for item in batch
        ]
        value_sql = ",".join("(" + ",".join("?" for _ in MAP_COLUMNS) + ")" for _ in batch)
        sql = (
            f"INSERT INTO match_maps ({columns}) VALUES {value_sql} "
            f"ON CONFLICT(fonbet_id) DO UPDATE SET {updates} "
            f"WHERE {changed}"
        )
        params = tuple(value for row in values for value in row)
        async with db.transaction(), db.conn.execute(sql, params):
            pass


__all__ = [
    "MAX_BATCH_ROWS",
    "bulk_upsert_maps",
    "bulk_upsert_matches",
    "load_matches_by_fonbet_ids",
]

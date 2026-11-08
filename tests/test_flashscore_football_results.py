"""Flashscore football fallback: parsing, regulation-time safety, matching order."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.flashscore_football_results import (
    FlashscoreFootballResultsClient,
    parse_feed,
)
from app.services.results import FootballResult, ResultsClient


def _event(
    event_id: str,
    home: str,
    away: str,
    score: tuple[int, int],
    *,
    status: str = "3",
    period: str = "3",
    start_ts: int = 1794067200,
) -> str:
    return (
        f"AA÷{event_id}¬AB÷{status}¬AC÷{period}¬AD÷{start_ts}¬"
        f"AE÷{home}¬AF÷{away}¬AG÷{score[0]}¬AH÷{score[1]}"
    )


def test_flashscore_parser_only_uses_completed_regulation_scores() -> None:
    raw = "¬~".join(
        [
            _event("ft", "Paris Saint-Germain", "Troyes", (1, 0)),
            _event("aet", "Cup Home", "Cup Away", (3, 2), period="10"),
            _event("live", "Live Home", "Live Away", (2, 1), status="2"),
            _event("bad", "No Score", "Missing", (0, 0)).replace("¬AG÷0¬AH÷0", ""),
        ]
    )

    results = parse_feed(raw)

    assert len(results) == 1
    result = results[0]
    assert (result.home, result.away) == ("Paris Saint-Germain", "Troyes")
    assert (result.score_home, result.score_away) == (1, 0)
    assert (result.final_home, result.final_away) == (1, 0)
    assert result.finished and result.source == "flashscore"


@pytest.mark.asyncio
async def test_flashscore_find_accepts_observed_name_aliases_and_orients_scores(monkeypatch) -> None:
    start_ts = int(datetime(2026, 11, 7, 16, tzinfo=timezone.utc).timestamp())
    provider_result = FootballResult(
        home="Troyes",
        away="Paris Saint-Germain",
        score_home=0,
        score_away=1,
        final_home=0,
        final_away=1,
        status="FT",
        start_ts=start_ts,
        source="flashscore",
    )
    client = FlashscoreFootballResultsClient()

    async def fake_day(offset: int) -> list[FootballResult]:
        return [provider_result]

    monkeypatch.setattr(client, "day", fake_day)
    found = await client.find("PSG", "Troyes", start_ts)

    assert found is not None
    assert (found.score_home, found.score_away) == (1, 0)
    assert found.source == "flashscore"


@pytest.mark.asyncio
async def test_livescore_find_also_orients_scores_to_requested_home_away(monkeypatch) -> None:
    start_ts = int(datetime(2026, 11, 7, 16, tzinfo=timezone.utc).timestamp())
    provider_result = FootballResult(
        home="Paris Saint-Germain",
        away="Troyes",
        score_home=1,
        score_away=0,
        final_home=1,
        final_away=0,
        status="FT",
        start_ts=start_ts,
    )
    client = ResultsClient()

    async def fake_day(day):
        return [provider_result]

    monkeypatch.setattr(client, "day", fake_day)
    found = await client.find("Troyes", "PSG", start_ts)

    assert found is not None
    assert (found.score_home, found.score_away) == (0, 1)

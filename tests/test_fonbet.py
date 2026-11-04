"""Парсер линии Fonbet и тир-1 фильтр (на синтетическом пакете той же структуры)."""

from __future__ import annotations

import pytest

from app.services.fonbet import FonbetClient, FonbetMatch, is_tier1
from app.services.sync import MatchSyncer

INCLUDE = ["IEM", "BLAST Rivals", "ESL Pro League", "PGL"]
EXCLUDE = ["Qualifier", "Academy", "H2H", "2x2"]


@pytest.mark.parametrize(
    "tournament,expected",
    [
        ("Counter-Strike. IEM Пекин. Групповой этап. Bo3", True),
        ("Counter-Strike. BLAST Rivals. Групповой этап. Bo3", True),
        ("Counter-Strike. ESL Pro League. Season 24. Bo3", True),
        ("Counter-Strike. H2H CS. 2X2. Москва. Bo1", False),
        ("Counter-Strike. European Pro League. Bo3", False),
        ("Counter-Strike. NODWIN Clutch. Bo3", False),
        ("Counter-Strike. IEM Пекин. Open Qualifier. Bo3", False),
        ("Counter-Strike. IEM. Academy. Bo3", False),
        ("Counter-Strike. Gamers Club Liga. Serie A. Bo3", False),
    ],
)
def test_tier1_filter(tournament: str, expected: bool) -> None:
    assert is_tier1(tournament, INCLUDE, EXCLUDE) is expected


def _packet() -> dict:
    return {
        "sports": [
            {"id": 29086, "name": "Киберспорт", "kind": "sport"},
            {"id": 147740, "name": "Counter-Strike. IEM Пекин. Групповой этап. Bo3", "kind": "segment"},
            {"id": 118263, "name": "Counter-Strike. H2H CS. 2X2. Москва. Bo1", "kind": "segment"},
            {"id": 1, "name": "Лига Чемпионов УЕФА", "kind": "segment"},
        ],
        "events": [
            {
                "id": 69563071, "level": 1, "sportId": 147740,
                "team1": "Aurora", "team2": "G2 Esports", "startTime": 1793671200, "place": "line",
            },
            {
                "id": 69563072, "level": 2, "sportId": 147740, "parentId": 69563071,
                "name": "1-я карта", "startTime": 1793671200, "place": "line",
            },
            {
                "id": 69529864, "level": 1, "sportId": 118263,
                "team1": "NEO-NOIR BROS", "team2": "THE EMPRESS KNIGHTS",
                "startTime": 1793670000, "place": "line",
            },
        ],
        "customFactors": [
            {"e": 69563071, "factors": [{"f": 921, "v": 1.65}, {"f": 923, "v": 2.25}, {"f": 930, "v": 1.85}]},
            {"e": 69563072, "factors": [{"f": 921, "v": 1.9}, {"f": 923, "v": 1.9}]},
            {"e": 69529864, "factors": [{"f": 921, "v": 1.5}, {"f": 923, "v": 1.5}]},
        ],
        "eventMiscs": [{"id": 69563071, "score1": 1, "score2": 0}],
        "liveEventInfos": [{"eventId": 69563071, "finished": False}],
    }


def test_parse_keeps_only_tier1_root_events() -> None:
    client = FonbetClient("https://example.invalid", include=INCLUDE, exclude=EXCLUDE)
    matches = client.parse(_packet())

    assert len(matches) == 1, "должен остаться только матч IEM (мусор и карты отфильтрованы)"
    match = matches[0]
    assert isinstance(match, FonbetMatch)
    assert match.event_id == 69563071
    assert (match.team_a, match.team_b) == ("Aurora", "G2 Esports")
    assert match.odds_a == 1.65 and match.odds_b == 2.25
    assert match.score_text == "1:0"
    assert match.finished is False


def test_parse_skips_events_without_winner_market() -> None:
    """Матч в линии без рынка на победителя не нужен: ставить на него нечего."""
    packet = _packet()
    packet["customFactors"][0]["factors"] = [{"f": 930, "v": 1.85}]
    packet["liveEventInfos"] = []          # матч именно в линии, не в live
    client = FonbetClient("https://example.invalid", include=INCLUDE, exclude=EXCLUDE)
    assert client.parse(packet) == []


def test_parse_keeps_live_event_without_winner_market() -> None:
    """А live-матч без кэфов нужен: у него есть счёт, а кэфы Fonbet снимает по ходу."""
    packet = _packet()
    packet["events"][0]["place"] = "live"
    packet["customFactors"][0]["factors"] = [{"f": 930, "v": 1.85}]
    client = FonbetClient("https://example.invalid", include=INCLUDE, exclude=EXCLUDE)
    matches = client.parse(packet)
    assert len(matches) == 1
    assert matches[0].odds_a is None
    assert matches[0].score_text == "1:0"


def test_no_tier1_in_line() -> None:
    packet = _packet()
    packet["sports"] = [s for s in packet["sports"] if s["id"] in (29086, 118263, 1)]
    client = FonbetClient("https://example.invalid", include=INCLUDE, exclude=EXCLUDE)
    assert client.parse(packet) == []


def _match(score_a, score_b) -> FonbetMatch:
    return FonbetMatch(
        event_id=1, tournament="IEM", team_a="A", team_b="B", start_time=0,
        odds_a=1.5, odds_b=2.5, score_a=score_a, score_b=score_b, finished=True,
    )


@pytest.mark.parametrize(
    "score_a,score_b,expected",
    [
        (2, 0, "a"),
        (0, 2, "b"),
        (2, 1, "a"),
        (3, 2, "a"),   # Bo5
        (1, 1, None),  # незавершённая серия
        (16, 14, None),  # это раунды карты, а не серия — доверять нельзя
        (None, None, None),
    ],
)
def test_winner_from_score(score_a, score_b, expected) -> None:
    assert MatchSyncer._winner_from_score(_match(score_a, score_b)) == expected


def test_fair_odds_remove_margin(monkeypatch) -> None:
    from app.config import settings

    match = FonbetMatch(
        event_id=1, tournament="IEM", team_a="A", team_b="B", start_time=0,
        odds_a=1.65, odds_b=2.25,
    )
    monkeypatch.setattr(settings, "odds_mode", "real")
    assert MatchSyncer._prepare_odds(match) == (1.65, 2.25)

    monkeypatch.setattr(settings, "odds_mode", "fair")
    fair_a, fair_b = MatchSyncer._prepare_odds(match)
    assert abs(1 / fair_a + 1 / fair_b - 1) < 0.005  # маржа убрана (с точностью до округления до 2 знаков)
    assert fair_a > 1.65 and fair_b > 2.25

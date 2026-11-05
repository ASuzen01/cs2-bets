"""Результаты CS2 из внешнего фида (bo3.gg, без ключа).

Fonbet убирает сыгранную серию из линии и счёт по картам после этого не отдаёт,
а без результата матч висел в «ждём результат» вечно — и ставки, включая ставки
на карты, оставались в игре. Поэтому итог берём из публичного фида bo3.gg:

* `GET /matches?filter[matches.status][eq]=finished&sort=-id` — сыгранные серии
  с командами (`bet_updates.team_1.name`) и счётом по картам;
* `GET /games?filter[games.match_id][eq]=…` — карты серии: номер, победитель
  и счёт раундов (`winner_clan_name`, `winner_clan_score` / `loser_clan_score`).

Названия команд у Fonbet и в фиде английские, но пишутся по-разному
(`BB Team` / `BetBoom`, `NAVI` / `Natus Vincere`), поэтому перед сравнением
приводим их к одному виду.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp

from app.services.results import name_score, same_team

log = logging.getLogger(__name__)

BASE_URL = "https://api.bo3.gg/api/v1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# Матч считаем тем же, если старт совпал в пределах трёх часов.
MATCH_MAX_DELTA_SECONDS = 3 * 3600

# Как разные фиды называют одни и те же команды.
ALIASES = {
    "bb": "betboom",
    "bb team": "betboom",
    "betboom team": "betboom",
    "navi": "natus vincere",
    "natus vincere junior": "natus vincere",
    "faze": "faze clan",
    "the mongolz": "mongolz",
    "g2": "g2 esports",
}


def canonical(name: str) -> str:
    """Приводит название команды к общему виду (BB Team → betboom)."""
    text = " ".join(str(name or "").lower().split())
    return ALIASES.get(text, text)


def same_cs2_team(left: str, right: str) -> bool:
    return (
        same_team(canonical(left), canonical(right)) or name_score(canonical(left), canonical(right)) >= 0.9
    )


@dataclass(slots=True)
class Cs2Game:
    """Карта серии: кто взял и с каким счётом по раундам."""

    number: int
    winner: str
    loser: str
    winner_score: int
    loser_score: int


@dataclass(slots=True)
class Cs2Match:
    """Сыгранная серия: команды, счёт по картам и время старта."""

    id: int
    team_a: str
    team_b: str
    score_a: int
    score_b: int
    start_ts: int
    tournament_id: int | None = None
    best_of: int | None = None
    games: list[Cs2Game] = field(default_factory=list)

    @property
    def winner_name(self) -> str:
        return self.team_a if self.score_a > self.score_b else self.team_b

    @property
    def score_text(self) -> str:
        return f"{self.score_a}:{self.score_b}"


class Cs2ResultsClient:
    """Клиент фида результатов CS2."""

    def __init__(self, timeout: int = 20) -> None:
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._matches: list[Cs2Match] = []
        self._matches_at = 0.0
        self._games: dict[int, tuple[float, list[Cs2Game]]] = {}
        self._lock = asyncio.Lock()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get(self, path: str, params: str) -> dict[str, Any]:
        url = f"{BASE_URL}/{path}?{params}"
        async with self._lock, self.session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    # ------------------------------------------------------------- матчи
    async def finished(self, *, since_ts: int, ttl: int = 300) -> list[Cs2Match]:
        """Сыгранные серии, начиная с `since_ts` (кэш на `ttl` секунд)."""
        if self._matches and time.time() - self._matches_at < ttl:
            return [match for match in self._matches if match.start_ts >= since_ts]

        matches: list[Cs2Match] = []
        offset = 0
        empty_pages = 0
        # Идём по страницам от новых id к старым, пока в окно дат попадают матчи.
        # Порядок id почти хронологический, но не строго, поэтому останавливаемся
        # после двух страниц подряд без единого матча в окне.
        while offset < 1000 and empty_pages < 3:
            packet = await self._get(
                "matches",
                "filter%5Bmatches.status%5D%5Beq%5D=finished"
                f"&sort=-id&page%5Blimit%5D=100&page%5Boffset%5D={offset}",
            )
            rows = packet.get("results") or []
            if not rows:
                break
            in_window = 0
            for row in rows:
                match = _parse_match(row)
                if match is None:
                    continue
                matches.append(match)
                if match.start_ts >= since_ts:
                    in_window += 1
            empty_pages = 0 if in_window else empty_pages + 1
            offset += 100

        self._matches = matches
        self._matches_at = time.time()
        log.info("Результаты CS2: %s сыгранных серий", len(matches))
        return [match for match in matches if match.start_ts >= since_ts]

    async def games_of(self, match_id: int, *, ttl: int = 3600) -> list[Cs2Game]:
        cached = self._games.get(match_id)
        if cached is not None and time.time() - cached[0] < ttl:
            return cached[1]
        packet = await self._get("games", f"filter%5Bgames.match_id%5D%5Beq%5D={match_id}&page%5Blimit%5D=10")
        games = [_parse_game(row) for row in packet.get("results") or []]
        result = [game for game in games if game is not None]
        result.sort(key=lambda game: game.number)
        self._games[match_id] = (time.time(), result)
        return result

    async def find(
        self, team_a: str, team_b: str, start_ts: int, *, since_ts: int | None = None
    ) -> Cs2Match | None:
        """Ищет нашу серию в фиде по обеим командам и времени старта."""
        since = since_ts if since_ts is not None else start_ts - 86400
        try:
            matches = await self.finished(since_ts=since)
        except Exception:
            log.exception("Не смог получить результаты CS2")
            return None

        best: Cs2Match | None = None
        best_delta = float(MATCH_MAX_DELTA_SECONDS)
        for match in matches:
            direct = same_cs2_team(team_a, match.team_a) and same_cs2_team(team_b, match.team_b)
            reverse = same_cs2_team(team_a, match.team_b) and same_cs2_team(team_b, match.team_a)
            if not (direct or reverse):
                continue
            delta = abs(float(match.start_ts) - float(start_ts))
            if delta < best_delta:
                best_delta, best = delta, match
        if best is None:
            return None
        try:
            best.games = await self.games_of(best.id)
        except Exception:
            log.warning("Не смог получить карты серии %s", best.id)
            best.games = []
        return best


def _parse_match(row: dict[str, Any]) -> Cs2Match | None:
    score_a, score_b = row.get("team1_score"), row.get("team2_score")
    if score_a is None or score_b is None:
        return None
    if int(score_a) == int(score_b):
        return None  # ничьих в CS2 не бывает — данных нет

    feed = row.get("bet_updates") or {}
    team_a = (feed.get("team_1") or {}).get("name")
    team_b = (feed.get("team_2") or {}).get("name")
    if not team_a or not team_b:
        return None  # без названий сопоставить нечего

    start_ts = _parse_date(row.get("start_date"))
    return Cs2Match(
        id=int(row["id"]),
        team_a=str(team_a),
        team_b=str(team_b),
        score_a=int(score_a),
        score_b=int(score_b),
        start_ts=start_ts,
        tournament_id=row.get("tournament_id"),
        best_of=row.get("bo_type"),
    )


def _parse_game(row: dict[str, Any]) -> Cs2Game | None:
    winner = row.get("winner_clan_name")
    loser = row.get("loser_clan_name")
    score_w, score_l = row.get("winner_clan_score"), row.get("loser_clan_score")
    if not winner or not loser or score_w is None or score_l is None:
        return None
    if str(row.get("status")) not in ("finished", "done"):
        return None
    return Cs2Game(
        number=int(row.get("number") or 0),
        winner=str(winner),
        loser=str(loser),
        winner_score=int(score_w),
        loser_score=int(score_l),
    )


def _parse_date(value: Any) -> int:
    text = str(value or "")
    if len(text) < 19:
        return 0
    try:
        moment = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 0
    return int(moment.timestamp())


__all__ = [
    "ALIASES",
    "Cs2Game",
    "Cs2Match",
    "Cs2ResultsClient",
    "canonical",
    "same_cs2_team",
]

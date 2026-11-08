"""Дополнительный фид финальных результатов футбола из Flashscore.

Livescore остаётся основным источником (он отдельно отдаёт счёт за 90 минут).
У него нет части российских матчей и иногда не совпадают короткие имена команд.
Flashscore используем только как резерв, и только для матчей, завершённых в
основное время: так счёт не может незаметно включить дополнительное время.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import aiohttp

from app.services.results import (
    PAIR_MAX_DELTA_SECONDS,
    FootballResult,
    pair_orientation,
)

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
FEED_URL = "https://local-global.flashscore.ninja/2/x/feed/f_1_{offset}_3_en_1"
FSIGN = "SW9D1eZo"


def _fields(block: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in block.split("¬"):
        key, separator, value = part.partition("÷")
        if separator:
            result[key] = value
    return result


def parse_feed(raw: str) -> list[FootballResult]:
    """Разбирает только завершённые в основное время матчи с полным счётом."""
    results: list[FootballResult] = []
    for block in raw.split("¬~"):
        fields = _fields(block)
        # AB=3 — завершён; AC=3 — обычное окончание в основное время.
        # Овертайм/пенальти намеренно не берём: нужен счёт за 90 минут.
        if fields.get("AB") != "3" or fields.get("AC") != "3":
            continue
        home, away = fields.get("AE", "").strip(), fields.get("AF", "").strip()
        if not home or not away:
            continue
        try:
            start_ts = int(fields.get("AD", ""))
            score_home, score_away = int(fields.get("AG", "")), int(fields.get("AH", ""))
        except (TypeError, ValueError):
            continue
        results.append(
            FootballResult(
                home=home,
                away=away,
                score_home=score_home,
                score_away=score_away,
                final_home=score_home,
                final_away=score_away,
                status="FT",
                start_ts=start_ts,
                source="flashscore",
            )
        )
    return results


def _swap(result: FootballResult) -> FootballResult:
    """Возвращает счёт в том же порядке команд, что и в нашей базе."""
    return FootballResult(
        home=result.away,
        away=result.home,
        score_home=result.score_away,
        score_away=result.score_home,
        final_home=result.final_away,
        final_away=result.final_home,
        status=result.status,
        start_ts=result.start_ts,
        source=result.source,
    )


class FlashscoreFootballResultsClient:
    """Клиент открытого Flashscore feed для резервного поиска футбольных итогов."""

    def __init__(self, timeout: int = 18, ttl: int = 600) -> None:
        self._timeout = timeout
        self._ttl = ttl
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[int, tuple[float, list[FootballResult]]] = {}
        self._lock = asyncio.Lock()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={
                    "User-Agent": USER_AGENT,
                    "x-fsign": FSIGN,
                    "Referer": "https://www.flashscore.com/",
                    "Accept": "*/*",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def day(self, offset: int) -> list[FootballResult]:
        cached = self._cache.get(offset)
        if cached is not None and time.time() - cached[0] < self._ttl:
            return cached[1]
        url = FEED_URL.format(offset=offset)
        async with self._lock, self.session.get(url) as response:
            response.raise_for_status()
            raw = await response.text(encoding="utf-8", errors="replace")
        results = parse_feed(raw)
        self._cache[offset] = (time.time(), results)
        log.info("Результаты футбола (Flashscore, день %+d): %s матчей", offset, len(results))
        return results

    async def find(
        self,
        team_a: str,
        team_b: str,
        start_ts: int,
        *,
        english_a: str | None = None,
        english_b: str | None = None,
    ) -> FootballResult | None:
        names_a = [name for name in (english_a, team_a) if name]
        names_b = [name for name in (english_b, team_b) if name]
        now = datetime.now(timezone.utc).date()
        target = datetime.fromtimestamp(start_ts, timezone.utc).date() if start_ts else now
        best: FootballResult | None = None
        best_delta = PAIR_MAX_DELTA_SECONDS

        for shift in (-1, 0, 1):
            day = target + timedelta(days=shift)
            offset = (day - now).days
            try:
                results = await self.day(offset)
            except Exception as exc:
                log.warning("Не смог получить футбольные результаты Flashscore за %s: %s", day, exc)
                continue
            for result in results:
                orientation = next(
                    (
                        pair_orientation(name_a, name_b, result.home, result.away)
                        for name_a in names_a
                        for name_b in names_b
                        if pair_orientation(name_a, name_b, result.home, result.away)
                    ),
                    None,
                )
                if orientation is None:
                    continue
                delta = abs(float(result.start_ts) - float(start_ts or result.start_ts))
                if delta >= best_delta:
                    continue
                best_delta = delta
                best = result if orientation == "direct" else _swap(result)
        return best


__all__ = ["FlashscoreFootballResultsClient", "parse_feed"]

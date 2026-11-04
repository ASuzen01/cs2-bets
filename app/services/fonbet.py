"""Клиент линии Fonbet: реальные CS2-матчи и кэфы.

Эндпоинты (публичная линия сайта, без авторизации):
    GET {base}/events/listBase?scopeMarket=1600&lang=ru

Пакет приходит gzip'ом, внутри:
    sports[]         — дерево/сегменты турниров (имена вида "Counter-Strike. IEM Пекин...")
    events[]         — события, level=1 — сам матч (level 2/3 — карты и субрынки)
    customFactors[]  — кэфы: {e: eventId, factors: [{f: id, v: значение}]}
    eventMiscs[]     — счёт: {id: eventId, score1, score2} (до конца карты — раунды)
    liveEventInfos[] — live-детали, в т.ч. finished

Карты матча — это отдельные события level=2 с parentId = id матча и именем «1-я карта».
У них те же факторы 921/923, то есть кэфы на победителя конкретной карты.

Важно: Fonbet не публикует стабильный контракт API. Все обращения идут через один
класс, чтобы источник можно было заменить (легальный odds-провайдер) без правок логики.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# id факторов в линии Fonbet: 921 — победа первой команды, 923 — второй
FACTOR_TEAM_A = 921
FACTOR_TEAM_B = 923

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


MAP_NUMBER_RE = re.compile(r"(\d+)\s*-?\s*я?\s*карт", re.IGNORECASE)


@dataclass(slots=True)
class FonbetMap:
    """Отдельная карта матча: кэфы на её победителя и счёт по раундам."""

    event_id: int
    parent_id: int
    map_number: int
    odds_a: float | None
    odds_b: float | None
    score_a: int | None = None
    score_b: int | None = None
    finished: bool = False

    @property
    def score_text(self) -> str | None:
        if self.score_a is None or self.score_b is None:
            return None
        return f"{self.score_a}:{self.score_b}"


@dataclass(slots=True)
class FonbetMatch:
    event_id: int
    tournament: str
    team_a: str
    team_b: str
    start_time: int
    odds_a: float | None
    odds_b: float | None
    is_live: bool = False
    score_a: int | None = None
    score_b: int | None = None
    finished: bool = False

    @property
    def score_text(self) -> str | None:
        if self.score_a is None or self.score_b is None:
            return None
        return f"{self.score_a}:{self.score_b}"


def _is_cs(tournament: str) -> bool:
    name = tournament.lower()
    return "counter-strike" in name or name.startswith("cs2") or "cs 2" in name


def is_tier1(tournament: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    """Тир-1 фильтр: белый список брендов турниров минус мусор (квалифайеры, академии, 2x2)."""
    name = tournament.lower()
    if not any(word.strip().lower() and word.strip().lower() in name for word in include):
        return False
    return not any(word.strip().lower() and word.strip().lower() in name for word in exclude)


class FonbetClient:
    def __init__(
        self,
        base_url: str,
        *,
        scope_market: int = 1600,
        timeout: int = 25,
        include: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> None:
        self._base = base_url.rstrip("/")
        self._scope = scope_market
        self._timeout = timeout
        self._include = list(include)
        self._exclude = list(exclude)
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- http
    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch_packet(self) -> dict[str, Any]:
        """Скачивает и распаковывает пакет линии."""
        url = f"{self._base}/events/listBase"
        params = {"scopeMarket": self._scope, "lang": "ru"}
        async with self._lock, self.session.get(url, params=params) as response:
            response.raise_for_status()  # не ддосим: один запрос за раз
            raw = await response.read()
        if raw[:2] == b"\x1f\x8b":  # сервер отдаёт gzip без Content-Encoding
            raw = gzip.decompress(raw)
        packet = json.loads(raw)
        log.debug("Линия Fonbet: %s событий", len(packet.get("events", [])))
        return packet

    # --------------------------------------------------------------- parse
    def parse(self, packet: dict[str, Any]) -> list[FonbetMatch]:
        """Достаёт из пакета матчи CS2 тир-1 с кэфами на победителя."""
        sports = {s.get("id"): s for s in packet.get("sports", [])}
        segments: dict[int, str] = {}
        for sport_id, sport in sports.items():
            name = sport.get("name") or ""
            if _is_cs(name) and is_tier1(name, self._include, self._exclude):
                segments[sport_id] = name

        if not segments:
            log.info("В линии нет CS2-турниров тир-1 (фильтр: %s)", ", ".join(self._include))
            return []

        factors = {b["e"]: b.get("factors", []) for b in packet.get("customFactors", []) if "e" in b}
        miscs = {m["id"]: m for m in packet.get("eventMiscs", []) if "id" in m}
        lives = {
            info["eventId"]: info for info in packet.get("liveEventInfos", []) if "eventId" in info
        }

        result: list[FonbetMatch] = []
        for event in packet.get("events", []):
            if event.get("level") != 1:  # level 2/3 — карты и субрынки
                continue
            tournament = segments.get(event.get("sportId"))
            if tournament is None:
                continue
            team_a, team_b = event.get("team1"), event.get("team2")
            if not team_a or not team_b:
                continue

            odds_a = odds_b = None
            for factor in factors.get(event["id"], []):
                value = factor.get("v")
                if not value:
                    continue
                if factor.get("f") == FACTOR_TEAM_A:
                    odds_a = float(value)
                elif factor.get("f") == FACTOR_TEAM_B:
                    odds_b = float(value)

            # В линии без рынка на победителя делать нечего, а вот live-матч нужен
            # и без кэфов: у него есть счёт по картам, а кэфы Fonbet иногда снимает
            # по ходу игры — тогда играем по прошлым.
            odds_missing = odds_a is None or odds_b is None
            is_live_event = event.get("place") == "live" or event["id"] in lives
            if odds_missing and not is_live_event:
                continue

            misc = miscs.get(event["id"], {})
            live = lives.get(event["id"], {})

            result.append(
                FonbetMatch(
                    event_id=int(event["id"]),
                    tournament=tournament,
                    team_a=str(team_a),
                    team_b=str(team_b),
                    start_time=int(event.get("startTime") or 0),
                    odds_a=odds_a,
                    odds_b=odds_b,
                    is_live=event.get("place") == "live" or bool(live),
                    score_a=_int_or_none(misc.get("score1")),
                    score_b=_int_or_none(misc.get("score2")),
                    finished=bool(live.get("finished") or misc.get("finished")),
                )
            )
        return result


def _map_number(name: str, fallback: int) -> int:
    """«1-я карта» → 1; если номер не разобрали — порядковый номер события."""
    match = MAP_NUMBER_RE.search(name or "")
    return int(match.group(1)) if match else fallback


def parse_maps(packet: dict[str, Any], client: FonbetClient) -> dict[int, list[FonbetMap]]:
    """Карты для уже отобранных матчей: parent_id → [1-я карта, 2-я карта, ...]."""
    matches = client.parse(packet)
    parents = {match.event_id: match for match in matches}
    if not parents:
        return {}

    factors = {b["e"]: b.get("factors", []) for b in packet.get("customFactors", []) if "e" in b}
    miscs = {m["id"]: m for m in packet.get("eventMiscs", []) if "id" in m}
    lives = {
        info["eventId"]: info for info in packet.get("liveEventInfos", []) if "eventId" in info
    }

    result: dict[int, list[FonbetMap]] = {}
    for event in packet.get("events", []):
        if event.get("level") != 2:
            continue
        parent_id = event.get("parentId")
        if parent_id not in parents:
            continue
        name = str(event.get("name") or "")
        if "карта" not in name.lower() or "показател" in name.lower():
            continue

        odds_a = odds_b = None
        for factor in factors.get(event["id"], []):
            value = factor.get("v")
            if not value:
                continue
            if factor.get("f") == FACTOR_TEAM_A:
                odds_a = float(value)
            elif factor.get("f") == FACTOR_TEAM_B:
                odds_b = float(value)
        misc = miscs.get(event["id"], {})
        live = lives.get(event["id"], {})
        if odds_a is None and odds_b is None and not misc:
            continue  # ни кэфов, ни счёта — карта пустая

        cards = result.setdefault(int(parent_id), [])
        cards.append(
            FonbetMap(
                event_id=int(event["id"]),
                parent_id=int(parent_id),
                map_number=_map_number(name, len(cards) + 1),
                odds_a=odds_a,
                odds_b=odds_b,
                score_a=_int_or_none(misc.get("score1")),
                score_b=_int_or_none(misc.get("score2")),
                finished=bool(live.get("finished") or misc.get("finished")),
            )
        )

    for cards in result.values():
        cards.sort(key=lambda card: card.map_number)
    return result


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

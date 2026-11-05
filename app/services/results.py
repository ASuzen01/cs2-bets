"""Финальные результаты футбольных матчей из внешнего источника.

Fonbet убирает сыгранное событие из линии и финальный счёт после этого не отдаёт.
До сих пор мы считали исход по последнему live-счёту — и это неправильно: гол на
90+4 или в добавленное время ломал расчёт целиком (ставка на победителя уходила
в «не зашла», хотя команда выиграла).

Поэтому финальный счёт берём из публичного фида результатов (livescore). Там по
каждому матчу есть:

* `Eps` — статус: `FT` (основное время), `AET` (доп. время), `AP`/`PEN` (пенальти),
  `Postp.`/`Canc.`/`Aband.` (не состоялся);
* `Tr1OR`/`Tr2OR` — счёт за 90 минут (это и есть счёт для ставок на исход, тоталы
  и форы: они считаются по основному времени);
* `Tr1`/`Tr2` — счёт с учётом добавленного/дополнительного времени;
* `Trh1`/`Trh2` — счёт первого тайма.

Мы сопоставляем матч по английским названиям команд (их отдаёт Fonbet при
`lang=en`) и дате старта, поэтому в базе у футбольного матча хранятся ещё и
английские имена (`team_a_en`, `team_b_en`).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
BASE_URL = "https://prod-cdn-public-api.livescore.com/v1/api/app/date/soccer/{day}/0"

# Матч действительно закончен — счёт окончательный.
FINAL_STATUSES = {"FT", "AET", "AP", "PEN", "AW"}
# Матч не состоялся (перенесён, отменён, прерван) — ставки возвращаются.
VOID_STATUSES = {"POSTP.", "CANC.", "ABAND.", "ABD.", "POSTP", "CANC", "ABAND"}

# Матч ищем только рядом по времени: иначе можно поймать однофамильцев из другого дня.
PAIR_MAX_DELTA_SECONDS = 3 * 3600.0

# Слова, которые не помогают отличить одну команду от другой.
STOP_TOKENS = {
    "fc", "cf", "sc", "fk", "afc", "ac", "as", "ss", "ssc", "us", "ud", "cd",
    "club", "de", "the", "ks", "sk", "if", "bk", "team", "fotball", "calcio",
}

# Маркеры молодёжных и резервных составов: если они есть только у одной стороны,
# это другая команда («AEK Athens U19» ≠ «AEK Athens»).
DIFF_TOKENS = {
    "u17", "u18", "u19", "u20", "u21", "u23", "youth", "academy", "reserves",
    "women", "w", "2", "3", "b", "ii", "juniors",
}


@dataclass(slots=True)
class FootballResult:
    """Итог матча: счёт за 90 минут, счёт с овертаймом и статус."""

    home: str
    away: str
    score_home: int          # 90 минут — для исхода, тоталов, фор
    score_away: int
    final_home: int          # с учётом добавленного/дополнительного времени
    final_away: int
    status: str
    start_ts: int

    @property
    def finished(self) -> bool:
        return self.status.upper() in FINAL_STATUSES

    @property
    def void(self) -> bool:
        return self.status.upper() in VOID_STATUSES

    @property
    def score_text(self) -> str:
        return f"{self.score_home}:{self.score_away}"


def _norm(name: str) -> str:
    """Нормализация названия команды: регистр, диакритика, лишние слова."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [token for token in text.split() if token and token not in STOP_TOKENS]
    return " ".join(tokens)


# Кириллица → латиница: русские названия Fonbet сравниваем с английским фидом.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
}


def translit(name: str) -> str:
    """«АЕК Афины» → «aek afiny»: чтобы сравнивать с английскими названиями."""
    return "".join(TRANSLIT.get(char, char) for char in str(name or "").lower())


def name_score(left: str, right: str) -> float:
    """Насколько названия похожи: 1.0 — одно и то же, 0.5 — совпал один токен."""
    if same_team(left, right):
        return 1.0
    left_tokens = set(_norm(translit(left)).split())
    right_tokens = set(_norm(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    common = left_tokens & right_tokens
    return 2 * len(common) / (len(left_tokens) + len(right_tokens))


def pair_matches(
    name_a: str, name_b: str, home: str, away: str, *, min_weak: float = 0.5, min_strong: float = 0.9
) -> bool:
    """Оба названия похожи, причём одно из них — уверенно (защита от однофамильцев)."""
    direct = (name_score(name_a, home), name_score(name_b, away))
    reverse = (name_score(name_a, away), name_score(name_b, home))
    for first, second in (direct, reverse):
        if min(first, second) >= min_weak and max(first, second) >= min_strong:
            return True
    return False


def same_team(left: str, right: str, *, threshold: float = 0.82) -> bool:
    """Похоже ли одно и то же название команды: «AFC Bournemouth» ≈ «Bournemouth»."""
    first, second = _norm(left), _norm(right)
    if not first or not second:
        return False
    if first == second:
        return True

    left_tokens, right_tokens = set(first.split()), set(second.split())
    # «U19», «2», «women» и прочие маркеры не дают считать команды одной и той же
    if (left_tokens ^ right_tokens) & DIFF_TOKENS:
        return False
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return True
    return difflib.SequenceMatcher(None, first, second).ratio() >= threshold


class ResultsClient:
    """Клиент публичного фида результатов (livescore)."""

    def __init__(self, timeout: int = 20) -> None:
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, list[FootballResult]]] = {}
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

    async def day(self, day: date, *, ttl: int = 600) -> list[FootballResult]:
        """Все матчи за день; результат кэшируется на `ttl` секунд."""
        key = day.strftime("%Y%m%d")
        cached = self._cache.get(key)
        if cached is not None and time.time() - cached[0] < ttl:
            return cached[1]

        url = BASE_URL.format(day=key)
        async with self._lock, self.session.get(url) as response:
            response.raise_for_status()
            packet = await response.json(content_type=None)

        results = parse_results(packet)
        self._cache[key] = (time.time(), results)
        log.info("Результаты за %s: %s матчей", key, len(results))
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
        """Ищет итог матча по названиям команд и дате старта (±1 день)."""
        names_a = [name for name in (english_a, team_a) if name]
        names_b = [name for name in (english_b, team_b) if name]
        moment = datetime.fromtimestamp(start_ts) if start_ts else datetime.now()
        best: FootballResult | None = None
        best_delta = PAIR_MAX_DELTA_SECONDS

        for shift in (-1, 0, 1):
            day = (moment + timedelta(days=shift)).date()
            try:
                results = await self.day(day)
            except Exception as exc:  # сеть/формат — не роняем синк
                log.warning("Не смог получить результаты за %s: %s", day, exc)
                continue
            for result in results:
                for name_a in names_a:
                    for name_b in names_b:
                        if not pair_matches(name_a, name_b, result.home, result.away):
                            continue
                        delta = abs(float(result.start_ts) - float(start_ts or result.start_ts))
                        if delta < best_delta:
                            best_delta, best = delta, result
        return best


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _parse_start(value: Any) -> int:
    """`20261104174500` → unix-время (UTC)."""
    text = str(value or "")
    if len(text) < 14:
        return 0
    try:
        moment = datetime(
            int(text[0:4]), int(text[4:6]), int(text[6:8]),
            int(text[8:10]), int(text[10:12]), int(text[12:14]),
        )
    except ValueError:
        return 0
    return int(moment.timestamp())


def parse_results(packet: dict[str, Any]) -> list[FootballResult]:
    """Разбирает пакет фида: берём только матчи с осмысленным статусом и счётом."""
    results: list[FootballResult] = []
    for stage in packet.get("Stages") or []:
        for event in stage.get("Events") or []:
            status = str(event.get("Eps") or "").strip()
            if not status:
                continue
            if status.upper() not in FINAL_STATUSES and status.upper() not in VOID_STATUSES:
                continue
            home = _team_name(event.get("T1"))
            away = _team_name(event.get("T2"))
            if not home or not away:
                continue

            final_home = _int(event.get("Tr1"))
            final_away = _int(event.get("Tr2"))
            # счёт за 90 минут: именно он считается для исхода, тоталов и фор
            score_home = _int(event.get("Tr1OR"), final_home)
            score_away = _int(event.get("Tr2OR"), final_away)

            results.append(
                FootballResult(
                    home=home,
                    away=away,
                    score_home=score_home,
                    score_away=score_away,
                    final_home=final_home,
                    final_away=final_away,
                    status=status,
                    start_ts=_parse_start(event.get("Esd")),
                )
            )
    return results


def _team_name(raw: Any) -> str:
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            return str(first.get("Nm") or "")
    return ""


__all__ = [
    "DIFF_TOKENS",
    "FINAL_STATUSES",
    "PAIR_MAX_DELTA_SECONDS",
    "TRANSLIT",
    "VOID_STATUSES",
    "FootballResult",
    "ResultsClient",
    "name_score",
    "pair_matches",
    "parse_results",
    "same_team",
    "translit",
]

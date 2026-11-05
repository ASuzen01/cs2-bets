"""Результаты КХЛ из открытого фида Flashscore.

Зачем отдельный источник
------------------------
КХЛ нет ни в одном из тех фидов, которыми мы пользуемся: в livescore её не бывает,
ESPN отдаёт 400 на `hockey/khl`, khl.ru закрыт (403), championat.com теперь требует
вход через SberID. Поэтому КХЛ до сих пор висела в «ждём результат», пока админ не
введёт счёт руками.

У Flashscore есть открытый фид (тот же, что грузит их страницу результатов):

    https://local-global.flashscore.ninja/2/x/feed/f_4_<день>_3_en_1

(`4` — хоккей, `<день>` — сдвиг от сегодняшнего дня; нужен заголовок `x-fsign`).
Внутри — по блоку на матч, поля через `÷`, блоки через `¬~`. Для хоккея важно, что
кроме итогового счёта есть **голы по периодам**:

* `BA`/`BB`, `BC`/`BD`, `BE`/`BF` — 1-й, 2-й и 3-й периоды;
* `AG`/`AH` — итоговый счёт (с овертаймом и буллитами);
* `AB` = 3 — матч закончен, `AC` = 10 — в овертайме, 11 — по буллитам.

Сумма трёх периодов и есть счёт основного времени — ровно то, что нужно рынку
«Основное время». Проверено на реальных матчах: у пяти из двадцати встреч сумма
периодов не совпала с итогом, и это были именно овертаймы и серии буллитов.

Названия команд у Flashscore английские и не всегда совпадают с Fonbet: у одних
клубов это город («Magnitogorsk» вместо «Metallurg Mg»), у других — «Tractor
Chelyabinsk» вместо «Traktor». Поэтому сопоставляем не строки, а клубы: у каждого
клуба КХЛ есть набор алиасов (русских и английских), и мы ищем любой из них в
названии. Если клуб не опознан — матч не считаем: лучше подождать админа, чем
расплатиться по чужому счёту.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
FEED_URL = "https://local-global.flashscore.ninja/2/x/feed/f_4_{offset}_3_en_1"
# Публичный ключ, который сама страница Flashscore отдаёт в разметке и подставляет
# в каждый запрос фида. Без него фид отвечает пустотой.
FSIGN = "SW9D1eZo"
KHL_TOURNAMENT = "KHL"

# `AB` = 3 — матч закончен. Стадия — в `AC`: 3 — основное время, 10 — овертайм,
# 11 — буллиты. Проверено на реальных матчах: у овертаймов AB тоже 3.
FINISHED = "3"
STAGES = {"10": "OT", "11": "SO"}

# Матч ищем рядом по времени: у КХЛ время в фиде точное, часа с запасом хватает.
PAIR_MAX_DELTA_SECONDS = 6 * 3600.0

def _norm(name: str) -> str:
    """Нормализация названия: регистр, диакритика, знаки препинания."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


# Клубы КХЛ: канонический ключ → алиасы в обеих системах. Порядок перебора — от
# длинных алиасов к коротким, иначе «Dynamo Moscow» перехватит «Dynamo Minsk».
KHL_CLUBS: dict[str, tuple[str, ...]] = {
    "ak_bars": ("ак барс", "ak bars", "bars kazan", "казан", "kazan"),
    "admiral": ("адмирал", "admiral", "vladivostok", "владивосток"),
    "amur": ("амур", "amur", "khabarovsk", "хабаровск"),
    "avangard": ("авангард", "avangard", "omsk", "омск"),
    "avtomobilist": ("автомобилист", "avtomobilist", "yekaterinburg", "екатеринбург"),
    "barys": ("барыс", "barys", "astana", "астана"),
    "cska": ("цска", "cska"),
    "dinamo_minsk": ("динамо минск", "dinamo minsk", "dynamo minsk", "minsk", "минск"),
    "dynamo_moscow": ("динамо москва", "dynamo moscow", "динамо московское"),
    "kunlun": ("кунлунь", "kunlun", "шанхай", "shanghai"),
    "lada": ("лада", "lada", "togliatti", "тольятти"),
    "lokomotiv": ("локомотив", "lokomotiv", "yaroslavl", "ярославль"),
    "metallurg": ("металлург", "metallurg", "magnitogorsk", "магнитогорск"),
    "neftekhimik": ("нефтехимик", "neftekhimik", "niznekamsk", "нижнекамск"),
    "salavat": ("салават", "salavat", "ufa", "уфа"),
    "severstal": ("северсталь", "severstal", "cherepovets", "череповец"),
    "sibir": ("сибирь", "sibir", "novosibirsk", "новосибирск"),
    "ska": ("ска", "ska"),
    "sochi": ("сочи", "sochi"),
    "spartak": ("спартак", "spartak"),
    "torpedo": ("торпедо", "torpedo", "nizhny novgorod", "нижний новгород"),
    "traktor": ("трактор", "traktor", "tractor", "chelyabinsk", "челябинск"),
    "vityaz": ("витязь", "vityaz", "подольск", "podolsk"),
}

# Алиасы прогоняем через ту же нормализацию, что и названия (там «й» превращается
# в «и»), и сортируем от длинных к коротким: «динамо минск» раньше «минск».
_ALIASES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((_norm(alias), club) for club, aliases in KHL_CLUBS.items() for alias in aliases),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


@dataclass(slots=True)
class KhlResult:
    """Итог матча КХЛ: счёт основного времени и счёт с овертаймом/буллитами."""

    home: str
    away: str
    reg_home: int            # сумма трёх периодов — для рынка «Основное время»
    reg_away: int
    final_home: int          # с овертаймом и буллитами — для «Победы в матче»
    final_away: int
    status: str              # FT / OT / SO
    start_ts: int

    @property
    def finished(self) -> bool:
        return self.status in ("FT", "OT", "SO")

    @property
    def decided_late(self) -> bool:
        """Матч дошёл до овертайма или буллитов: в основное время была ничья."""
        return self.status in ("OT", "SO")


def club_key(name: str) -> str | None:
    """«Металлург Мг» и «Magnitogorsk» → 'metallurg'. Не опознали — None."""
    text = f" {_norm(name)} "
    for alias, club in _ALIASES:
        if f" {alias} " in text:
            return club
    return None


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class _Event:
    """Сырой блок матча из фида: нам нужны только счёт и названия."""

    fields: dict[str, str]

    @property
    def event_id(self) -> str:
        return self.fields.get("AA", "")

    @property
    def home(self) -> str:
        return self.fields.get("AE", "")

    @property
    def away(self) -> str:
        return self.fields.get("AF", "")

    @property
    def status_code(self) -> str:
        return self.fields.get("AB", "")

    @property
    def stage_code(self) -> str:
        return self.fields.get("AC", "")

    @property
    def start_ts(self) -> int:
        return _int(self.fields.get("AD"))

    def regulation(self) -> tuple[int, int]:
        """Счёт основного времени — сумма трёх периодов."""
        home = _int(self.fields.get("BA")) + _int(self.fields.get("BC")) + _int(self.fields.get("BE"))
        away = _int(self.fields.get("BB")) + _int(self.fields.get("BD")) + _int(self.fields.get("BF"))
        return home, away

    def final(self) -> tuple[int, int]:
        return _int(self.fields.get("AG")), _int(self.fields.get("AH"))


def parse_feed(raw: str) -> list[KhlResult]:
    """Разбирает ответ фида: берём только матчи КХЛ с итоговым счётом."""
    results: list[KhlResult] = []
    tournament = ""
    for block in raw.split("¬~"):
        fields: dict[str, str] = {}
        for part in block.split("¬"):
            key, sep, value = part.partition("÷")
            if sep:
                fields[key] = value
        if "ZA" in fields:
            tournament = fields["ZA"]
            continue
        if "AA" not in fields:
            continue
        if KHL_TOURNAMENT not in tournament.upper():
            continue
        event = _Event(fields)
        if event.status_code != FINISHED:
            continue            # матч ещё не закончился
        reg_home, reg_away = event.regulation()
        final_home, final_away = event.final()
        if final_home == final_away:
            continue            # ничьих в КХЛ не бывает: данные неполные
        status = STAGES.get(event.stage_code, "FT")
        if (reg_home, reg_away) != (final_home, final_away) and status == "FT":
            # счёт разошёлся с суммой периодов — значит, был овертайм или буллиты,
            # даже если стадия в фиде не проставлена
            status = "OT"
        results.append(
            KhlResult(
                home=event.home,
                away=event.away,
                reg_home=reg_home,
                reg_away=reg_away,
                final_home=final_home,
                final_away=final_away,
                status=status,
                start_ts=event.start_ts,
            )
        )
    return results


class KhlResultsClient:
    """Клиент фида Flashscore: матчи КХЛ по дням."""

    def __init__(self, timeout: int = 20, ttl: int = 600) -> None:
        self._timeout = timeout
        self._ttl = ttl
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[int, tuple[float, list[KhlResult]]] = {}
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
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def day(self, offset: int) -> list[KhlResult]:
        """Матчи КХЛ за день `сегодня + offset` (0 — сегодня, -1 — вчера)."""
        cached = self._cache.get(offset)
        if cached is not None and time.time() - cached[0] < self._ttl:
            return cached[1]
        url = FEED_URL.format(offset=offset)
        async with self._lock, self.session.get(url) as response:
            response.raise_for_status()
            raw = await response.text(encoding="utf-8", errors="replace")
        results = parse_feed(raw)
        self._cache[offset] = (time.time(), results)
        log.info("КХЛ (flashscore, день %+d): %s матчей", offset, len(results))
        return results

    async def find(
        self,
        team_a: str,
        team_b: str,
        start_ts: int,
        *,
        english_a: str | None = None,
        english_b: str | None = None,
    ) -> KhlResult | None:
        """Ищет итог матча КХЛ по клубам и дате старта (±1 день).

        Возвращает счёт уже в нашем порядке команд: если в фиде хозяева и гости
        стоят наоборот, счёт меняется местами.
        """
        key_a = _first_key(english_a, team_a)
        key_b = _first_key(english_b, team_b)
        if not key_a or not key_b or key_a == key_b:
            log.warning(
                "КХЛ: не опознал клубы в «%s» / «%s» — матч ждёт админа", team_a, team_b
            )
            return None

        today = datetime.now(timezone.utc).date()
        target = datetime.fromtimestamp(start_ts, tz=timezone.utc).date() if start_ts else today
        best: KhlResult | None = None
        best_delta = PAIR_MAX_DELTA_SECONDS

        for shift in (-1, 0, 1):
            moment = target + timedelta(days=shift)
            offset = (moment - today).days
            try:
                results = await self.day(offset)
            except Exception as exc:      # сеть/формат — не роняем синк
                log.warning("КХЛ: не смог получить результаты за %s: %s", moment, exc)
                continue
            for result in results:
                home, away = club_key(result.home), club_key(result.away)
                if {home, away} != {key_a, key_b}:
                    continue
                delta = abs(float(result.start_ts) - float(start_ts or result.start_ts))
                if delta >= best_delta:
                    continue
                best_delta = delta
                best = result if (home, away) == (key_a, key_b) else _swap(result)
        return best


def _first_key(*names: str | None) -> str | None:
    """Первый опознанный клуб из списка названий (английское обычно точнее)."""
    for name in names:
        if name:
            key = club_key(name)
            if key:
                return key
    return None


def _swap(result: KhlResult) -> KhlResult:
    """В фиде хозяева и гости наоборот — разворачиваем счёт под наш порядок."""
    return KhlResult(
        home=result.away,
        away=result.home,
        reg_home=result.reg_away,
        reg_away=result.reg_home,
        final_home=result.final_away,
        final_away=result.final_home,
        status=result.status,
        start_ts=result.start_ts,
    )


__all__ = [
    "KhlResult",
    "KhlResultsClient",
    "club_key",
    "parse_feed",
]

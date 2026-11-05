"""Результаты КХЛ из открытого фида Flashscore (задача 52).

КХЛ нет в livescore, ESPN отдаёт 400, khl.ru — 403, championat.com просит вход через
SberID. Зато у Flashscore есть открытый фид: там по каждому матчу есть голы **по
периодам**, поэтому можно посчитать и основное время, и итог с овертаймом.

Тесты ходят только по сохранённой фикстуре — в сеть не лезут.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.services.khl_results import (
    KhlResult,
    KhlResultsClient,
    club_key,
    parse_feed,
)

FIXTURE = Path(__file__).with_name("fixtures_khl_feed.txt")


def _feed() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ------------------------------------------------------------------ разбор фида
def test_regulation_score_is_sum_of_periods() -> None:
    """Трактор — Барыс 2:1: периоды 0:0, 0:0, 2:1 — это и есть основное время."""
    results = {result.home: result for result in parse_feed(_feed())}
    traktor = results["Tractor Chelyabinsk"]
    assert (traktor.reg_home, traktor.reg_away) == (2, 1)
    assert (traktor.final_home, traktor.final_away) == (2, 1)
    assert traktor.status == "FT"
    assert traktor.decided_late is False


def test_overtime_match_keeps_regulation_draw() -> None:
    """Сочи — Авангард: 1:1 в основное, 2:1 в овертайме — разных рынка, разные счёта."""
    results = {result.home: result for result in parse_feed(_feed())}
    sochi = results["Sochi"]
    assert (sochi.reg_home, sochi.reg_away) == (1, 1)      # ничья для «Основного времени»
    assert (sochi.final_home, sochi.final_away) == (2, 1)  # победа для «Победы в матче»
    assert sochi.status == "OT"
    assert sochi.decided_late is True


def test_other_tournaments_are_ignored() -> None:
    """В фикстуре есть блок чужого турнира — в результаты КХЛ он не попадает."""
    home_teams = {result.home for result in parse_feed(_feed())}
    assert home_teams == {"Tractor Chelyabinsk", "Sochi"}


def test_unfinished_match_is_skipped() -> None:
    """Незакончённый матч (нет итогового счёта) не считаем — иначе расплатимся раньше времени."""
    raw = _feed().replace("AB÷3", "AB÷2").replace("AG÷2", "AG÷").replace("AH÷1", "AH÷")
    assert parse_feed(raw) == []


def test_empty_feed_is_not_a_crash() -> None:
    assert parse_feed("") == []
    assert parse_feed("SA÷4¬~") == []


# ------------------------------------------------------------------ названия
@pytest.mark.parametrize(
    ("fonbet", "flashscore"),
    [
        ("Адмирал", "Vladivostok"),
        ("Металлург Мг", "Magnitogorsk"),
        ("Нефтехимик", "Niznekamsk"),
        ("Северсталь", "Cherepovets"),
        ("Амур", "Khabarovsk"),
        ("Автомобилист", "Yekaterinburg"),
        ("Торпедо", "Nizhny Novgorod"),
        ("Шанхай Дрэгонс", "Shanghai"),
        ("Сибирь", "Novosibirsk"),
        ("Трактор", "Tractor Chelyabinsk"),
        ("Динамо Москва", "Dynamo Moscow"),
        ("Динамо Минск", "Minsk"),
        ("Ак Барс", "Bars Kazan"),
        ("Салават Юлаев", "Salavat Ufa"),
        ("Локомотив", "Lokomotiv Yaroslavl"),
        ("Спартак", "Spartak Moscow"),
        ("СКА", "SKA St. Petersburg"),
        ("ЦСКА", "CSKA Moscow"),
        ("Авангард", "Avangard Omsk"),
        ("Лада", "Lada"),
    ],
)
def test_russian_and_english_names_are_the_same_club(fonbet: str, flashscore: str) -> None:
    """«Металлург Мг» и «Magnitogorsk» — один клуб: сравниваем клубы, а не строки."""
    assert club_key(fonbet) == club_key(flashscore) is not None


def test_unknown_club_is_not_guessed() -> None:
    """Незнакомое название — не ошибка, но и не повод считать матч наугад."""
    assert club_key("ВХЛ. Динамо СПб") is None
    assert club_key("") is None


def test_moscow_and_minsk_dynamo_are_different_clubs() -> None:
    """Короткий алиас «минск» не должен перехватывать московское «Динамо»."""
    assert club_key("Динамо Москва") != club_key("Динамо Минск")


# ------------------------------------------------------------------ поиск матча
class FakeDay(KhlResultsClient):
    """Клиент без сети: отдаёт заранее заданные результаты."""

    def __init__(self, results: list[KhlResult]) -> None:
        super().__init__()
        self._results = results

    async def day(self, offset: int) -> list[KhlResult]:
        return self._results


def _yesterday_ts(hour: int = 18) -> int:
    return int(time.time()) - 24 * 3600 - hour * 3600


def test_find_matches_by_club_and_time() -> None:
    result = KhlResult(
        home="Tractor Chelyabinsk", away="Barys Astana",
        reg_home=2, reg_away=1, final_home=2, final_away=1,
        status="FT", start_ts=_yesterday_ts(),
    )
    client = FakeDay([result])
    found = __import__("asyncio").run(
        client.find("Трактор", "Барыс", result.start_ts, english_a="Traktor", english_b="Barys")
    )
    assert found is result


def test_find_swaps_scores_when_feed_has_home_and_away_swapped() -> None:
    """В фиде хозяева и гости могут стоять наоборот — счёт разворачиваем."""
    result = KhlResult(
        home="Barys Astana", away="Tractor Chelyabinsk",
        reg_home=1, reg_away=2, final_home=1, final_away=2,
        status="FT", start_ts=_yesterday_ts(),
    )
    client = FakeDay([result])
    found = __import__("asyncio").run(
        client.find("Трактор", "Барыс", result.start_ts, english_a="Traktor", english_b="Barys")
    )
    assert (found.reg_home, found.reg_away) == (2, 1)
    assert (found.final_home, found.final_away) == (2, 1)


def test_find_returns_none_for_unknown_club() -> None:
    client = FakeDay([])
    found = __import__("asyncio").run(
        client.find("ХК Неизвестный", "Трактор", _yesterday_ts())
    )
    assert found is None


def test_find_does_not_take_a_match_from_another_day() -> None:
    result = KhlResult(
        home="Tractor Chelyabinsk", away="Barys Astana",
        reg_home=2, reg_away=1, final_home=2, final_away=1,
        status="FT", start_ts=_yesterday_ts(),
    )
    client = FakeDay([result])
    far = result.start_ts - 3 * 24 * 3600
    found = __import__("asyncio").run(
        client.find("Трактор", "Барыс", far, english_a="Traktor", english_b="Barys")
    )
    assert found is None


# ------------------------------------------------------------------ синк
@pytest.fixture
async def db(tmp_path):
    from app.db import Database

    database = Database(tmp_path / "khl.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "dev", "Тест")
    yield database
    await database.close()


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.sync.notify_bettors", _noop)


async def _khl_match(db, *, minutes_ago: int = 200) -> dict:
    fields = {
        "fonbet_id": 31337,
        "tournament": "Фонбет КХЛ. Регулярный сезон",
        "team_a": "Трактор",
        "team_b": "Барыс",
        "odds_a": 2.0,
        "odds_x": 4.0,
        "odds_b": 3.0,
        "sport": "hockey",
    }
    row, _ = await db.upsert_match(start_time=int(time.time()) + 3600, **fields)
    await db.place_hockey_bet(int(row["id"]), 1, None, "reg", "x", 100)
    row, _ = await db.upsert_match(
        start_time=int(time.time()) - minutes_ago * 60, **fields
    )
    await db.set_status(int(row["id"]), "live")
    return row


async def test_khl_is_settled_from_flashscore(db, monkeypatch) -> None:
    """КХЛ раньше ждала админа — теперь считается сама: 2:1 в основное время."""
    from app.services.sync import MatchSyncer

    row = await _khl_match(db)
    result = KhlResult(
        home="Tractor Chelyabinsk", away="Barys Astana",
        reg_home=2, reg_away=1, final_home=2, final_away=1,
        status="FT", start_ts=int(row["start_time"]),
    )
    syncer = MatchSyncer(bot=None, db=db, results=object())
    syncer.hockey_results = _NoNhl()
    syncer.khl_results = FakeDay([result])

    stats = {"settled": 0, "awaiting": 0, "cancelled": 0}
    await syncer._settle_hockey([], stats)

    assert stats["settled"] == 1
    match = await db.get_match(int(row["id"]))
    assert str(match["status"]) == "finished"
    assert (int(match["reg_a"]), int(match["reg_b"])) == (2, 1)
    assert str(match["result_source"]) == "flashscore"
    # ставка была на ничью основного времени — она проиграла
    assert await db.get_balance(1) == 900


class _NoNhl:
    """Заглушка фида livescore: КХЛ там нет, поэтому всегда None."""

    async def find(self, *args, **kwargs):
        return None

    async def day(self, *args, **kwargs):
        return []

    async def close(self) -> None:
        return None

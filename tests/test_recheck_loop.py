"""Перепроверка по расписанию: раз в час считаем, «всё ок» — сводкой в сутки."""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.recheck import RecheckService, has_problems, report_text


class FakeResults:
    """Футбольный фид: результата нет, значит расхождений не будет."""

    async def find(self, *args, **kwargs):
        return None

    async def day(self, *args, **kwargs):
        return []

    async def close(self) -> None:
        return None


class FakeCs2:
    async def find(self, *args, **kwargs):
        return None

    async def finished(self, *args, **kwargs):
        return []

    async def games_of(self, *args, **kwargs):
        return []

    async def close(self) -> None:
        return None


class FakeBot:
    """Вместо Telegram — просто копим тексты."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.recheck.notify_bettors", _noop)
    monkeypatch.setattr("app.services.maps.notify_map_bettors", _noop)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "loop.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


def _service(db: Database, bot: FakeBot, *, digest_hours: int = 24) -> RecheckService:
    service = RecheckService(db, db, hours=48, interval_minutes=60, digest_hours=digest_hours)
    service._notifier = bot.notify
    return service


def test_report_text_marks_failures() -> None:
    text = report_text(
        {
            "hours": 48,
            "checked": 10,
            "football": [],
            "cs2": [],
            "maps_settled": 0,
            "maps_repaired": 0,
            "unmatched": 0,
            "errors": 2,
        }
    )
    assert "Сбоев при проверке: 2" in text


def test_has_problems_sees_corrections_and_failures() -> None:
    clean = {"football": [], "cs2": [], "maps_settled": 0, "maps_repaired": 0, "errors": 0}
    assert has_problems(clean) is False
    assert has_problems({**clean, "maps_settled": 3}) is True
    assert has_problems({**clean, "errors": 1}) is True
    assert has_problems({**clean, "cs2": ["что-то исправили"]}) is True


async def test_first_clean_check_sends_digest(db: Database, monkeypatch) -> None:
    bot = FakeBot()
    service = _service(db, bot)
    service.runner = _clean_report()

    await service.run_once()

    assert len(bot.sent) == 1
    assert "ежедневная сводка" in bot.sent[0]
    assert await db.get_meta_int(RecheckService.META_KEY) > 0


async def test_repeated_clean_checks_stay_silent(db: Database, monkeypatch) -> None:
    """Раз в час — но сводка одна в сутки: иначе админам летит спам."""
    bot = FakeBot()
    service = _service(db, bot)
    service.runner = _clean_report()

    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert len(bot.sent) == 1
    assert service.runs == 3


async def test_digest_repeats_after_a_day(db: Database, monkeypatch) -> None:
    bot = FakeBot()
    service = _service(db, bot)
    service.runner = _clean_report()

    await service.run_once()
    await db.set_meta(RecheckService.META_KEY, str(int(time.time()) - 25 * 3600))
    await service.run_once()

    assert len(bot.sent) == 2


async def test_problem_is_reported_every_time(db: Database, monkeypatch) -> None:
    """Нашли расхождение — пишем сразу, не дожидаясь суточной сводки."""
    bot = FakeBot()
    service = _service(db, bot)
    service.runner = _report_with_cs2_fix()

    await service.run_once()
    await service.run_once()

    assert len(bot.sent) == 2
    assert all("ежедневная сводка" not in text for text in bot.sent)
    assert service.last_problems is True


def _clean_report():
    async def _fake(db, bot, *args, **kwargs):
        return {
            "hours": 48,
            "checked": 104,
            "football": [],
            "cs2": [],
            "maps_settled": 0,
            "maps_repaired": 0,
            "unmatched": 77,
            "errors": 0,
        }

    return _fake


def _report_with_cs2_fix():
    async def _fake(db, bot, *args, **kwargs):
        return {
            "hours": 48,
            "checked": 104,
            "football": [],
            "cs2": ["Natus Vincere — Team Falcons: по фиду 2:1 — победил Natus Vincere"],
            "maps_settled": 3,
            "maps_repaired": 0,
            "unmatched": 77,
            "errors": 0,
        }

    return _fake


async def test_schedule_defaults_to_hourly(db: Database) -> None:
    service = RecheckService(None, db)
    assert service.interval == 60
    assert service.digest_hours == 24
    assert service.enabled is True

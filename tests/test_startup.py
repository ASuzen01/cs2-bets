"""Старт и остановка приложения: поднимается без сети и без Telegram.

Смысл теста — поймать поломку на старте, из-за которой Render не может
перевести новую сборку (health-check не отвечает, деплой откатывается).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db import Database
from app.services.recheck import RecheckService


class FakeSession:
    closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    """Минимальный бот: наружу не ходит, только запоминает вызовы."""

    def __init__(self) -> None:
        self.webhook: dict[str, Any] | None = None
        self.commands: list[Any] = []
        self.session = FakeSession()

    async def get_me(self):
        class Me:
            id = 42
            username = "test_bot"

        return Me()

    async def set_webhook(self, **kwargs) -> bool:
        self.webhook = kwargs
        return True

    async def set_my_commands(self, commands) -> bool:
        self.commands = list(commands)
        return True


class FakeSyncer:
    """Синк линии: на старте только запускается, сеть не трогаем."""

    started = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Никаких внешних запросов: фиды, уведомления и синк подменены."""

    async def _noop(*args, **kwargs):
        return None

    async def _empty_list(*args, **kwargs):
        return []

    monkeypatch.setattr("bot.repair_refunded_maps", _noop_map_repair())
    monkeypatch.setattr("bot.apply_manual_results", _empty_list)
    monkeypatch.setattr("bot._set_commands", _noop)
    monkeypatch.setattr("app.services.recheck.notify_bettors", _noop)
    monkeypatch.setattr("app.services.maps.notify_map_bettors", _noop)


def _noop_map_repair():
    async def _fake(db, bot, *args, **kwargs):
        return {"settled": 0, "paid": 0}

    return _fake


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "startup.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


def _service(db: Database, bot: FakeBot) -> RecheckService:
    """Сервис с подменённой проверкой: сам расчёт тестируется отдельно."""

    async def _clean(db, bot, *args, **kwargs):
        return {
            "hours": 48,
            "checked": 0,
            "football": [],
            "cs2": [],
            "maps_settled": 0,
            "maps_repaired": 0,
            "unmatched": 0,
            "errors": 0,
        }

    service = RecheckService(bot, db, hours=48, interval_minutes=60, runner=_clean)
    service._notifier = _collect([])
    return service


def _collect(bucket: list[str]):
    async def _notify(text: str) -> None:
        bucket.append(text)

    return _notify


async def test_startup_and_shutdown_run_clean(db: Database, monkeypatch) -> None:
    import bot as bot_module

    bot = FakeBot()
    syncer = FakeSyncer()
    recheck = _service(db, bot)
    sent: list[str] = []
    recheck._notifier = _collect(sent)

    await bot_module.on_startup(
        bot=bot,
        db=db,
        syncer=syncer,
        allowed_updates=[],
        backup=None,
        recheck=recheck,
    )

    assert syncer.started is True
    assert recheck.runs == 1                     # проверка сразу на старте
    assert recheck._task is not None             # и дальше — по расписанию
    assert bot_module.settings.bot_username == "test_bot"   # имя бота из getMe

    await bot_module.on_shutdown(bot=bot, db=db, syncer=syncer, recheck=recheck)
    assert recheck._task is None


async def test_startup_works_without_recheck_service(db: Database) -> None:
    """Старые вызовы (без сервиса) не должны падать."""
    import bot as bot_module

    await bot_module.on_startup(
        bot=FakeBot(), db=db, syncer=FakeSyncer(), allowed_updates=[], backup=None
    )

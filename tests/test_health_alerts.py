from __future__ import annotations

from app.services.health_alerts import HealthAlertMonitor
from app.web.limits import metrics


class FakeDB:
    def __init__(self, sync_at: int = 0) -> None:
        self.sync_at = sync_at
        self.fail = False
        self.meta: dict[str, str] = {}

    async def get_meta(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("database offline")
        if key == "last_sync_at" and self.sync_at:
            return str(self.sync_at)
        return self.meta.get(key)

    async def set_meta(self, key: str, value: str) -> None:
        if self.fail:
            raise RuntimeError("database offline")
        self.meta[key] = str(value)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


class FakeSyncer:
    last_sync_at = 0
    last_error = None


async def test_alerts_db_and_stale_feed_then_notify_on_recovery(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "admin_ids", "123,456")
    db = FakeDB(sync_at=1)
    bot = FakeBot()
    monitor = HealthAlertMonitor(bot, db, FakeSyncer())
    monitor.started_at = 0

    db.fail = True
    await monitor.check_once(now=2000)
    assert monitor.active == {"db", "feed_stale"}
    assert [chat for chat, _ in bot.messages] == [123, 456, 123, 456]
    assert "БД недоступна" in bot.messages[0][1]
    assert "Лента" in bot.messages[2][1]

    # Непрерывное состояние не спамит сообщениями.
    await monitor.check_once(now=2060)
    assert len(bot.messages) == 4

    db.fail = False
    db.sync_at = 2060
    await monitor.check_once(now=2061)
    assert monitor.active == set()
    assert len(bot.messages) == 8
    assert all("проблема устранена" in text for _, text in bot.messages[4:])


async def test_active_alert_state_survives_monitor_restart(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "admin_ids", "123")
    db = FakeDB(sync_at=1)
    bot = FakeBot()

    first = HealthAlertMonitor(bot, db, FakeSyncer())
    first.started_at = 0
    await first.check_once(now=2000)
    assert len(bot.messages) == 1
    assert db.meta["health_alert_active:feed_stale"] == "1"

    # Recreating the monitor simulates an application restart. The active
    # incident remains recorded, so it does not send another start alert.
    restarted = HealthAlertMonitor(bot, db, FakeSyncer())
    restarted.started_at = 0
    await restarted.check_once(now=2060)
    assert len(bot.messages) == 1
    assert restarted.active == {"feed_stale"}

    db.sync_at = 3000
    await restarted.check_once(now=3001)
    assert len(bot.messages) == 2
    assert "проблема устранена" in bot.messages[-1][1]
    assert db.meta["health_alert_active:feed_stale"] == "0"

    db.sync_at = 1
    await restarted.check_once(now=4000)
    assert len(bot.messages) == 3
    assert "Лента матчей" in bot.messages[-1][1]
    assert db.meta["health_alert_active:feed_stale"] == "1"


async def test_alert_on_429_spike_is_cooled_down(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "admin_ids", "123")
    monkeypatch.setattr(metrics, "rejected", 0)
    bot = FakeBot()
    db = FakeDB(sync_at=5000)
    monitor = HealthAlertMonitor(bot, db, FakeSyncer(), rate_limit_threshold=3)
    monitor.started_at = 5000

    metrics.rejected = 3
    await monitor.check_once(now=5000)
    assert len(bot.messages) == 1
    assert "3 ответов 429" in bot.messages[0][1]

    metrics.rejected = 6
    db.sync_at = 5060
    await monitor.check_once(now=5060)
    assert len(bot.messages) == 1

    metrics.rejected = 6
    db.sync_at = 6801
    await monitor.check_once(now=6801)
    metrics.rejected = 9
    db.sync_at = 6862
    await monitor.check_once(now=6862)
    assert len(bot.messages) == 2

    monkeypatch.setattr(metrics, "rejected", 0)

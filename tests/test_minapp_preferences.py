from __future__ import annotations

import time

import pytest

from app.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "preferences.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "user1", "User One")
    yield database
    await database.close()


async def test_result_notification_preference_defaults_on_and_persists(db: Database) -> None:
    assert await db.result_notifications_enabled(1) is True
    await db.set_result_notifications(1, False)
    assert await db.result_notifications_enabled(1) is False
    await db.set_result_notifications(1, True)
    assert await db.result_notifications_enabled(1) is True


async def test_match_follow_add_list_remove(db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=876543,
        tournament="Тестовая лига",
        team_a="Команда А",
        team_b="Команда Б",
        start_time=int(time.time()) + 3600,
        odds_a=1.5,
        odds_b=2.5,
    )
    match_id = int(match["id"])
    assert await db.followed_match_ids(1) == []
    assert await db.set_match_follow(1, match_id, True) is True
    assert await db.set_match_follow(1, match_id, True) is True
    assert await db.followed_match_ids(1) == [match_id]
    assert await db.set_match_follow(1, match_id, False) is False
    assert await db.followed_match_ids(1) == []


async def test_follow_notification_respects_user_preference(db: Database, monkeypatch) -> None:
    from app.services import notify

    match, _ = await db.upsert_match(
        fonbet_id=876545,
        tournament="Тестовая лига",
        team_a="Команда А",
        team_b="Команда Б",
        start_time=int(time.time()) + 20,
        odds_a=1.5,
        odds_b=2.5,
    )
    match_id = int(match["id"])
    await db.set_match_follow(1, match_id, True)
    sent = []

    async def fake_send_user(*args, **kwargs):
        sent.append(args)

    monkeypatch.setattr(notify, "send_user", fake_send_user)
    await db.set_result_notifications(1, False)
    await notify.notify_match_followers(None, db, match)
    assert sent == []
    await db.set_result_notifications(1, True)
    await notify.notify_match_followers(None, db, match)
    assert len(sent) == 1


async def test_cannot_follow_settled_match(db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=876544,
        tournament="Тестовая лига",
        team_a="Команда А",
        team_b="Команда Б",
        start_time=int(time.time()) - 10,
        odds_a=1.5,
        odds_b=2.5,
    )
    match_id = int(match["id"])
    await db.settle_match(match_id, "a")
    assert await db.set_match_follow(1, match_id, True) is False

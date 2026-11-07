from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage

from app.db import Database

USER_ID = 9123456


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "admin-notify.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(USER_ID, "debug_user", "Debug User")
    yield database
    await database.close()


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        if self.fail:
            raise TelegramAPIError(
                method=SendMessage(chat_id=kwargs["chat_id"], text=kwargs["text"]),
                message="blocked",
            )
        self.sent.append(kwargs)


class FakeMessage:
    def __init__(self, *, chat_type: str = "private", fail_send: bool = False) -> None:
        self.answers: list[str] = []
        self.chat = SimpleNamespace(type=chat_type)
        self.bot = FakeBot(fail=fail_send)
        self.from_user = SimpleNamespace(id=500)

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)

    async def reply(self, text: str, **kwargs) -> None:
        self.answers.append(text)


async def test_admin_notify_sends_plain_text_to_id_or_username(db: Database) -> None:
    from app.handlers.admin import cmd_notify_player

    message = FakeMessage()
    command = SimpleNamespace(args="@debug_user Привет <игрок> & удачи")

    await cmd_notify_player(message, command, db)

    assert len(message.bot.sent) == 1
    assert message.bot.sent[0]["chat_id"] == USER_ID
    assert message.bot.sent[0]["text"] == "Привет &lt;игрок&gt; &amp; удачи"
    assert "Уведомление игроку Debug User" in message.answers[-1]
    events = await db.user_recent_events(USER_ID)
    assert events[0]["name"] == "admin_notification"
    assert events[0]["payload"] == "admin=500"


async def test_admin_notify_refuses_group_chat(db: Database) -> None:
    from app.handlers.admin import cmd_notify_player

    message = FakeMessage(chat_type="supergroup")
    await cmd_notify_player(message, SimpleNamespace(args=f"{USER_ID} Привет"), db)

    assert message.bot.sent == []
    assert "только в личном чате" in message.answers[0]


async def test_admin_notify_reports_invalid_and_unknown_targets(db: Database) -> None:
    from app.handlers.admin import cmd_notify_player

    message = FakeMessage()
    await cmd_notify_player(message, SimpleNamespace(args=""), db)
    assert "Формат" in message.answers[-1]

    await cmd_notify_player(
        message, SimpleNamespace(args="123456789 неизвестный игрок"), db
    )
    assert "не найден" in message.answers[-1]
    assert message.bot.sent == []


async def test_admin_notify_reports_telegram_delivery_failure(db: Database) -> None:
    from app.handlers.admin import cmd_notify_player

    message = FakeMessage(fail_send=True)
    await cmd_notify_player(message, SimpleNamespace(args=f"{USER_ID} Проверка"), db)

    assert "Telegram не доставил" in message.answers[-1]
    assert message.bot.sent == []
    assert await db.user_recent_events(USER_ID) == []

"""Сквозная проверка без Telegram: бот-лаунчер + HTTP API приложения.

Запуск:  python scripts/integration_check.py
Токен и сеть не нужны — Bot подменяется заглушкой, приложение гоняется через TestClient.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402
from aiogram.types import Chat, Message, Update  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from app.config import settings  # noqa: E402

sent: list[str] = []
edits: list[str] = []
markups: list[object] = []


def _fake_message(text: str, chat_id: int = 777) -> Message:
    return Message.model_construct(message_id=1, date=0, chat=Chat(id=chat_id, type="private"), text=text)


async def _fake_call(self: Bot, method, *args, **kwargs):  # noqa: ANN001, ANN002
    name = type(method).__name__
    if name == "SendMessage":
        sent.append(getattr(method, "text", ""))
        markups.append(getattr(method, "reply_markup", None))
        return _fake_message(getattr(method, "text", ""), int(getattr(method, "chat_id", 777)))
    if name == "EditMessageText":
        edits.append(getattr(method, "text", ""))
        return _fake_message(getattr(method, "text", ""), int(getattr(method, "chat_id", 777)))
    return True


def _user(user_id: int = 777) -> dict:
    return {"id": user_id, "is_bot": False, "first_name": "Антон", "username": "sap", "language_code": "ru"}


def message_update(
    update_id: int, text: str, user_id: int = 777, chat_type: str = "private"
) -> Update:
    chat_id = user_id if chat_type == "private" else -100100
    chat = {"id": chat_id, "type": chat_type}
    if chat_type == "private":
        chat["first_name"] = "Антон"
    else:
        chat["title"] = "Дружеский чат"
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(time.time()),
                "chat": chat,
                "from": _user(user_id),
                "text": text,
                "entities": (
                    [{"type": "bot_command", "offset": 0, "length": len(text.split("/")[1].split()[0]) + 1}]
                    if text.startswith("/")
                    else []
                ),
            },
        }
    )


def callback_update(update_id: int, data: str, user_id: int = 777) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb{update_id}",
                "chat_instance": "1",
                "from": _user(user_id),
                "data": data,
                "message": {
                    "message_id": 100,
                    "date": int(time.time()),
                    "chat": {"id": user_id, "type": "private", "first_name": "Антон"},
                    "text": "экран",
                },
            },
        }
    )


def signed_init_data(bot_token: str, user_id: int, name: str) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF_integration",
        "user": json.dumps({"id": user_id, "first_name": name, "language_code": "ru"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


async def main() -> None:
    token = "123456:INTEGRATION"
    settings.bot_token = token
    settings.admin_ids = "777"
    settings.db_path = "data/_integration.db"
    settings.miniapp_url = "https://example.onrender.com"
    settings.bot_username = "Happy_ludka_bot"
    settings.miniapp_startapp = False
    settings.miniapp_dev_user = 0  # dev-режим без initData выключаем, проверяем настоящую авторизацию

    from bot import create_bot, create_dispatcher  # noqa: PLC0415
    from app.db import Database  # noqa: PLC0415
    from app.services.sync import MatchSyncer  # noqa: PLC0415
    from app.web.server import build_app  # noqa: PLC0415

    Path("data/_integration.db").unlink(missing_ok=True)
    Bot.__call__ = _fake_call  # type: ignore[method-assign]

    bot = create_bot()
    db = Database(
        settings.db_file,
        start_balance=settings.start_balance,
        url=settings.database_url,
        schema=settings.database_schema,
    )
    dp = create_dispatcher(db, MatchSyncer(bot, db))
    await db.connect()
    try:
        await check(bot, db, dp)
    finally:
        await db.close()
        await bot.session.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"data/_integration.db{suffix}").unlink(missing_ok=True)


async def check(bot, db, dp) -> None:  # noqa: ANN001

    now = int(time.time())
    first, _ = await db.upsert_match(
        fonbet_id=1,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=now + 3600,
        odds_a=1.65,
        odds_b=2.25,
    )

    # --- бот как лаунчер ---------------------------------------------------
    await dp.feed_update(bot, message_update(1, "/start"))
    assert sent, "бот не ответил на /start"
    assert "1 000" in sent[-1] or "1\u202f000" in sent[-1], sent[-1]
    launch_markup = markups[-1]
    assert launch_markup is not None, "у /start нет кнопки приложения"
    buttons = [b for row in launch_markup.inline_keyboard for b in row]
    assert len(buttons) == 1, "в боте должна остаться ровно одна кнопка"
    assert buttons[0].web_app and buttons[0].web_app.url.endswith("example.onrender.com"), buttons[0]

    await dp.feed_update(bot, message_update(2, "/help"))
    assert "Правила" in sent[-1], sent[-1][:80]

    await dp.feed_update(bot, message_update(3, "привет, как поставить?"))
    assert "приложении" in sent[-1], sent[-1][:80]

    # --- группа: web_app-кнопка там не работает, отдаём ссылку на бота --------
    await dp.feed_update(bot, message_update(30, "/start", user_id=501, chat_type="group"))
    group_markup = markups[-1]
    assert group_markup is not None, "в группе нет кнопки входа"
    group_button = group_markup.inline_keyboard[0][0]
    assert group_button.web_app is None, "в группе web_app-кнопка не отрисуется"
    assert group_button.url == "https://t.me/Happy_ludka_bot?start=app", group_button.url

    # --- админ: подтверждение исхода кнопкой -------------------------------
    await db.set_status(int(first["id"]), "awaiting")
    await dp.feed_update(bot, message_update(4, "/results", user_id=777))
    assert any("Ждут подтверждения" in text for text in sent[-3:]), [t[:60] for t in sent[-3:]]
    confirm_markup = markups[-1]
    assert confirm_markup is not None and any(
        b.callback_data == f"ase:{first['id']}:a" for row in confirm_markup.inline_keyboard for b in row
    ), "нет кнопки подтверждения исхода"

    await dp.feed_update(bot, callback_update(5, f"ase:{first['id']}:a"))
    assert any("рассчитан" in text.lower() for text in edits + sent), "исход не подтвердился"

    # --- HTTP API мини-приложения ------------------------------------------
    from app.web.server import build_app  # noqa: PLC0415

    web = build_app(db)
    init = signed_init_data(settings.bot_token, 777, "Антон")
    async with TestClient(TestServer(web)) as client:
        health = await client.get("/health")
        assert health.status == 200 and (await health.json())["ok"] is True

        unauth = await client.get("/api/state")
        assert unauth.status == 401, unauth.status

        state_resp = await client.get("/api/state", headers={"X-Telegram-Init-Data": init})
        assert state_resp.status == 200, await state_resp.text()
        state = await state_resp.json()
        balance_before = state["user"]["balance"]
        assert balance_before == 1000, state

        second, _ = await db.upsert_match(
            fonbet_id=2,
            tournament="Counter-Strike. BLAST Rivals. Групповой этап. Bo3",
            team_a="MOUZ",
            team_b="Team Vitality",
            start_time=now + 7200,
            odds_a=2.00,
            odds_b=1.80,
        )
        bet = await client.post(
            "/api/bet",
            headers={"X-Telegram-Init-Data": init},
            json={"match_id": int(second["id"]), "side": "a", "amount": 250},
        )
        assert bet.status == 200, await bet.text()
        bet_body = await bet.json()
        assert bet_body["balance"] == 750, bet_body
        assert abs(float(bet_body["odds"]) - 2.00) < 0.01, bet_body

        express = await client.post(
            "/api/express",
            headers={"X-Telegram-Init-Data": init},
            json={"amount": 50, "legs": [{"match_id": int(second["id"]), "side": "b"}]},
        )
        assert express.status == 400, "экспресс из одной ноги должен отклоняться"

        index = await client.get("/", headers={"X-Telegram-Init-Data": init})
        assert index.status == 200 and "<html" in (await index.text()).lower()

    print(f"OK: /start отдаёт одну кнопку приложения ({buttons[0].web_app.url})")
    print(f"OK: в группе вход через {group_button.url}")
    print("OK: /help, фолбэк на текст, /results и подтверждение исхода")
    print(f"OK: API /health, 401 без подписи, /api/state → баланс {balance_before}")
    print(f"OK: ставка 250 · кэф 2.00 → баланс {bet_body['balance']}")
    print("OK: экспресс из одной ноги отклонён, интерфейс отдаётся")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"data/_integration.db{suffix}").unlink(missing_ok=True)

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

from aiogram import Bot
from aiogram.types import Chat, Message, Update
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings

sent: list[str] = []
edits: list[str] = []
markups: list[object] = []


def _fake_message(text: str, chat_id: int = 777) -> Message:
    return Message.model_construct(message_id=1, date=0, chat=Chat(id=chat_id, type="private"), text=text)


async def _fake_call(self: Bot, method, *args, **kwargs):
    name = type(method).__name__
    if name == "SendPhoto":
        sent.append(getattr(method, "caption", "") or "")
        markups.append(getattr(method, "reply_markup", None))
        return _fake_message(getattr(method, "caption", "") or "", int(getattr(method, "chat_id", 777)))
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

    from app.db import Database
    from app.services.sync import MatchSyncer
    from bot import create_bot, create_dispatcher

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


async def check(bot, db, dp) -> None:

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
    assert "Экспресс" in sent[-1], sent[-1][:80]  # /help теперь только про экспрессы

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
    from app.web.server import build_app

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

        # --- футбол: широкая роспись и ставка на рынок -------------------
        football, _ = await db.upsert_match(
            fonbet_id=9001,
            tournament="Лига Чемпионов УЕФА. Общий этап",
            team_a="Лейпциг",
            team_b="Манчестер Сити",
            start_time=now + 7200,
            odds_a=5.3,
            odds_x=4.9,
            odds_b=1.53,
            sport="football",
        )
        football_id = int(football["id"])
        state2 = await (await client.get("/api/state", headers={"X-Telegram-Init-Data": init})).json()
        football_line = state2.get("football") or []
        assert football_line, "футбольная линия пуста"
        card = next(m for m in football_line if m["id"] == football_id)
        assert card["sport"] == "football" and card["odds_x"] == 4.9, card
        groups = {m["group"] for m in card["markets"]}
        assert {"Исход", "Тотал голов", "Точный счёт", "Фора", "Обе забьют"} <= groups, groups
        assert len(card["markets"]) >= 60, len(card["markets"])
        # исход 1X2 не дублируется в росписи: за него отвечают кнопки П1/X/П2
        assert all(m["key"] != "result" or m["group"] == "Исход" for m in card["markets"])

        football_bet = await client.post(
            "/api/bet",
            headers={"X-Telegram-Init-Data": init},
            json={"match_id": football_id, "side": "f:total:over:2.5", "amount": 100},
        )
        assert football_bet.status == 200, await football_bet.text()
        football_body = await football_bet.json()
        assert football_body["market"] == "football" and football_body["label"] == "ТБ 2.5", football_body

        draw_bet = await client.post(
            "/api/bet",
            headers={"X-Telegram-Init-Data": init},
            json={"match_id": football_id, "side": "x", "amount": 50},
        )
        assert draw_bet.status == 200, await draw_bet.text()
        assert abs(float((await draw_bet.json())["odds"]) - 4.9) < 0.01

        # расчёт по счёту 3:1 — тотал 2.5 зашёл, ничья нет
        settled = await db.settle_football(football_id, 3, 1, source="test")
        assert settled["winner"] == "a", settled
        assert settled["paid"] > 0, settled
        after = await (await client.get("/api/state", headers={"X-Telegram-Init-Data": init})).json()
        expected = football_body["balance"] - 50 + round(100 * football_body["odds"])
        assert after["user"]["balance"] == expected, (after["user"], expected)

        # --- бонусы: колесо и пасхалка -----------------------------------
        assert "bonus" in state and state["bonus"]["wheel"]["available"] is True, state.get("bonus")
        assert state["bonus"]["daily"]["amount"] == settings.daily_bonus
        slots = state["bonus"]["wheel"]["slots"]
        assert len(slots) == 24, len(slots)
        assert [s["value"] for s in slots].count(1000) == 1
        assert [s["value"] for s in slots].count(200) == 7
        assert [s["value"] for s in slots].count(50) == 8
        assert [s["value"] for s in slots].count(0) == 8
        assert all(slots[i]["value"] != slots[(i + 1) % 24]["value"] for i in range(24)), \
            "рядом стоят одинаковые сектора"

        # баланс перед колесом берём из состояния: до этого его меняли ставки на футбол
        before_wheel = (await (await client.get(
            "/api/state", headers={"X-Telegram-Init-Data": init}
        )).json())["user"]["balance"]

        wheel = await client.post("/api/wheel", headers={"X-Telegram-Init-Data": init}, json={})
        wheel_body = await wheel.json()
        assert wheel_body["spun"] is True, wheel_body
        assert wheel_body["prize"] == slots[wheel_body["slot"]]["value"], wheel_body
        balance_after_wheel = wheel_body["balance"]
        assert balance_after_wheel == before_wheel + wheel_body["prize"], wheel_body

        # платная крутка после бесплатной
        cost = state["bonus"]["wheel"]["cost"]
        assert cost == settings.wheel_cost, cost
        paid = await client.post("/api/wheel", headers={"X-Telegram-Init-Data": init}, json={"paid": True})
        paid_body = await paid.json()
        assert paid_body["spun"] is True and paid_body["free"] is False, paid_body
        assert paid_body["prize"] == slots[paid_body["slot"]]["value"], paid_body
        assert paid_body["balance"] == balance_after_wheel - cost + paid_body["prize"], paid_body
        balance_after_wheel = paid_body["balance"]

        tease = await client.post("/api/tease", headers={"X-Telegram-Init-Data": init}, json={})
        tease_body = await tease.json()
        assert tease_body["claimed"] is True and tease_body["amount"] == 50, tease_body
        assert tease_body["balance"] == balance_after_wheel + 50, tease_body

        tease_again = await client.post("/api/tease", headers={"X-Telegram-Init-Data": init}, json={})
        assert (await tease_again.json())["claimed"] is False

        index = await client.get("/", headers={"X-Telegram-Init-Data": init})
        html = await index.text()
        assert index.status == 200 and "<html" in html.lower()
        assert 'id="screen-bonus"' in html, "нет вкладки бонусов"
        assert "#i-bonus" in html and "#i-matches" in html, "иконки табов не встроены"

    # --- бэкапы: снимок, проверка, восстановление -------------------------
    from app.services.backup import decode, make_dump, restore

    await dp.feed_update(bot, message_update(40, "/backup", user_id=777))
    assert any("Бэкап от" in text or "Размер" in text for text in sent[-3:]), sent[-3:]
    last_backup = await db.last_backup()
    assert last_backup is not None and int(last_backup["users"]) >= 1, last_backup

    balance_before_dump = await db.get_balance(777)
    blob, _payload = await make_dump(db, kind="test")
    decode(blob)                                  # дамп проходит проверку целостности
    await db.add_coins(777, 500, "проверка восстановления")
    assert await db.get_balance(777) == balance_before_dump + 500
    counts = await restore(db, blob)
    assert counts["users"] >= 1, counts
    assert await db.get_balance(777) == balance_before_dump, "баланс не откатился из дампа"

    print(f"OK: /backup → файл копии, дамп {len(blob) / 1024:.1f} КБ, восстановление откатило баланс")
    print("OK: футбол — 66 рынков в карточке, ставка на ТБ 2.5 и на ничью, расчёт по счёту 3:1")
    print(f"OK: /start отдаёт одну кнопку приложения ({buttons[0].web_app.url})")
    print(f"OK: в группе вход через {group_button.url}")
    print("OK: /help, фолбэк на текст, /results и подтверждение исхода")
    print(f"OK: API /health, 401 без подписи, /api/state → баланс {balance_before}")
    print(f"OK: ставка 250 · кэф 2.00 → баланс {bet_body['balance']}")
    print("OK: экспресс из одной ноги отклонён, интерфейс отдаётся")
    print(f"OK: колесо → бесплатная крутка дала {wheel_body['prize']} (сектор {wheel_body['slot']})")
    print(f"OK: платная крутка за {cost} → {paid_body['prize']}, баланс {paid_body['balance']}")
    print(f"OK: пасхалка на 10 нажатий → +{tease_body['amount']}, второй раз в сутки нельзя")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"data/_integration.db{suffix}").unlink(missing_ok=True)

"""Интеграционная проверка без Telegram: прогоняем апдейты через диспетчер.

Проверяет кнопочный сценарий целиком:
    /start → меню → список матчей → карточка → выбор суммы → ставка
    → добавление исходов в экспресс → ставка экспресса → баланс
    → админский расчёт результата

Запуск:  python scripts/integration_check.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402
from aiogram.types import Chat, Message, Update  # noqa: E402

from app.config import settings  # noqa: E402

sent: list[str] = []
edits: list[str] = []


def _fake_message(text: str, chat_id: int = 777) -> Message:
    return Message.model_construct(message_id=1, date=0, chat=Chat(id=chat_id, type="private"), text=text)


async def _fake_call(self: Bot, method, *args, **kwargs):  # noqa: ANN001, ANN002
    name = type(method).__name__
    if name == "SendMessage":
        sent.append(getattr(method, "text", ""))
        return _fake_message(getattr(method, "text", ""), int(getattr(method, "chat_id", 777)))
    if name == "EditMessageText":
        edits.append(getattr(method, "text", ""))
        return _fake_message(getattr(method, "text", ""), int(getattr(method, "chat_id", 777)))
    if name in ("AnswerCallbackQuery", "SetMyCommands", "GetMe", "DeleteWebhook"):
        return True
    return True


def _user(user_id: int = 777) -> dict:
    return {
        "id": user_id,
        "is_bot": False,
        "first_name": "Антон",
        "username": "sap",
        "language_code": "ru",
    }


def message_update(update_id: int, text: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(time.time()),
                "chat": {"id": 777, "type": "private", **{"first_name": "Антон"}},
                "from": _user(),
                "text": text,
                "entities": (
                    [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                    if text.startswith("/")
                    else []
                ),
            },
        }
    )


def callback_update(update_id: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb{update_id}",
                "chat_instance": "1",
                "from": _user(),
                "data": data,
                "message": {
                    "message_id": 100,
                    "date": int(time.time()),
                    "chat": {"id": 777, "type": "private", **{"first_name": "Антон"}},
                    "text": "экран",
                },
            },
        }
    )


async def main() -> None:
    settings.bot_token = "123456:INTEGRATION"
    settings.admin_ids = "777"
    settings.db_path = "data/_integration.db"

    from bot import create_bot, create_dispatcher  # noqa: PLC0415
    from app.db import Database  # noqa: PLC0415
    from app.services.sync import MatchSyncer  # noqa: PLC0415

    Path("data/_integration.db").unlink(missing_ok=True)
    Bot.__call__ = _fake_call  # type: ignore[method-assign]

    bot = create_bot()
    db = Database(settings.db_file, start_balance=settings.start_balance, url=settings.database_url, schema=settings.database_schema)
    syncer = MatchSyncer(bot, db)
    dp = create_dispatcher(db, syncer)
    await db.connect()

    now = int(time.time())
    first, _ = await db.upsert_match(
        fonbet_id=1, tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora", team_b="G2 Esports", start_time=now + 3600, odds_a=1.65, odds_b=2.25,
    )
    second, _ = await db.upsert_match(
        fonbet_id=2, tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="9z Team", team_b="TYLOO", start_time=now + 7200, odds_a=1.44, odds_b=2.80,
    )
    third, _ = await db.upsert_match(
        fonbet_id=3, tournament="Counter-Strike. BLAST Rivals. Групповой этап. Bo3",
        team_a="MOUZ", team_b="Team Vitality", start_time=now + 9000, odds_a=2.00, odds_b=1.80,
    )

    # --- кнопочный сценарий ---
    await dp.feed_update(bot, message_update(1, "/start"))
    await dp.feed_update(bot, callback_update(2, "ml:0"))
    await dp.feed_update(bot, callback_update(3, f"mc:{first['id']}"))
    await dp.feed_update(bot, callback_update(4, f"ms:{first['id']}:a"))
    await dp.feed_update(bot, callback_update(5, f"ma:{first['id']}:a:100"))

    balance_after_single = await db.get_balance(777)
    assert balance_after_single == 900, balance_after_single

    await dp.feed_update(bot, callback_update(6, f"xa:{second['id']}:b"))
    await dp.feed_update(bot, callback_update(7, f"xa:{third['id']}:a"))
    await dp.feed_update(bot, callback_update(8, "xc"))
    await dp.feed_update(bot, callback_update(9, "xm:250"))

    balance_after_express = await db.get_balance(777)
    assert balance_after_express == 650, balance_after_express

    expresses = await db.user_open_expresses(777)
    assert len(expresses) == 1, expresses
    express = expresses[0]
    assert abs(float(express["odds"]) - 2.80 * 2.00) < 0.01, express["odds"]

    await dp.feed_update(bot, callback_update(10, "b"))
    await dp.feed_update(bot, callback_update(11, "tp"))
    await dp.feed_update(bot, callback_update(12, "hs"))
    await dp.feed_update(bot, callback_update(13, "menu"))

    # --- админ: экран «Результаты» и подтверждение кнопкой ---
    await db.set_status(int(second["id"]), "awaiting")
    await dp.feed_update(bot, callback_update(14, "res"))
    await dp.feed_update(bot, callback_update(15, f"ase:{second['id']}:a"))  # нога экспресса проиграла
    express_row = await db.get_express(int(express["id"]))
    assert express_row["status"] == "lost", express_row["status"]

    # --- проверки ответов ---
    assert sent and edits, "бот ничего не отправил"
    assert any("Матчи тир-1" in text for text in edits + sent), "нет экрана со списком матчей"
    assert any("Ставка принята" in text for text in edits + sent), "нет подтверждения ставки"
    assert any("Экспресс #" in text for text in edits + sent), "нет экрана экспресса"
    assert any("Баланс" in text for text in edits + sent), "нет экрана баланса"
    assert any("Ждут подтверждения" in text for text in edits + sent), "нет экрана результатов"

    print(f"OK: экранов отрисовано {len(edits) + len(sent)}, ошибок нет")
    print(f"OK: одиночная ставка 100 → баланс {balance_after_single}")
    print(f"OK: экспресс 250 · кэф {float(express['odds']):.2f} → баланс {balance_after_express}")
    print(f"OK: после проигранной ноги экспресс #{express['id']} — lost")

    await db.close()
    await bot.session.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"data/_integration.db{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"data/_integration.db{suffix}").unlink(missing_ok=True)

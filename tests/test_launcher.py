"""Бот — только лаунчер: одна кнопка, которая открывает мини-приложение."""

from __future__ import annotations

import pytest

from app import keyboards, texts
from app.config import settings


@pytest.fixture()
def miniapp_url(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "miniapp_url", "https://cs2-bets-bot.onrender.com")
    return "https://cs2-bets-bot.onrender.com"


def test_app_kb_single_button(miniapp_url: str) -> None:
    markup = keyboards.app_kb()
    assert markup is not None
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len(buttons) == 1, "в боте должна остаться одна кнопка — открыть приложение"
    assert buttons[0].web_app is not None
    assert buttons[0].web_app.url == miniapp_url


def test_app_kb_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "miniapp_url", "")
    assert keyboards.app_kb() is None


def test_group_kb_is_url_button(miniapp_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """В группе web_app-кнопка не отрисовывается — отдаём ссылку на бота."""
    monkeypatch.setattr(settings, "bot_username", "Happy_ludka_bot")
    monkeypatch.setattr(settings, "miniapp_startapp", False)
    markup = keyboards.app_kb(group=True)
    assert markup is not None
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len(buttons) == 1
    assert buttons[0].web_app is None, "в группе web_app-кнопку Telegram не покажет"
    assert buttons[0].url == "https://t.me/Happy_ludka_bot?start=app"


def test_group_kb_startapp_variant(miniapp_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_username", "Happy_ludka_bot")
    monkeypatch.setattr(settings, "miniapp_startapp", True)
    markup = keyboards.app_kb(group=True)
    assert markup is not None
    url = markup.inline_keyboard[0][0].url
    assert url == "https://t.me/Happy_ludka_bot?startapp=app"


def test_group_kb_without_username(miniapp_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_username", "")
    assert keyboards.app_kb(group=True) is None


def test_group_start_text() -> None:
    text = texts.GROUP_START.format(name="Антон")
    assert "приложени" in text.lower()
    assert "баланс" in text.lower()


def test_start_text_has_balance_and_no_meta_phrases() -> None:
    text = texts.START.format(name="Антон", balance=texts.money(1250))
    assert "Антон" in text
    assert "1\u202f250" in text
    # приветствие короткое: без объяснений «всё внутри приложения» и «тир-1»
    lowered = text.lower()
    assert "всё внутри приложения" not in lowered
    assert "все внутри приложения" not in lowered
    assert "кнопки в чате" not in lowered
    assert "тир-1" not in lowered


def test_rules_are_only_about_express() -> None:
    """Правила короткие: только экспрессы, без лишней теории про линию."""
    rules = texts.rules_text().lower()
    assert "экспресс" in rules
    assert str(settings.max_express_legs) in rules
    assert "fonbet" not in rules
    assert "тир-1" not in rules
    assert "кэф фиксируется" not in rules


def test_confirm_result_kb_admin_callbacks() -> None:
    match = {"id": 7, "team_a": "Aurora", "team_b": "G2 Esports"}
    markup = keyboards.confirm_result_kb(match)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks == ["ase:7:a", "ase:7:b", "av:7"]


def test_routers_have_no_button_ui() -> None:
    from aiogram import Dispatcher

    from app.handlers import setup_handlers

    dp = Dispatcher()
    setup_handlers(dp)
    names = {router.name for router in dp.sub_routers}
    assert names == {"admin", "common", "coupon", "misc"}
    assert "ui" not in names


def test_bet_result_texts() -> None:
    common = {
        "team_a": "Aurora",
        "team_b": "G2 Esports",
        "picked": "Aurora",
        "amount": 200,
        "odds_value": 1.65,
        "balance": 1580,
        "score": "2:0",
    }
    won = texts.bet_result(won=True, void=False, payout=330, **common)
    assert "Ставка сыграла" in won and "330" in won and "1\u202f580" in won

    lost = texts.bet_result(won=False, void=False, payout=0, **common)
    assert "не зашла" in lost and "сгорела" in lost

    void = texts.bet_result(won=False, void=True, payout=200, **common)
    assert "возвращена" in void and "200" in void

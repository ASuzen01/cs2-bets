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


def test_start_text_has_balance_and_no_buttons_hint() -> None:
    text = texts.START.format(name="Антон", balance=texts.money(1250))
    assert "Антон" in text
    assert "1\u202f250" in text
    assert "приложени" in text.lower()


def test_rules_mention_fonbet_and_express_limit() -> None:
    rules = texts.rules_text()
    assert "Fonbet" in rules
    assert str(settings.max_express_legs) in rules


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
    assert names == {"admin", "common", "misc"}
    assert "ui" not in names

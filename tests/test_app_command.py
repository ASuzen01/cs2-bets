"""Команда /app: ссылка на приложение для общего чата."""

from __future__ import annotations

from app import keyboards, texts
from app.config import settings


def test_deep_link_uses_bot_username(monkeypatch) -> None:
    monkeypatch.setattr(settings, "bot_username", "Happy_ludka_bot")
    monkeypatch.setattr(settings, "miniapp_startapp", False)
    assert keyboards.deep_link() == "https://t.me/Happy_ludka_bot?start=app"


def test_group_button_is_a_link_not_webapp(monkeypatch) -> None:
    monkeypatch.setattr(settings, "bot_username", "Happy_ludka_bot")
    monkeypatch.setattr(settings, "miniapp_url", "https://cs2-bets-bot.onrender.com")
    markup = keyboards.app_kb(group=True)
    assert markup is not None
    button = markup.inline_keyboard[0][0]
    # в группе Telegram не рисует web_app — там обычная ссылка
    assert button.web_app is None
    assert button.url == "https://t.me/Happy_ludka_bot?start=app"


def test_app_text_shows_link() -> None:
    link = "https://t.me/Happy_ludka_bot?start=app"
    text = texts.APP_LINK.format(link=f'<a href="{link}">{link}</a>')
    assert link in text
    assert "{link}" not in text

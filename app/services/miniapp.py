"""Проверка initData мини-приложения (Telegram WebApp).

Алгоритм из документации Telegram:
    secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
    hash       = HMAC_SHA256(key=secret_key, msg=data_check_string)

Плюс проверка свежести auth_date, чтобы ссылку нельзя было переиспользовать вечно.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class AuthError(Exception):
    """initData отсутствует, подделан или просрочен."""


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def validate_init_data(
    init_data: str, bot_token: str, *, max_age: int = 24 * 3600
) -> dict[str, Any]:
    """Возвращает разобранные поля initData или поднимает AuthError."""
    if not init_data:
        raise AuthError("initData пустой")
    if not bot_token:
        raise AuthError("BOT_TOKEN не задан")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise AuthError("в initData нет hash")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    expected = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("подпись initData не сходится")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise AuthError("auth_date не число") from exc

    if auth_date <= 0:
        raise AuthError("в initData нет auth_date")
    if max_age > 0 and time.time() - auth_date > max_age:
        raise AuthError("initData устарел, откройте приложение заново")

    user: dict[str, Any] = {}
    if pairs.get("user"):
        try:
            user = json.loads(pairs["user"])
        except json.JSONDecodeError as exc:
            raise AuthError("поле user не JSON") from exc

    return {
        "user": user,
        "user_id": int(user.get("id") or 0),
        "username": user.get("username"),
        "full_name": " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        )
        or None,
        "auth_date": auth_date,
        "query_id": pairs.get("query_id"),
    }


def sign_init_data(bot_token: str, user: dict[str, Any], *, auth_date: int | None = None) -> str:
    """Собирает валидный initData — нужно только для тестов и локальной разработки."""
    pairs = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    digest = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    from urllib.parse import urlencode

    return urlencode({**pairs, "hash": digest})

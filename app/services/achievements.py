"""Достижения: маленькие поводы вернуться.

Считаем не по событиям, а по базе: у каждого достижения условие на «сухие»
цифры игрока (`Database.achievement_stats`). Поэтому достижение нельзя потерять
и не нужно переписывать историю, когда появляется новое.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from app.db import Database


def _stats_of(stats: dict[str, Any]) -> dict[str, Any]:
    return stats


ACHIEVEMENTS: Final[tuple[dict[str, Any], ...]] = (
    {
        "code": "first_bet",
        "title": "Первая ставка",
        "desc": "Поставить на любой матч",
        "icon": "🎯",
        "needs": lambda s: s["placed"] >= 1,
    },
    {
        "code": "first_win",
        "title": "Первая победа",
        "desc": "Любая зашедшая ставка",
        "icon": "✅",
        "needs": lambda s: s["won"] >= 1,
    },
    {
        "code": "express_2",
        "title": "Экспресс собрался",
        "desc": "Экспресс из двух и более исходов",
        "icon": "🧩",
        "needs": lambda s: s["express_placed"] >= 1,
    },
    {
        "code": "express_win",
        "title": "Экспресс зашёл",
        "desc": "Выиграть экспресс",
        "icon": "🏆",
        "needs": lambda s: s["express_won"] >= 1,
    },
    {
        "code": "three_lines",
        "title": "Три линии",
        "desc": "Экспресс из CS2, футбола и хоккея",
        "icon": "🌍",
        "needs": lambda s: s["sports_in_express"] >= 3,
    },
    {
        "code": "odds_5",
        "title": "Пятёрка",
        "desc": "Ставка или экспресс с кэфом от 5.00",
        "icon": "🚀",
        "needs": lambda s: s["max_odds"] >= 5.0,
    },
    {
        "code": "five_wins",
        "title": "Пять заходов",
        "desc": "Пять выигранных ставок",
        "icon": "✋",
        "needs": lambda s: s["won"] >= 5,
    },
    {
        "code": "casino_win",
        "title": "Поймал игру",
        "desc": "Выиграть в рулетку или кости",
        "icon": "🎲",
        "needs": lambda s: s["casino_won"] >= 1,
    },
    {
        "code": "streak_3",
        "title": "Три дня подряд",
        "desc": "Забрать бонус три дня подряд",
        "icon": "🔥",
        "needs": lambda s: s["streak_days"] >= 3,
    },
    {
        "code": "rich",
        "title": "Богач",
        "desc": "Накопить 5 000 монет",
        "icon": "💎",
        "needs": lambda s: s["balance"] >= 5000,
    },
)

BY_CODE: Final[dict[str, dict[str, Any]]] = {item["code"]: item for item in ACHIEVEMENTS}


def _condition(item: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    return item["needs"]


async def sync(db: Database, user_id: int) -> list[dict[str, Any]]:
    """Открывает всё, что игрок уже заслужил. Возвращает только новые достижения."""
    stats = await db.achievement_stats(user_id)
    known = {str(row["code"]) for row in await db.unlocked_achievements(user_id)}
    pending = [
        item["code"]
        for item in ACHIEVEMENTS
        if item["code"] not in known and _condition(item)(_stats_of(stats))
    ]
    fresh = await db.unlock_achievements(user_id, pending)
    return [BY_CODE[code] for code in fresh if code in BY_CODE]


async def payload(db: Database, user_id: int) -> dict[str, Any]:
    """Список для профиля: что открыто, что ещё нет."""
    unlocked = {
        str(row["code"]): int(row["unlocked_at"]) for row in await db.unlocked_achievements(user_id)
    }
    items = [
        {
            "code": item["code"],
            "title": item["title"],
            "desc": item["desc"],
            "icon": item["icon"],
            "unlocked": item["code"] in unlocked,
            "at": unlocked.get(item["code"]),
        }
        for item in ACHIEVEMENTS
    ]
    return {"items": items, "unlocked": len(unlocked), "total": len(items)}


def public(item: dict[str, Any]) -> dict[str, Any]:
    """Достижение без служебного условия — только то, что показываем игроку."""
    return {key: item[key] for key in ("code", "title", "desc", "icon")}


__all__ = ["ACHIEVEMENTS", "BY_CODE", "payload", "public", "sync"]

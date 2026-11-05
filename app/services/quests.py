"""Задания дня: три штуки в сутки, автопрогресс и награда монетами.

Зачем: у игрока появляется причина зайти, даже если в линии нет его команды.
Задания меняются по кругу — сегодня «поставь и крутни», завтра «собери экспресс».
"""

from __future__ import annotations

import time
from typing import Any, Final

QUESTS: Final[tuple[dict[str, Any], ...]] = (
    {"code": "bet", "title": "Сделай любую ставку", "target": 1, "reward": 100},
    {"code": "casino", "title": "Сыграй раунд в рулетку или кости", "target": 1, "reward": 100},
    {"code": "express", "title": "Собери экспресс из двух исходов", "target": 1, "reward": 200},
    {"code": "bonus", "title": "Забери ежедневный бонус или крутни колесо", "target": 1, "reward": 150},
)

DAILY_COUNT: Final = 3


def day_number(now: float | None = None) -> int:
    """Номер суток — общий для заданий, колеса и серии дней."""
    return int((now if now is not None else time.time()) // 86400)


def quests_of_day(day: int) -> list[dict[str, Any]]:
    """Три задания из четырёх: набор сдвигается на один каждый день."""
    start = day % len(QUESTS)
    return [dict(QUESTS[(start + index) % len(QUESTS)]) for index in range(DAILY_COUNT)]


def by_code(day: int, code: str) -> dict[str, Any] | None:
    return next((quest for quest in quests_of_day(day) if quest["code"] == code), None)


def payload(rows: list[Any], day: int) -> list[dict[str, Any]]:
    """Задания для фронта: прогресс, награда, можно ли забрать."""
    descriptions = {quest["code"]: quest for quest in quests_of_day(day)}
    result: list[dict[str, Any]] = []
    for row in rows:
        code = str(row["code"])
        meta = descriptions.get(code, {})
        progress = int(row["progress"])
        target = int(row["target"])
        result.append(
            {
                "code": code,
                "title": meta.get("title", code),
                "progress": progress,
                "target": target,
                "reward": int(row["reward"]),
                "claimed": row["claimed_at"] is not None,
                "ready": progress >= target and row["claimed_at"] is None,
            }
        )
    return result

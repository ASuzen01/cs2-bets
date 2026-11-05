"""Ручные результаты матчей: когда линия результата не отдала, а матч сыгран.

Fonbet убирает сыгранное событие из линии и счёт после этого не отдаёт. Если счёт
мы не успели поймать в live, результат берётся из файла `data/manual_results.json`:

    [
      {"teams": ["Team Liquid", "TYLOO"], "score": "2:0", "winner": "a",
       "note": "IEM Beijing 2026, нижняя сетка"}
    ]

`winner` — `a` (первая команда) или `b` (вторая). Матч ищется по обеим командам
(порядок не важен), поэтому id из базы вписывать не нужно.

Важно: сыгранный матч НИКОГДА не отменяется с возвратом ставок. Если он уже был
отменён раньше, файл его пересчитает по факту: вернёт возвраты, выплатит выигрыш
и разошлёт уведомления. Обработка идемпотентна: рассчитанный матч повторно не
трогается, так что файл может лежать в репозитории сколько угодно.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiogram import Bot

from app.db import BettingError, Database
from app.services.maps import finish_maps_of_match
from app.services.notify import notify_bettors

log = logging.getLogger(__name__)

RESULTS_PATH = Path("data/manual_results.json")


def load_results(path: Path | str = RESULTS_PATH) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("Не смог прочитать %s", path)
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


async def _find_match(db: Database, item: dict[str, Any]) -> Any | None:
    if item.get("match_id"):
        return await db.get_match(int(item["match_id"]))

    teams = [str(team).strip().lower() for team in item.get("teams") or []]
    if len(teams) != 2:
        return None
    rows = await db._fetchall(
        "SELECT * FROM matches WHERE lower(team_a) IN (?, ?) AND lower(team_b) IN (?, ?) "
        "ORDER BY start_time DESC LIMIT 1",
        (teams[0], teams[1], teams[0], teams[1]),
    )
    return rows[0] if rows else None


async def apply_manual_results(
    db: Database, bot: Bot | None = None, path: Path | str = RESULTS_PATH
) -> list[str]:
    """Применяет ручные результаты. Возвращает список строк — что сделали (для лога)."""
    done: list[str] = []
    for item in load_results(path):
        winner = str(item.get("winner") or "").lower()
        if winner not in ("a", "b"):
            log.warning("Ручной результат без победителя: %s", item)
            continue

        match = await _find_match(db, item)
        if match is None:
            log.warning("Ручной результат: матч не найден (%s)", item.get("teams") or item.get("match_id"))
            continue

        status = str(match["status"])
        if status == "finished" and str(match["winner"] or "") == winner:
            continue  # уже рассчитан именно так — ничего не делаем
        if status == "finished":
            log.warning("Матч #%s уже рассчитан иначе — ручной результат пропущен", match["id"])
            continue

        score = str(item.get("score") or "")
        score_a = score_b = None
        if ":" in score:
            left, right = score.split(":", 1)
            if left.strip().isdigit() and right.strip().isdigit():
                score_a, score_b = int(left), int(right)

        try:
            if status == "cancelled":
                result = await db.recount_match(
                    int(match["id"]), winner, allow_express_legs=True
                )
            else:
                result = await db.settle_match(int(match["id"]), winner, source="manual")
        except BettingError as exc:
            log.warning("Ручной результат для матча #%s не применился: %s", match["id"], exc)
            continue

        if score_a is not None and score_b is not None:
            await db.conn.execute(
                "UPDATE matches SET score_a = ?, score_b = ? WHERE id = ?",
                (score_a, score_b, int(match["id"])),
            )
            await db.conn.commit()

        if bot is not None:
            await notify_bettors(bot, db, result, score=score or None)

        # карты серии: доигранные считаем по счёту, неначатые возвращаем
        await finish_maps_of_match(db, bot, int(match["id"]), source="manual")

        line = (
            f"#{match['id']} {match['team_a']} — {match['team_b']}: "
            f"{result['team']} ({score or 'без счёта'}), выплачено {result['paid']}"
        )
        done.append(line)
        log.info("Ручной результат применён: %s", line)
    return done


__all__ = ["RESULTS_PATH", "apply_manual_results", "load_results"]

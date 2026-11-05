"""Расчёт ставок на карты: доигранная карта считается, неначатая — возвращается.

Порядок один и тот же везде (синк, `/cleanup`, ручные результаты):

* по счёту раундов видно победителя — карта рассчитывается, ставки на неё играют;
* счёта нет вообще (карту так и не начали, серия кончилась раньше) — возврат;
* счёт есть, но карта не доиграна (например, линия оборвалась на 9:3) — карта
  остаётся в «ждём результат», ставки в игре и ждут ручного подтверждения `/maps`.

Возврат «матч не сыгран» на сыгранную карту — ошибка: именно из-за неё ставки на
карты пропадали, когда серия закрывалась.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot

from app.db.database import Database
from app.services.cs2_rules import map_score_text, map_winner
from app.services.notify import notify_map_bettors

log = logging.getLogger(__name__)

OPEN_MAP_STATUSES = ("open", "live", "awaiting")


async def settle_map_by_score(
    db: Database, bot: Bot | None, card: Any, *, source: str = "fonbet"
) -> dict[str, Any] | None:
    """Рассчитывает карту, если по счёту раундов уже видно победителя."""
    score_a, score_b = card["score_a"], card["score_b"]
    winner = map_winner(score_a, score_b)
    if winner is None:
        return None
    try:
        result = await db.settle_map(int(card["id"]), winner, source=source)
    except Exception:
        log.exception("Не смог рассчитать карту #%s", card["id"])
        return None
    if bot is not None:
        await notify_map_bettors(
            bot, db, result, score=map_score_text(score_a, score_b)
        )
    log.info(
        "Карта #%s (%s) рассчитана: %s, счёт %s",
        card["id"],
        f"матч #{card['match_id']}",
        result["team"],
        map_score_text(score_a, score_b),
    )
    return result


async def open_maps_of_match(db: Database, match_id: int) -> list[Any]:
    return [
        card
        for card in await db.match_maps(match_id)
        if str(card["status"]) in OPEN_MAP_STATUSES
    ]


async def finish_maps_of_match(
    db: Database, bot: Bot | None, match_id: int, *, source: str = "maps"
) -> dict[str, int]:
    """Серия закончилась: разбираем все её карты — считаем по счёту или возвращаем.

    Возврат «карта не игралась» получают только карты, на которых не было ни одного
    раунда. Карта со счётом, но без явного победителя остаётся ждать `/maps`.
    """
    done = {"settled": 0, "refunded": 0, "waiting": 0}
    for card in await open_maps_of_match(db, match_id):
        settled = await settle_map_by_score(db, bot, card, source=source)
        if settled is not None:
            done["settled"] += 1
            continue

        score_a, score_b = card["score_a"], card["score_b"]
        if score_a is None and score_b is None:
            try:
                result = await db.cancel_map(int(card["id"]))
            except Exception:
                log.exception("Не смог вернуть ставки по карте #%s", card["id"])
                continue
            done["refunded"] += 1
            if bot is not None and result["bets"]:
                await notify_map_bettors(bot, db, result, void=True)
            log.info("Карта #%s не игралась — ставки возвращены", card["id"])
            continue

        # раунды играли, а победителя по счёту не видно — оставляем на ручной ввод
        await db.set_map_status(int(card["id"]), "awaiting")
        done["waiting"] += 1
        log.warning(
            "Карта #%s (матч #%s) со счётом %s не рассчиталась — жду /maps",
            card["id"],
            match_id,
            map_score_text(score_a, score_b),
        )
    return done


__all__ = [
    "OPEN_MAP_STATUSES",
    "finish_maps_of_match",
    "open_maps_of_match",
    "settle_map_by_score",
]

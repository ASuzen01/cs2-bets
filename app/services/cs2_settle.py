"""Расчёт CS2-серии по внешнему фиду результатов: матч и его карты.

Fonbet не отдаёт ни счёт серии, ни счёт карт после того, как событие ушло из
линии. Поэтому, если локально серию закрыть нечем (карты без счёта), идём в
`app/services/cs2_results.py` и берём оттуда:

* победителя серии и счёт по картам (2:1, 2:0 …);
* результат каждой карты (кто взял и с каким счётом по раундам) — этого хватает,
  чтобы рассчитать и ставки на карты, которые до этого висели в «ждём».

Матч и карты считаются теми же функциями, что и везде: `settle_match`,
`resettle_match` (для уже посчитанных) и `settle_map`.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot

from app.db.database import Database
from app.services.cs2_results import Cs2Match, Cs2ResultsClient, same_cs2_team
from app.services.maps import finish_maps_of_match
from app.services.notify import notify_bettors, notify_map_bettors

log = logging.getLogger(__name__)


def _side_of(team_a: str, team_b: str, name: str) -> str | None:
    """К какой стороне нашей карточки относится команда из фида."""
    if same_cs2_team(team_a, name):
        return "a"
    if same_cs2_team(team_b, name):
        return "b"
    return None


async def settle_from_feed(
    db: Database,
    bot: Bot | None,
    row: Any,
    client: Cs2ResultsClient,
    *,
    source: str = "feed",
) -> dict[str, Any] | None:
    """Пытается закрыть серию по фиду. None — фид её не знает."""
    team_a, team_b = str(row["team_a"]), str(row["team_b"])
    match: Cs2Match | None = await client.find(team_a, team_b, int(row["start_time"]))
    if match is None:
        return None

    # переворачиваем счёт, если в фиде команды стоят наоборот
    swapped = _side_of(team_a, team_b, match.team_b) == "b" and _side_of(team_a, team_b, match.team_a) != "a"
    score_a, score_b = (match.score_b, match.score_a) if swapped else (match.score_a, match.score_b)
    winner = "a" if score_a > score_b else "b"

    maps_settled = await _settle_maps_from_feed(db, bot, row, match, source=source)

    status = str(row["status"])
    stored_winner = str(row["winner"] or "")
    if status == "finished" and stored_winner == winner:
        return {
            "match_id": int(row["id"]),
            "settled": False,
            "maps_settled": maps_settled,
            "winner": winner,
            "score": f"{score_a}:{score_b}",
            "team": team_a if winner == "a" else team_b,
            "paid": 0,
        }

    if status in ("finished", "cancelled"):
        result = await db.resettle_match(int(row["id"]), winner, allow_express_legs=True, source=source)
    else:
        result = await db.settle_match(int(row["id"]), winner, source=source)

    # счёт серии — в карточку
    await db.conn.execute(
        "UPDATE matches SET score_a = ?, score_b = ? WHERE id = ?",
        (score_a, score_b, int(row["id"])),
    )
    await db.conn.commit()

    if bot is not None:
        await notify_bettors(bot, db, result, score=f"{score_a}:{score_b}")

    # карты, которых нет в фиде, но которые точно не игрались — возвращаем
    await finish_maps_of_match(db, bot, int(row["id"]), source=source)

    log.info(
        "CS2 #%s рассчитан по фиду: %s — %s (%s:%s), карт %s",
        row["id"],
        team_a,
        team_b,
        score_a,
        score_b,
        maps_settled,
    )
    result["settled"] = True
    result["maps_settled"] = maps_settled
    result["score"] = f"{score_a}:{score_b}"
    result["team"] = team_a if winner == "a" else team_b
    return result


async def _settle_maps_from_feed(
    db: Database, bot: Bot | None, row: Any, match: Cs2Match, *, source: str
) -> int:
    """Раскладываем ставки на карты по данным фида."""
    if not match.games:
        return 0
    team_a, team_b = str(row["team_a"]), str(row["team_b"])
    settled = 0
    for card in await db.match_maps(int(row["id"])):
        if str(card["status"]) not in ("open", "live", "awaiting"):
            continue
        game = next((g for g in match.games if g.number == int(card["map_number"])), None)
        if game is None:
            continue
        winner = _side_of(team_a, team_b, game.winner)
        if winner is None:
            continue
        score_a, score_b = (
            (game.winner_score, game.loser_score) if winner == "a" else (game.loser_score, game.winner_score)
        )
        await db.set_map_score(int(card["id"]), score_a, score_b)
        try:
            result = await db.settle_map(int(card["id"]), winner, source=source)
        except Exception:
            log.exception("Не смог рассчитать карту #%s по фиду", card["id"])
            continue
        settled += 1
        if bot is not None:
            await notify_map_bettors(bot, db, result, score=f"{score_a}:{score_b}")
    return settled


__all__ = ["settle_from_feed"]

"""Перепроверка расчётов за последние часы: футбол — по фиду, CS2 — по картам.

Зачем: расчёт может ошибиться (посчитали по live-счёту до гола на 90+4, карта
закрылась возвратом, хотя её доиграли, серию закрыли не по тому победителю).
Здесь мы берём свежие матчи и сверяем каждый с фактами:

* футбол — финальный счёт из фида результатов (`app/services/results.py`);
* CS2 — счёт по картам (карта до 13 раундов, овертайм, серия Bo1/Bo3/Bo5).

Если факт расходится с тем, что у нас в базе, матч пересчитывается: прошлые
выплаты/возвраты откатываются, ставки считаются заново, игроки получают
уведомления. Если факта нет — матч не трогаем.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aiogram import Bot

from app.config import settings
from app.db.database import Database
from app.services.cs2_rules import map_winner, required_maps
from app.services.maps import finish_maps_of_match, repair_refunded_maps
from app.services.notify import notify_bettors, send_user
from app.services.results import FootballResult, ResultsClient

log = logging.getLogger(__name__)

# Какие статусы матча имеет смысл перепроверять.
CHECK_STATUSES = ("open", "live", "awaiting", "finished", "cancelled")

# Последний отчёт: его же показывает /health, чтобы результат перепроверки был виден
# без захода в бота.
LAST_REPORT: dict[str, Any] = {}


async def recheck_recent(
    db: Database,
    bot: Bot | None,
    results: ResultsClient | None = None,
    *,
    hours: int = 48,
) -> dict[str, Any]:
    """Сверяет расчёты последних `hours` часов с фактами. Возвращает отчёт."""
    client = results or ResultsClient()
    since = int(time.time()) - hours * 3600
    report: dict[str, Any] = {
        "hours": hours,
        "checked": 0,
        "football": [],
        "cs2": [],
        "maps_repaired": 0,
        "maps_settled": 0,
    }

    try:
        maps = await repair_refunded_maps(db, bot)
        report["maps_repaired"] = int(maps["settled"])
    except Exception:
        log.exception("Перепроверка: не смог починить возвращённые карты")

    for row in await db.recent_matches(limit=1000):
        if int(row["start_time"] or 0) < since:
            continue
        if str(row["status"]) not in CHECK_STATUSES:
            continue
        report["checked"] += 1
        try:
            if str(row["sport"]) == "football":
                line = await _recheck_football(db, bot, client, row)
                if line:
                    report["football"].append(line)
            else:
                line, maps_done = await _recheck_cs2(db, bot, row)
                report["maps_settled"] += maps_done
                if line:
                    report["cs2"].append(line)
        except Exception:
            log.exception("Перепроверка матча #%s упала", row["id"])

    if results is None:
        await client.close()
    LAST_REPORT.clear()
    LAST_REPORT.update(report)
    log.info(
        "Перепроверка за %s ч: матчей %s, футбол исправлен %s, CS2 исправлен %s, карт %s",
        hours, report["checked"], len(report["football"]), len(report["cs2"]),
        report["maps_settled"],
    )
    return report


async def _recheck_football(
    db: Database, bot: Bot | None, client: ResultsClient, row: Any
) -> str | None:
    """Сверяет футбольный матч с фидом результатов."""
    found: FootballResult | None = await client.find(
        str(row["team_a"]),
        str(row["team_b"]),
        int(row["start_time"]),
        english_a=row["team_a_en"] if "team_a_en" in row.keys() else None,  # noqa: SIM118
        english_b=row["team_b_en"] if "team_b_en" in row.keys() else None,  # noqa: SIM118
    )
    if found is None:
        return None
    if found.start_ts and abs(found.start_ts - int(row["start_time"])) > 6 * 3600:
        # нашли однофамильцев (дубль, юноши, другая дата) — так не рискуем
        log.warning(
            "Перепроверка: результат «%s — %s» не похож на матч #%s (время старта)",
            found.home, found.away, row["id"],
        )
        return None

    title = f"{row['team_a']} — {row['team_b']}"
    status = str(row["status"])

    if found.void:
        if status == "cancelled":
            return None
        result = await db.cancel_football_match(int(row["id"]), reason=f"статус {found.status}")
        if bot is not None:
            await notify_bettors(bot, db, result, void=True)
        return f"{title}: не состоялся ({found.status}) — ставки возвращены"

    if not found.finished:
        return None

    true_score = (found.score_home, found.score_away)
    stored = (row["score_a"], row["score_b"])
    if status == "finished" and stored == true_score:
        return None

    if status in ("finished", "cancelled"):
        result = await db.resettle_football(int(row["id"]), *true_score, source="recheck")
    else:
        result = await db.settle_football(int(row["id"]), *true_score, source="recheck")
    if bot is not None:
        await notify_bettors(bot, db, result, score=found.score_text)

    was = f"{stored[0]}:{stored[1]}" if stored[0] is not None else "без счёта"
    return (
        f"{title}: было {was} ({status}), по факту {found.score_text} — "
        f"пересчитано, выплачено {result['paid']}"
    )


async def _recheck_cs2(db: Database, bot: Bot | None, row: Any) -> tuple[str | None, int]:
    """Сверяет CS2-серию со счётом по картам."""
    cards = await db.match_maps(int(row["id"]))
    if not cards:
        return None, 0

    wins = {"a": 0, "b": 0}
    for card in cards:
        winner = card["winner"] if str(card["status"]) == "finished" else None
        if winner is None:
            winner = map_winner(card["score_a"], card["score_b"])
        if winner in ("a", "b"):
            wins[str(winner)] += 1

    need = required_maps(str(row["tournament"]))
    if wins["a"] < need and wins["b"] < need:
        return None, 0                 # серию по картам не закрыть — не гадаем

    winner = "a" if wins["a"] >= need else "b"
    series = f"{wins['a']}:{wins['b']}"
    status = str(row["status"])
    title = f"{row['team_a']} — {row['team_b']}"

    # карты серии: доигранные считаем по счёту, неначатые возвращаем
    settled_maps = 0
    try:
        done = await finish_maps_of_match(db, bot, int(row["id"]), source="recheck")
        settled_maps = int(done["settled"])
    except Exception:
        log.exception("Перепроверка: карты матча #%s не закрылись", row["id"])

    if status == "finished" and str(row["winner"] or "") == winner:
        return None, settled_maps

    if status in ("finished", "cancelled"):
        result = await db.resettle_match(
            int(row["id"]), winner, allow_express_legs=True, source="recheck"
        )
    else:
        result = await db.settle_match(int(row["id"]), winner, source="recheck")
    if bot is not None:
        await notify_bettors(bot, db, result, score=series)

    was = str(row["winner"] or status)
    return (
        f"{title}: было «{was}», по картам {series} — победил {result['team']}, "
        f"выплачено {result['paid']}, карт рассчитано {settled_maps}"
    ), settled_maps


def report_text(report: dict[str, Any]) -> str:
    """Короткий отчёт для админа."""
    lines = [f"<b>Перепроверка за {report['hours']} ч</b>"]
    lines.append(f"Проверено матчей: {report['checked']}")
    corrected = len(report["football"]) + len(report["cs2"])
    if not corrected and not report["maps_settled"] and not report["maps_repaired"]:
        lines.append("Расхождения не найдены — всё посчитано верно ✅")
        return "\n".join(lines)

    if report["football"]:
        lines.append("\n<b>Футбол:</b>")
        lines.extend(f"• {line}" for line in report["football"])
    if report["cs2"]:
        lines.append("\n<b>CS2:</b>")
        lines.extend(f"• {line}" for line in report["cs2"])
    if report["maps_settled"] or report["maps_repaired"]:
        lines.append(
            f"\nКарт рассчитано: {report['maps_settled']}, "
            f"откатано ошибочных возвратов по картам: {report['maps_repaired']}"
        )
    return "\n".join(lines)


async def notify_admins(bot: Bot, db: Database, text: str) -> None:
    for admin_id in settings.admin_ids_list:
        await send_user(bot, db, int(admin_id), text)


__all__ = ["LAST_REPORT", "notify_admins", "recheck_recent", "report_text"]

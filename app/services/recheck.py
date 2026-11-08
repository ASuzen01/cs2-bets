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

import asyncio
import contextlib
import logging
import time
from typing import Any

from aiogram import Bot

from app.config import settings
from app.db.database import Database
from app.services.cs2_results import Cs2ResultsClient
from app.services.cs2_rules import map_winner, required_maps
from app.services.cs2_settle import settle_from_feed
from app.services.flashscore_football_results import FlashscoreFootballResultsClient
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
    cs2_results: Cs2ResultsClient | None = None,
    flashscore_results: FlashscoreFootballResultsClient | None = None,
) -> dict[str, Any]:
    """Сверяет расчёты последних `hours` часов с фактами. Возвращает отчёт."""
    client = results or ResultsClient()
    # При переданном в тест/вызов primary-клиенте не обращаемся к сети неожиданно.
    # В штатном production-вызове без инъекций включаем Flashscore автоматически.
    own_flashscore = flashscore_results is None and results is None
    flashscore = flashscore_results or (
        FlashscoreFootballResultsClient() if own_flashscore else None
    )
    cs2 = cs2_results or Cs2ResultsClient()
    since = int(time.time()) - hours * 3600
    report: dict[str, Any] = {
        "hours": hours,
        "checked": 0,
        "football": [],
        "cs2": [],
        "maps_repaired": 0,
        "maps_settled": 0,
        "unmatched": 0,
        "errors": 0,
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
                line, matched = await _recheck_football(db, bot, client, row, flashscore)
                if matched is False:
                    report["unmatched"] += 1
                if line:
                    report["football"].append(line)
            else:
                line, maps_done = await _recheck_cs2(db, bot, row, cs2)
                report["maps_settled"] += maps_done
                if line:
                    report["cs2"].append(line)
        except Exception:
            report["errors"] += 1
            log.exception("Перепроверка матча #%s упала", row["id"])

    if results is None:
        await client.close()
    if own_flashscore and flashscore is not None:
        await flashscore.close()
    if cs2_results is None:
        await cs2.close()
    LAST_REPORT.clear()
    LAST_REPORT.update(report)
    log.info(
        "Перепроверка за %s ч: матчей %s, футбол исправлен %s, CS2 исправлен %s, карт %s",
        hours, report["checked"], len(report["football"]), len(report["cs2"]),
        report["maps_settled"],
    )
    return report


async def _recheck_football(
    db: Database,
    bot: Bot | None,
    client: ResultsClient,
    row: Any,
    flashscore: FlashscoreFootballResultsClient | None = None,
) -> tuple[str | None, bool | None]:
    """Сверяет футбольный матч с фидом результатов.

    Возвращает (строка отчёта, найден ли результат в фиде).
    """
    names = {
        "english_a": row["team_a_en"] if "team_a_en" in row.keys() else None,  # noqa: SIM118
        "english_b": row["team_b_en"] if "team_b_en" in row.keys() else None,  # noqa: SIM118
    }
    teams = (str(row["team_a"]), str(row["team_b"]), int(row["start_time"]))
    found: FootballResult | None = await client.find(*teams, **names)
    if found is None and flashscore is not None:
        found = await flashscore.find(*teams, **names)
    if found is None:
        return None, False
    if found.start_ts and abs(found.start_ts - int(row["start_time"])) > 6 * 3600:
        # нашли однофамильцев (дубль, юноши, другая дата) — так не рискуем
        log.warning(
            "Перепроверка: результат «%s — %s» не похож на матч #%s (время старта)",
            found.home, found.away, row["id"],
        )
        return None, False

    title = f"{row['team_a']} — {row['team_b']}"
    status = str(row["status"])

    if found.void:
        if status == "cancelled":
            return None, True
        result = await db.cancel_football_match(int(row["id"]), reason=f"статус {found.status}")
        if bot is not None:
            await notify_bettors(bot, db, result, void=True)
        return f"{title}: не состоялся ({found.status}) — ставки возвращены", True

    if not found.finished:
        return None, True

    true_score = (found.score_home, found.score_away)
    stored = (row["score_a"], row["score_b"])
    if status == "finished" and stored == true_score:
        return None, True

    if status in ("finished", "cancelled"):
        result = await db.resettle_football(int(row["id"]), *true_score, source="recheck")
    else:
        result = await db.settle_football(int(row["id"]), *true_score, source="recheck")
    if bot is not None:
        await notify_bettors(bot, db, result, score=found.score_text)

    was = f"{stored[0]}:{stored[1]}" if stored[0] is not None else "без счёта"
    return (
        f"{title}: было {was} ({status}), по факту {found.score_text} — "
        f"пересчитано, выплачено {result['paid']}",
        True,
    )


async def _recheck_cs2(
    db: Database, bot: Bot | None, row: Any, cs2: Cs2ResultsClient | None = None
) -> tuple[str | None, int]:
    """Сверяет CS2-серию с фактами: внешний фид, затем счёт по картам.

    Фид (bo3.gg) знает и победителя серии, и счёт каждой карты, поэтому он
    первый источник: Fonbet после ухода события из линии счёт не отдаёт и серия
    висела бы в «ждём результат» вечно.
    """
    if cs2 is not None:
        try:
            fed = await settle_from_feed(db, bot, row, cs2, source="recheck")
        except Exception:
            log.exception("Перепроверка: фид CS2 не закрыл матч #%s", row["id"])
            fed = None
        if fed is not None:
            if not fed.get("settled") and not fed.get("maps_settled"):
                return None, 0
            title = f"{row['team_a']} — {row['team_b']}"
            was = str(row["winner"] or row["status"])
            return (
                f"{title}: было «{was}», по фиду {fed['score']} — победил {fed['team']}, "
                f"выплачено {fed['paid']}, карт рассчитано {fed['maps_settled']}"
            ), int(fed["maps_settled"])

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
    if report.get("errors"):
        lines.append(f"Сбоев при проверке: {report['errors']} (подробности в логах)")
    if report.get("unmatched"):
        lines.append(
            f"Не нашлось в фиде результатов: {report['unmatched']} "
            "(такие матчи не трогаем)"
        )
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


class RecheckService:
    """Перепроверка расчётов по расписанию.

    Правила уведомлений (чтобы админам не приходил одинаковый «всё ок» каждый час):

    * нашли расхождение — пересчитали матч или карту — пишем сразу, каждая проверка;
    * проверка упала — тоже пишем сразу;
    * всё чисто — одна сводка в сутки (`RECHECK_OK_DIGEST_HOURS`), отметка о ней
      лежит в базе, поэтому перезапуски сервиса её не сбрасывают.
    """

    META_KEY = "recheck_ok_notified_at"

    def __init__(
        self,
        bot: Bot | None,
        db: Database,
        *,
        hours: int | None = None,
        interval_minutes: int | None = None,
        digest_hours: int | None = None,
        notifier: Any = None,
        runner: Any = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.hours = int(hours if hours is not None else settings.recheck_hours)
        self.interval = max(1, int(
            interval_minutes if interval_minutes is not None else settings.recheck_interval_minutes
        ))
        self.digest_hours = int(
            digest_hours if digest_hours is not None else settings.recheck_ok_digest_hours
        )
        self._notifier = notifier
        self.runner = runner or recheck_recent     # подменяется в тестах
        self._task: asyncio.Task[None] | None = None
        self.runs = 0
        self.last_at: int = 0
        self.last_problems = False
        self.last_error: str | None = None
        self.last_notified_at: int = 0

    # ------------------------------------------------------------ lifecycle
    @property
    def stats(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "interval_minutes": self.interval,
            "hours": self.hours,
            "digest_hours": self.digest_hours,
            "last_at": self.last_at or None,
            "last_problems": self.last_problems,
            "last_notified_at": self.last_notified_at or None,
            "last_error": self.last_error,
        }

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def start(self) -> None:
        if not self.enabled:
            log.info("Перепроверка по расписанию выключена (RECHECK_INTERVAL_MINUTES=0)")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="recheck")
            log.info("Перепроверка по расписанию: раз в %s мин", self.interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # первую проверку делает on_startup (run_once), поэтому здесь сразу спим:
        # иначе на старте расчёт сверялся бы дважды подряд
        while True:
            await asyncio.sleep(self.interval * 60)
            try:
                await self.run_once()
            except Exception:
                self.last_error = "проверка упала"
                log.exception("Плановая перепроверка не удалась")

    # ---------------------------------------------------------------- работа
    async def run_once(self) -> dict[str, Any]:
        """Одна проверка: считаем, уведомляем по правилам, возвращаем отчёт."""
        report = await self.runner(self.db, self.bot, hours=self.hours)
        self.runs += 1
        self.last_at = int(time.time())
        self.last_error = None
        self.last_problems = has_problems(report)
        await self._notify(report)
        return report

    async def _notify(self, report: dict[str, Any]) -> None:
        """Расхождения — сразу; «всё ок» — не чаще раза в `digest_hours` часов."""
        now = int(time.time())
        if has_problems(report):
            await self._send(report, digest=False)
            return

        last_ok = await self.db.get_meta_int(self.META_KEY)
        if last_ok and now - last_ok < self.digest_hours * 3600:
            log.info("Перепроверка чистая, но сводку уже отправляли — молчим")
            return
        await self._send(report, digest=True)
        await self.db.set_meta(self.META_KEY, str(now))

    async def _send(self, report: dict[str, Any], *, digest: bool) -> None:
        text = report_text(report)
        if digest:
            text += (
                f"\n\nПроверки идут каждые {self.interval} мин — это ежедневная сводка, "
                "при расхождениях напишем сразу."
            )
        if self._notifier is not None:
            await self._notifier(text)
        elif self.bot is not None:
            await notify_admins(self.bot, self.db, text)
        self.last_notified_at = int(time.time())


def has_problems(report: dict[str, Any]) -> bool:
    """Есть ли в отчёте что-то, о чём надо написать сразу."""
    return bool(
        report.get("football")
        or report.get("cs2")
        or report.get("maps_settled")
        or report.get("maps_repaired")
        or report.get("errors")
    )


__all__ = [
    "LAST_REPORT",
    "RecheckService",
    "has_problems",
    "notify_admins",
    "recheck_recent",
    "report_text",
]

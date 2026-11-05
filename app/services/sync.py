"""Фоновый синк линии Fonbet: матчи, кэфы, закрытие приёма ставок, расчёт результата."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app import keyboards, texts
from app.config import settings
from app.db import Database
from app.services.cs2_results import Cs2ResultsClient
from app.services.cs2_rules import map_winner, required_maps
from app.services.cs2_settle import settle_from_feed
from app.services.fonbet import FonbetClient, FonbetMap, FonbetMatch, parse_maps
from app.services.football import FootballMatch, is_placeholder_team, parse_football
from app.services.hockey import HockeyMatch, parse_hockey
from app.services.maps import finish_maps_of_match, settle_map_by_score
from app.services.notify import notify_bettors
from app.services.results import FootballResult, ResultsClient

log = logging.getLogger(__name__)

# Футбольный матч с перерывом и добавленным временем заканчивается за ~100 минут.
# Fonbet выбрасывает завершённые события из линии и финальный счёт после этого
# не отдаёт (/events/event возвращает пустоту), поэтому финалом считаем последний
# счёт, который видели в live, — но только если событие пропало и минуту не
# появляется снова (пять минут тишины).
FOOTBALL_FINISH_MINUTES = 100
FOOTBALL_GONE_QUIET_SECONDS = 300
# Как часто переспрашивать фид результатов CS2 по одному и тому же матчу.
CS2_FEED_RECHECK_SECONDS = 600
# Как часто переспрашивать фид результатов по матчу, который уже ушёл из линии.
FOOTBALL_RESULT_RETRY_SECONDS = 600
# Как часто обновлять английские названия команд (для поиска результатов).
FOOTBALL_NAMES_TTL_SECONDS = 1800
# Сколько тиков подряд матч должен отсутствовать в линии, чтобы мы его сняли.
# Один-два пропуска — это сбой фида; три подряд (около двух минут) — событие убрали.
LINE_GONE_TICKS = 3
# Ниже этого размера линию считаем битой и ничего по ней не снимаем.
LINE_MIN_HEALTHY = {"football": 20, "hockey": 10}
# Хоккейный матч: три периода по 20 минут + перерывы + овертайм — час с небольшим.
# Раньше этого срока фид результатов не спрашиваем.
HOCKEY_FINISH_MINUTES = 100
HOCKEY_RESULT_RETRY_SECONDS = 600


# Сколько матч формата может идти (минуты): пока не истекло, live-приём открыт,
# даже если Fonbet убрал событие из линии и результата мы ещё не знаем.
FORMAT_MINUTES = {1: 90, 2: 170, 3: 260}
# Запас сверх формата: если счёта мы так и не увидели, результат взять негде —
# возвращаем ставки, а не держим матч в «ждём результат» сутками.
FORMAT_GRACE_MINUTES = 5
# Матч уже в «ждём результат», а счёта так и не было: держать его дальше нет смысла.
AWAITING_NO_SCORE_MINUTES = 90


def format_minutes(tournament: str) -> int:
    return FORMAT_MINUTES.get(required_maps(tournament), 170)


def decisive_score(tournament: str, score_a: int | None, score_b: int | None) -> bool:
    """Счёт уже не отыграть: серия выиграна (Bo3 — 2:0/2:1, Bo5 — 3:…)."""
    if score_a is None or score_b is None:
        return False
    need = required_maps(tournament)
    if need == 1:
        return False          # у Bo1 в счёте раунды, а не карты — по нему не считаем
    return score_a >= need or score_b >= need


def _map_winner(score_a: int | None, score_b: int | None) -> str | None:
    """Победитель карты по раундам — см. app/services/cs2_rules."""
    return map_winner(score_a, score_b)


async def series_from_maps(db: Database, row: Any) -> tuple[str | None, str | None]:
    """Победитель серии и счёт по картам — из уже рассчитанных карт."""
    wins = {"a": 0, "b": 0}
    for card in await db.match_maps(int(row["id"])):
        if str(card["status"]) == "finished" and card["winner"] in ("a", "b"):
            wins[str(card["winner"])] += 1

    need = required_maps(str(row["tournament"]))
    score = f"{wins['a']}:{wins['b']}"
    if wins["a"] >= need:
        return "a", score
    if wins["b"] >= need:
        return "b", score
    return None, None



class MatchSyncer:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        client: FonbetClient | None = None,
        results: ResultsClient | None = None,
        cs2_results: Cs2ResultsClient | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.results = results or ResultsClient()
        self.hockey_results = ResultsClient(sport="hockey")
        self.cs2_results = cs2_results or Cs2ResultsClient()
        self._feed_checked: dict[int, int] = {}
        # матч_id → сколько тиков подряд его нет в линии (снимаем на LINE_GONE_TICKS)
        self._line_gone: dict[int, int] = {}
        self._names_en_at = 0
        self.client = client or FonbetClient(
            settings.fonbet_base_url,
            scope_market=settings.fonbet_scope_market,
            include=settings.tier1_include_list,
            exclude=settings.tier1_exclude_list,
        )
        self._task: asyncio.Task[None] | None = None
        self.last_sync_at: int = 0
        self.last_error: str | None = None
        self.last_stats: dict[str, Any] | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="fonbet-sync")
            log.info("Синк линии запущен, период %s сек", settings.sync_interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.client.close()
        await self.results.close()
        await self.cs2_results.close()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # сеть/парсинг — не роняем бота
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Синхронизация линии упала")
            await asyncio.sleep(max(30, settings.sync_interval))

    # ----------------------------------------------------------------- tick
    async def tick(self) -> dict[str, Any]:
        packet = await self.client.fetch_packet()
        matches = self.client.parse(packet)
        maps_by_parent = parse_maps(packet, self.client)
        football = parse_football(
            packet, settings.football_include_list, settings.football_exclude_list
        )
        hockey = (
            parse_hockey(packet, settings.hockey_include_list, settings.hockey_exclude_list)
            if settings.hockey_enabled
            else []
        )
        stats = {
            "fetched": len(matches),
            "created": 0,
            "updated": 0,
            "closed": 0,
            "settled": 0,
            "awaiting": 0,
            "maps_settled": 0,
            "football": len(football),
            "football_created": 0,
            "hockey": len(hockey),
            "hockey_created": 0,
        }

        await self._store(matches, maps_by_parent, stats)
        await self._store_football(football, stats)
        await self._store_hockey(hockey, stats)
        await self._close_started(stats)
        # сначала карты: матч считается в том числе по их результатам
        await self._settle_maps(maps_by_parent, stats)
        await self._settle(matches, stats)
        await self._refresh_english_names()
        await self._settle_football(football, stats)
        await self._settle_hockey(hockey, stats)
        await self._close_stale(stats)

        self.last_sync_at = int(time.time())
        self.last_error = None
        self.last_stats = dict(stats)
        log.info(
            "Линия: %s матчей (новых %s, закрыто %s, рассчитано %s, карт %s, ждут %s)",
            stats["fetched"], stats["created"], stats["closed"], stats["settled"],
            stats["maps_settled"], stats["awaiting"],
        )
        return stats

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _prepare_odds(match: FonbetMatch) -> tuple[float | None, float | None]:
        odds_a, odds_b = match.odds_a, match.odds_b
        if settings.odds_mode == "fair" and odds_a and odds_b:
            margin = 1 / odds_a + 1 / odds_b
            return round(odds_a * margin, 2), round(odds_b * margin, 2)
        return odds_a, odds_b

    @staticmethod
    def _winner_from_score(match: FonbetMatch) -> str | None:
        """Победитель по счёту серии. Раунды отдельной карты (16:14) не принимаем."""
        score_a, score_b = match.score_a, match.score_b
        if score_a is None or score_b is None or score_a == score_b:
            return None
        if max(score_a, score_b) > 3 or (score_a + score_b) > 5:
            return None
        return "a" if score_a > score_b else "b"

    async def _store(
        self,
        matches: list[FonbetMatch],
        maps_by_parent: dict[int, list[FonbetMap]],
        stats: dict[str, Any],
    ) -> None:
        now = int(time.time())
        horizon = now + settings.autopost_horizon_hours * 3600
        chats = await self.db.list_autopost_chats() if settings.autopost_matches else []
        posted = 0

        for match in matches:
            tracked = await self.db.match_by_fonbet(match.event_id)
            old_line = bool(match.start_time) and match.start_time < now - 6 * 3600
            # старую линию не берём, но матч, который уже у нас висит незакрытым,
            # обязаны довести до результата — иначе он уйдёт в отмену
            if old_line and (
                tracked is None or str(tracked["status"]) not in ("open", "live", "awaiting")
            ):
                continue
            # live-матч без кэфов всё равно нужен: у него есть счёт по картам,
            # а кэфы Fonbet иногда снимает по ходу игры — тогда оставляем прошлые
            odds_missing = match.odds_a is None or match.odds_b is None
            if odds_missing and tracked is None:
                continue          # в линии матч без кэфов никому не нужен
            odds_a, odds_b = (None, None) if odds_missing else self._prepare_odds(match)
            row, created = await self.db.upsert_match(
                fonbet_id=match.event_id,
                tournament=match.tournament,
                team_a=match.team_a,
                team_b=match.team_b,
                start_time=match.start_time,
                odds_a=odds_a,
                odds_b=odds_b,
                score_a=match.score_a,   # счёт по картам: нужен для live-табло
                score_b=match.score_b,
            )
            await self._store_maps(row, match.event_id, maps_by_parent)

            if created:
                stats["created"] += 1
                should_post = (
                    not odds_missing
                    and posted < settings.autopost_limit
                    and match.start_time <= horizon
                )
                if should_post and chats:
                    posted += 1
                    await self._post_card(row, chats)
            else:
                stats["updated"] += 1

    async def _store_maps(
        self, row: Any, fonbet_id: int, maps_by_parent: dict[int, list[FonbetMap]]
    ) -> None:
        """Кэфы на победителя каждой карты («1-я карта», «2-я карта»…)."""
        for card in maps_by_parent.get(fonbet_id, []):
            await self.db.upsert_map(
                match_id=int(row["id"]),
                fonbet_id=card.event_id,
                map_number=card.map_number,
                odds_a=card.odds_a,
                odds_b=card.odds_b,
                score_a=card.score_a,
                score_b=card.score_b,
            )

    async def _settle_maps(self, maps_by_parent: dict[int, list[FonbetMap]], stats: dict) -> None:
        """Раскладываем ставки по картам, как только карта доиграна.

        Победителя берём из счёта раундов (13 раундов, овертайм), а не из флага
        `finished`: Fonbet этот флаг почти никогда не присылает, из-за чего карты
        висели нерассчитанными, а потом уходили в возврат вместе с серией.
        """
        by_event: dict[int, FonbetMap] = {}
        for cards in maps_by_parent.values():
            for card in cards:
                by_event[card.event_id] = card

        for row in await self.db.maps_by_status(["open", "live"]):
            card = by_event.get(int(row["fonbet_id"]))
            if card is not None and card.score_a is not None and card.score_b is not None:
                await self.db.upsert_map(
                    match_id=int(row["match_id"]),
                    fonbet_id=card.event_id,
                    map_number=card.map_number,
                    odds_a=card.odds_a,
                    odds_b=card.odds_b,
                    score_a=card.score_a,
                    score_b=card.score_b,
                )

            # счёт берём самый свежий: из линии, а если карта оттуда ушла — из базы
            score_a = card.score_a if card is not None and card.score_a is not None else row["score_a"]
            score_b = card.score_b if card is not None and card.score_b is not None else row["score_b"]
            winner = map_winner(score_a, score_b)
            if winner is None:
                # карта ещё идёт (или её счёт потерялся) — ставки продолжают приниматься
                continue
            if await settle_map_by_score(self.db, self.bot, row):
                stats["maps_settled"] += 1

    # --------------------------------------------------------------- футбол
    @staticmethod
    def _prepare_three_way(
        match: FootballMatch | HockeyMatch,
    ) -> tuple[float | None, float | None, float | None]:
        """Кэфы 1X2 как у книги; в режиме fair — с убранной маржой."""
        if not match.has_result_odds:
            return match.odds_1, match.odds_x, match.odds_2
        odds_1, odds_x, odds_2 = match.odds_1, match.odds_x, match.odds_2
        assert odds_1 and odds_x and odds_2
        if settings.odds_mode == "fair":
            margin = 1 / odds_1 + 1 / odds_x + 1 / odds_2
            return (
                round(odds_1 * margin, 2),
                round(odds_x * margin, 2),
                round(odds_2 * margin, 2),
            )
        return round(odds_1, 2), round(odds_x, 2), round(odds_2, 2)

    async def _store_football(self, matches: list[FootballMatch], stats: dict[str, Any]) -> None:
        """Пишем футбольные матчи в ту же таблицу, что и CS2 (поле sport их различает)."""
        now = int(time.time())
        for match in matches:
            if not match.has_result_odds:
                continue
            if match.start_time and match.start_time < now - 6 * 3600:
                tracked = await self.db.match_by_fonbet(match.event_id)
                if tracked is None or str(tracked["status"]) not in ("open", "live", "awaiting"):
                    continue
            odds_1, odds_x, odds_2 = self._prepare_three_way(match)
            row, created = await self.db.upsert_match(
                fonbet_id=match.event_id,
                tournament=match.tournament,
                team_a=match.team_a,
                team_b=match.team_b,
                start_time=match.start_time,
                odds_a=odds_1,
                odds_x=odds_x,
                odds_b=odds_2,
                score_a=match.score_a,
                score_b=match.score_b,
                sport="football",
            )
            del row
            if created:
                stats["football_created"] += 1

    # --------------------------------------------------------------- хоккей
    async def _store_hockey(self, matches: list[HockeyMatch], stats: dict[str, Any]) -> None:
        """Пишем хоккейные матчи рядом с футболом: sport='hockey' их различает."""
        now = int(time.time())
        for match in matches:
            if not match.has_result_odds:
                continue
            if match.start_time and match.start_time < now - 6 * 3600:
                tracked = await self.db.match_by_fonbet(match.event_id)
                if tracked is None or str(tracked["status"]) not in ("open", "live", "awaiting"):
                    continue
            odds_1, odds_x, odds_2 = self._prepare_three_way(match)
            row, created = await self.db.upsert_match(
                fonbet_id=match.event_id,
                tournament=match.tournament,
                team_a=match.team_a,
                team_b=match.team_b,
                start_time=match.start_time,
                odds_a=odds_1,
                odds_x=odds_x,
                odds_b=odds_2,
                score_a=match.score_a,
                score_b=match.score_b,
                sport="hockey",
            )
            del row
            if created:
                stats["hockey_created"] += 1

    async def _settle_hockey(
        self, matches: list[HockeyMatch], stats: dict[str, Any]
    ) -> None:
        """Считаем хоккей по фиду результатов: НХЛ есть, КХЛ там нет.

        По счёту из линии считать нельзя: Fonbet отдаёт итоговый счёт (с овертаймом),
        а для рынка «Основное время» нужен счёт после трёх периодов — в фиде он есть
        (`Tr1OR`/`Tr2OR`), в линии нет. Поэтому НХЛ закрывается автоматически, а КХЛ
        ждёт ручного расчёта админом — деньги важнее скорости.
        """
        # счёт из линии не используем (см. докстринг), но список событий нужен:
        # по нему видно, какие матчи из линии пропали
        line_ids = {match.event_id for match in matches}
        now = int(time.time())
        for row in await self.db.matches_by_status(["open", "live", "awaiting"]):
            if str(row["sport"]) != "hockey":
                continue
            if await self._drop_from_line_if_gone(
                row, line_ids, len(line_ids), stats, sport="hockey"
            ):
                continue
            if now - int(row["start_time"]) < HOCKEY_FINISH_MINUTES * 60:
                continue

            last_check = int(row["last_result_check"] or 0)
            if last_check and now - last_check < HOCKEY_RESULT_RETRY_SECONDS:
                continue
            await self.db.mark_result_check(int(row["id"]), now)

            result = await self._hockey_result_from_feed(row)
            if result is not None and result.void:
                await self._cancel_market_row(row, stats, reason=f"статус {result.status}")
                continue
            if result is not None and result.finished:
                await self._settle_hockey_row(
                    row, result.score_home, result.score_away,
                    result.final_home, result.final_away, stats,
                )
                continue

            if str(row["status"]) != "awaiting":
                # без кнопок «победила первая/вторая»: у хоккея два счёта,
                # их вводит админ командой /hockey
                await self.db.set_status(int(row["id"]), "awaiting")
                stats["awaiting"] += 1
                text = texts.hockey_awaiting_text(row)
                for chat_id in await self.db.chat_ids_with_bets(int(row["id"])):
                    await self._safe_send(chat_id, text)
                log.info(
                    "Хоккей #%s %s — %s: результата в фиде нет (КХЛ считает админ)",
                    row["id"], row["team_a"], row["team_b"],
                )

    async def _hockey_result_from_feed(self, row: Any) -> FootballResult | None:
        """Счёт из хоккейного фида: reg — основное время, final — с овертаймом."""
        try:
            return await self.hockey_results.find(
                str(row["team_a"]),
                str(row["team_b"]),
                int(row["start_time"]),
                english_a=row["team_a_en"] if "team_a_en" in row.keys() else None,  # noqa: SIM118
                english_b=row["team_b_en"] if "team_b_en" in row.keys() else None,  # noqa: SIM118
            )
        except Exception:
            log.exception("Не смог получить результат хоккейного матча #%s", row["id"])
            return None

    async def _settle_hockey_row(
        self,
        row: Any,
        reg_a: int,
        reg_b: int,
        final_a: int,
        final_b: int,
        stats: dict[str, Any],
    ) -> None:
        """Расплатиться по хоккейному матчу и рассказать об этом в чаты."""
        try:
            result = await self.db.settle_hockey(
                int(row["id"]), reg_a, reg_b, final_a, final_b, source="feed"
            )
        except Exception:
            log.exception("Не смог рассчитать хоккейный матч #%s", row["id"])
            return

        stats["settled"] += 1
        text = texts.hockey_settled_text(
            result, int(row["id"]), f"{reg_a}:{reg_b}", f"{final_a}:{final_b}"
        )
        for chat_id in await self.db.chat_ids_with_bets(int(row["id"])):
            await self._safe_send(chat_id, text)
        await notify_bettors(
            self.bot, self.db, result,
            score=f"{reg_a}:{reg_b} (осн.) · {final_a}:{final_b} (итог)",
        )
        for express in result.get("expresses") or []:
            target = express.get("chat_id") or None
            if target:
                await self._safe_send(int(target), texts.express_result(express))
        log.info(
            "Хоккей #%s рассчитан: %s — %s (основное %s:%s, итог %s:%s), выплачено %s",
            row["id"], row["team_a"], row["team_b"], reg_a, reg_b, final_a, final_b, result["paid"],
        )

    async def _settle_football(
        self, matches: list[FootballMatch], stats: dict[str, Any]
    ) -> None:
        """Считаем футбольные матчи по фактическому финальному счёту.

        Счёт из линии берём, только пока матч там есть и Fonbet сам пометил его
        завершённым. Как только событие пропало, финальный счёт спрашиваем у фида
        результатов: последний live-счёт брать нельзя — гол на 90+4 или в
        добавленное время меняет исход, а мы бы уже расплатились по старому счёту.
        Если результата нигде нет, матч ждёт (`awaiting`) и переспрашивает фид.
        """
        by_fonbet = {match.event_id: match for match in matches}
        now = int(time.time())

        for row in await self.db.matches_by_status(["open", "live", "awaiting"]):
            if str(row["sport"]) != "football":
                continue
            match = by_fonbet.get(int(row["fonbet_id"]))
            if match is None and await self._drop_from_line_if_gone(
                row, by_fonbet.keys(), len(matches), stats, sport="football"
            ):
                continue
            if match is not None:
                if int(row["last_result_check"] or 0):
                    # матч вернулся в линию — отметку сбрасываем
                    await self.db.mark_result_check(int(row["id"]), 0)
                if match.finished and match.score_a is not None and match.score_b is not None:
                    await self._settle_football_row(row, match.score_a, match.score_b, stats)
                continue

            # события нет в линии: либо оно ещё не появилось, либо матч уже сыгран
            if now - int(row["start_time"]) < FOOTBALL_FINISH_MINUTES * 60:
                continue

            last_check = int(row["last_result_check"] or 0)
            if last_check and now - last_check < FOOTBALL_RESULT_RETRY_SECONDS:
                continue          # не долбим фид каждые 45 секунд
            await self.db.mark_result_check(int(row["id"]), now)

            result = await self._football_result_from_feed(row)
            if result is not None and result.void:
                await self._cancel_market_row(row, stats, reason=f"статус {result.status}")
                continue
            if result is not None and result.finished:
                await self._settle_football_row(row, result.score_home, result.score_away, stats)
                continue

            if str(row["status"]) != "awaiting":
                await self._mark_awaiting(row, None)
                stats["awaiting"] += 1
                log.info(
                    "Футбол #%s %s — %s: результата пока нет, ждём фид",
                    row["id"], row["team_a"], row["team_b"],
                )

    async def _football_result_from_feed(self, row: Any) -> FootballResult | None:
        """Финальный счёт из внешнего фида результатов (None — не нашли/не готово)."""
        try:
            return await self.results.find(
                str(row["team_a"]),
                str(row["team_b"]),
                int(row["start_time"]),
                english_a=row["team_a_en"] if "team_a_en" in row.keys() else None,  # noqa: SIM118
                english_b=row["team_b_en"] if "team_b_en" in row.keys() else None,  # noqa: SIM118
            )
        except Exception:
            log.exception("Не смог получить результат футбольного матча #%s", row["id"])
            return None

    async def _refresh_english_names(self) -> None:
        """Подтягиваем английские названия команд — по ним ищем результаты.

        Делаем редко и только когда есть футбольные матчи без английских имён:
        лишний мегабайт линии на каждом тике ни к чему.
        """
        now = int(time.time())
        if now - self._names_en_at < FOOTBALL_NAMES_TTL_SECONDS:
            return
        pending = await self.db.matches_without_names_en("football")
        if settings.hockey_enabled:
            pending = list(pending) + list(await self.db.matches_without_names_en("hockey"))
        if not pending:
            return
        self._names_en_at = now
        try:
            names = await self.client.english_names()
        except Exception:
            log.exception("Не смог получить английские названия команд")
            return
        for row in pending:
            pair = names.get(int(row["fonbet_id"]))
            if pair is None:
                continue
            await self.db.set_match_names_en(int(row["id"]), pair[0], pair[1])

    async def _drop_from_line_if_gone(
        self,
        row: Any,
        line_ids: Any,
        line_size: int,
        stats: dict[str, Any],
        *,
        sport: str,
    ) -> bool:
        """Матч ещё не начался, но пропал из линии — снимаем его с приёма ставок.

        Так из линии уходят события, которые перестали попадать под фильтры
        турниров (например, отборочные матчи) или которые букмекер убрал сам.
        Три пропуска подряд — чтобы сбой одного пакета не снимал живые матчи.
        """
        match_id = int(row["id"])
        if int(row["start_time"]) <= int(time.time()):
            return False                     # матч уже начался: им занимается расчёт
        if int(row["fonbet_id"]) in line_ids:
            self._line_gone.pop(match_id, None)
            return False
        if line_size < LINE_MIN_HEALTHY.get(sport, 10):
            return False                     # линия битая/пустая — ничего не снимаем
        ticks = self._line_gone.get(match_id, 0) + 1
        if ticks < LINE_GONE_TICKS:
            self._line_gone[match_id] = ticks
            return False
        self._line_gone.pop(match_id, None)
        await self._cancel_market_row(row, stats, reason="событие убрано из линии")
        log.info(
            "Матч #%s %s — %s (%s) пропал из линии — снят",
            match_id, row["team_a"], row["team_b"], sport,
        )
        return True

    async def _cancel_market_row(
        self, row: Any, stats: dict[str, Any], *, reason: str
    ) -> None:
        """Матч не состоялся (перенесён/отменён) — только здесь возврат и уместен.

        Один и тот же путь для футбола и хоккея: ставки у них лежат с market
        'football'/'hockey', и `cancel_match` (он про CS2) их не видит.
        """
        if await self.db.match_bet_count(int(row["id"])) == 0:
            await self.db.set_status(int(row["id"]), "cancelled")
            stats["cancelled"] = stats.get("cancelled", 0) + 1
            log.info("Матч #%s (%s) снят (%s), ставок нет", row["id"], row["sport"], reason)
            return
        try:
            result = await self.db.cancel_football_match(int(row["id"]), reason=reason)
        except Exception:
            log.exception("Не смог вернуть ставки по матчу #%s", row["id"])
            return
        stats["cancelled"] = stats.get("cancelled", 0) + 1
        await notify_bettors(self.bot, self.db, result, void=True)
        log.info("Матч #%s (%s) не состоялся (%s) — ставки возвращены",
                 row["id"], row["sport"], reason)

    async def _settle_football_row(
        self, row: Any, score_a: int, score_b: int, stats: dict[str, Any]
    ) -> None:
        """Расплатиться по футбольному матчу и рассказать об этом в чаты."""
        try:
            result = await self.db.settle_football(
                int(row["id"]), score_a, score_b, source="fonbet"
            )
        except Exception:
            log.exception("Не смог рассчитать футбольный матч #%s", row["id"])
            return

        stats["settled"] += 1
        score = f"{score_a}:{score_b}"
        text = texts.football_settled_text(result, int(row["id"]), score)
        for chat_id in await self.db.chat_ids_with_bets(int(row["id"])):
            await self._safe_send(chat_id, text)
        await notify_bettors(self.bot, self.db, result, score=score)
        for express in result.get("expresses") or []:
            target = express.get("chat_id") or None
            if target:
                await self._safe_send(int(target), texts.express_result(express))
        log.info(
            "Футбол #%s рассчитан: %s — %s (%s), выплачено %s",
            row["id"], row["team_a"], row["team_b"], score, result["paid"],
        )

    async def _post_card(self, match: Any, chats: list[Any]) -> None:
        markup = keyboards.app_kb(group=True)  # в группе web_app-кнопка не отрисуется
        for index, chat in enumerate(chats):
            try:
                message = await self.bot.send_message(
                    chat_id=int(chat["chat_id"]),
                    text=texts.match_card(match),
                    reply_markup=markup,
                )
                if index == 0:
                    await self.db.set_card(int(match["id"]), int(chat["chat_id"]), message.message_id)
            except TelegramAPIError as exc:
                log.warning("Не смог запостить матч #%s в чат %s: %s", match["id"], chat["chat_id"], exc)

    async def _close_started(self, stats: dict[str, Any]) -> None:
        # матч переезжает из линии в live; при live_betting приём ставок при этом не закрывается
        for match in await self.db.close_started_matches():
            stats["closed"] += 1
            await self.db.start_maps_of_match(int(match["id"]))
            await self._edit_card(int(match["id"]))
            # обновим карточку и в остальных чатах: там своя клавиатура без кнопок
            for chat_id in await self.db.chat_ids_with_bets(int(match["id"])):
                await self._edit_card_in_chat(int(match["id"]), chat_id)

    @staticmethod
    def _required_maps(tournament: str) -> int:
        return required_maps(tournament)

    async def _series_from_maps(self, row: Any) -> tuple[str | None, str | None]:
        return await series_from_maps(self.db, row)

    async def _settle(self, matches: list[FonbetMatch], stats: dict[str, Any]) -> None:
        by_fonbet = {match.event_id: match for match in matches}
        now = int(time.time())

        for row in await self.db.matches_by_status(["live", "awaiting"]):
            if str(row["sport"]) != "cs2":
                continue          # футбол считает отдельный проход, по счёту
            match = by_fonbet.get(int(row["fonbet_id"]))
            score: str | None = None

            if match is not None and not match.finished:
                continue  # матч ещё идёт

            winner: str | None = None
            if match is not None:
                score = match.score_text
                winner = self._winner_from_score(match)

            if winner is None:
                # запасной путь: серия считается по картам
                winner, series_score = await self._series_from_maps(row)
                score = series_score or score

            if winner is None:
                # нет данных: либо ещё рано, либо исход придётся подтвердить вручную
                if match is not None:
                    await self._mark_awaiting(row, score)
                    stats["awaiting"] += 1
                elif row["status"] == "live" and now - int(row["start_time"]) > 3 * 3600:
                    await self._mark_awaiting(row, None)
                    stats["awaiting"] += 1
                continue

            await self._settle_and_announce(row, winner, score, source="fonbet")
            stats["settled"] += 1

        # матчи, которые ушли из линии, а результата у нас нет
        await self._settle_gone_cs2(by_fonbet, stats)

    async def _settle_gone_cs2(
        self, by_fonbet: dict[int, FonbetMatch], stats: dict[str, Any]
    ) -> None:
        """Матчи, которых Fonbet больше не отдаёт: считаем по последнему счёту.

        Fonbet убирает событие из линии, как только матч закончился, и результата
        после этого не отдаёт. Пока формат матча не истёк — держим его live и даём
        ставить (кэфы прошлые); счёт уже выигранный (2:0 в Bo3) — рассчитываем.
        """
        now = int(time.time())
        for row in await self.db.matches_by_status(["live", "awaiting"]):
            if str(row["sport"]) != "cs2":
                continue
            if int(row["fonbet_id"]) in by_fonbet:
                continue
            # 1) факт из внешнего фида — он знает и серию, и счёт каждой карты
            if await self._settle_cs2_from_feed(row, stats):
                continue

            score_a, score_b = row["score_a"], row["score_b"]
            tournament = str(row["tournament"])
            last_seen = int(row["last_result_check"] or 0)

            if decisive_score(tournament, score_a, score_b) and now - int(row["start_time"]) > 60 * 60:
                if not last_seen:
                    await self.db.mark_result_check(int(row["id"]))
                    continue
                if now - last_seen < FOOTBALL_GONE_QUIET_SECONDS:
                    continue
                winner, score = await self._series_from_maps(row)
                if winner is None:
                    winner = "a" if int(score_a) > int(score_b) else "b"
                    score = f"{int(score_a)}:{int(score_b)}"
                await self._settle_and_announce(row, winner, score, source="fonbet")
                stats["settled"] += 1
                continue

            played_out = (now - int(row["start_time"])) // 60
            expired = played_out > format_minutes(tournament)

            # Сыгранный матч НИКОГДА не отменяем возвратом: держим awaiting и ждём
            # результат (из live-счёта, по картам или вручную через /recount).
            if expired and str(row["status"]) != "awaiting":
                await self._mark_awaiting(row, None)
                stats["awaiting"] += 1
                log.info(
                    "CS2 #%s ушёл из линии без счёта — ждём результат вручную",
                    row["id"],
                )

    async def _settle_cs2_from_feed(self, row: Any, stats: dict[str, Any]) -> bool:
        """Закрывает серию по результатам из фида (bo3.gg). True — матч посчитан."""
        match_id = int(row["id"])
        now = int(time.time())
        if now - self._feed_checked.get(match_id, 0) < CS2_FEED_RECHECK_SECONDS:
            return False
        self._feed_checked[match_id] = now
        try:
            result = await settle_from_feed(
                self.db, self.bot, row, self.cs2_results, source="fonbet"
            )
        except Exception:
            log.exception("Фид CS2 не закрыл матч #%s", match_id)
            return False
        if result is None:
            return False
        if result.get("settled"):
            stats["settled"] += 1
            text = texts.settled_text(result, match_id, result.get("score"))
            for chat_id in await self.db.chat_ids_with_bets(match_id):
                await self._safe_send(chat_id, text)
        return True

    async def _settle_and_announce(
        self, row: Any, winner: str, score: str | None, source: str
    ) -> None:
        try:
            result = await self.db.settle_match(int(row["id"]), winner, source=source)
        except Exception:
            log.exception("Не смог рассчитать матч #%s", row["id"])
            return

        text = texts.settled_text(result, int(row["id"]), score)
        chats = await self.db.chat_ids_with_bets(int(row["id"]))
        for chat_id in chats:
            await self._safe_send(chat_id, text)

        # персонально каждому игроку: зашла его ставка или нет
        await notify_bettors(self.bot, self.db, result, score=score)

        for express in result.get("expresses") or []:
            target = express.get("chat_id") or (chats[0] if chats else None)
            if target:
                await self._safe_send(int(target), texts.express_result(express))

        await self._void_unplayed_maps(int(row["id"]))
        await self._edit_card(int(row["id"]))
        log.info("Матч #%s рассчитан: %s (%s)", row["id"], result["team"], score)

    async def _void_unplayed_maps(self, match_id: int) -> None:
        """Серия закончилась — закрываем её карты: по счёту считаем, неначатые возвращаем."""
        await finish_maps_of_match(self.db, self.bot, match_id)

    async def _close_stale(self, stats: dict[str, Any]) -> None:
        """Матч давно висит без результата.

        Сыгранный матч со ставками не отменяем никогда — только ждём исход
        (карты или ручной ввод). Возврат допустим лишь там, где ставить было некому.
        """
        for row in await self.db.stale_matches(settings.stale_match_hours):
            if str(row["sport"]) != "cs2":
                await self._close_stale_football(row, stats)
                continue          # у футбола своя логика ожидания результата
            # факт из фида — самый надёжный исход
            if await self._settle_cs2_from_feed(row, stats):
                continue
            # вдруг результат всё-таки известен по картам — тогда считаем, а не отменяем
            winner, series_score = await self._series_from_maps(row)
            if winner is not None:
                await self._settle_and_announce(row, winner, series_score, source="maps")
                stats["settled"] = stats.get("settled", 0) + 1
                log.info("Зависший матч #%s рассчитан по картам (%s)", row["id"], series_score)
                continue

            bets = await self.db.match_bet_count(int(row["id"]))
            if bets:
                # ставки есть — матч игрался, значит исход будет. Никаких возвратов.
                if str(row["status"]) != "awaiting":
                    await self._mark_awaiting(row, None)
                    stats["awaiting"] += 1
                log.warning(
                    "Матч #%s висит без результата, но ставок %s — оставляю в awaiting",
                    row["id"],
                    bets,
                )
                continue

            try:
                result = await self.db.cancel_match(int(row["id"]))
            except Exception:
                log.exception("Не смог отменить зависший матч #%s", row["id"])
                continue
            stats["cancelled"] = stats.get("cancelled", 0) + 1
            await notify_bettors(self.bot, self.db, result, void=True)
            for card in await self.db.match_maps(int(row["id"])):
                if str(card["status"]) in ("open", "live", "awaiting"):
                    try:
                        await self.db.cancel_map(int(card["id"]))
                    except Exception:
                        log.exception("Не смог вернуть карту #%s", card["id"])
            await self._edit_card(int(row["id"]))
            log.info("Матч #%s завис без результата и без ставок — снят с линии", row["id"])

    async def _close_stale_football(self, row: Any, stats: dict[str, Any]) -> None:
        """Зависший матч не-CS2: реальные ждут фид, «Хозяева — Гости» снимаем."""
        if str(row["sport"]) == "hockey" and not settings.hockey_manual_settle:
            # ручной расчёт выключен, а фид результата не знает — возвращаем деньги,
            # вместо того чтобы держать ставки в «ждём результат» бесконечно
            await self._cancel_market_row(row, stats, reason="результат не найден")
            return
        if not (is_placeholder_team(str(row["team_a"])) or is_placeholder_team(str(row["team_b"]))):
            return                # настоящий матч ждёт финального счёта из фида
        try:
            result = await self.db.cancel_football_match(
                int(row["id"]), reason="событие без названий команд"
            )
        except Exception:
            log.exception("Не смог снять фейковый матч #%s", row["id"])
            return
        stats["cancelled"] = stats.get("cancelled", 0) + 1
        await notify_bettors(self.bot, self.db, result, void=True)
        await self._edit_card(int(row["id"]))
        log.info("Матч #%s без названий команд снят с линии", row["id"])

    async def _mark_awaiting(self, row: Any, score: str | None) -> None:
        already = str(row["status"]) == "awaiting"
        await self.db.set_status(int(row["id"]), "awaiting")
        await self.db.mark_result_check(int(row["id"]))
        if already:
            # карточку с кнопками показываем один раз, а не каждые 45 секунд
            return
        text = texts.awaiting_text(row, score)
        for chat_id in await self.db.chat_ids_with_bets(int(row["id"])):
            await self._safe_send(chat_id, text, reply_markup=keyboards.confirm_result_kb(row))

    async def _edit_card(self, match_id: int) -> None:
        row = await self.db.get_match(match_id)
        if row is None or not row["posted_chat_id"] or not row["posted_message_id"]:
            return
        await self._edit_card_in_chat(match_id, int(row["posted_chat_id"]))

    async def _edit_card_in_chat(self, match_id: int, chat_id: int) -> None:
        row = await self.db.get_match(match_id)
        if row is None:
            return
        if not row["posted_message_id"] or int(row["posted_chat_id"] or 0) != chat_id:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(row["posted_message_id"]),
                text=texts.match_card(row),
                reply_markup=keyboards.app_kb(group=True),
            )
        except TelegramAPIError as exc:
            log.debug("Карточка матча #%s не обновилась: %s", match_id, exc)

    async def _safe_send(self, chat_id: int, text: str, **kwargs: Any) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, exc)

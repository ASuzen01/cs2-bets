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
from app.services.fonbet import FonbetClient, FonbetMap, FonbetMatch, parse_maps
from app.services.football import FootballMatch, parse_football
from app.services.notify import notify_bettors, notify_map_bettors

log = logging.getLogger(__name__)

# Футбольный матч с перерывом и добавленным временем заканчивается за ~100 минут.
# Fonbet выбрасывает завершённые события из линии и финальный счёт после этого
# не отдаёт (/events/event возвращает пустоту), поэтому финалом считаем последний
# счёт, который видели в live, — но только если событие пропало и минуту не
# появляется снова (пять минут тишины).
FOOTBALL_FINISH_MINUTES = 100
FOOTBALL_GONE_QUIET_SECONDS = 300


# Сколько матч формата может идти (минуты): пока не истекло, live-приём открыт,
# даже если Fonbet убрал событие из линии и результата мы ещё не знаем.
FORMAT_MINUTES = {1: 90, 2: 170, 3: 260}
# Запас сверх формата: если счёта мы так и не увидели, результат взять негде —
# возвращаем ставки, а не держим матч в «ждём результат» сутками.
FORMAT_GRACE_MINUTES = 5


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


def required_maps(tournament: str) -> int:
    """Сколько карт нужно взять, чтобы выиграть серию."""
    name = (tournament or "").lower()
    if "bo5" in name:
        return 3
    if "bo1" in name:
        return 1
    return 2


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
    def __init__(self, bot: Bot, db: Database, client: FonbetClient | None = None) -> None:
        self.bot = bot
        self.db = db
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
        }

        await self._store(matches, maps_by_parent, stats)
        await self._store_football(football, stats)
        await self._close_started(stats)
        # сначала карты: матч считается в том числе по их результатам
        await self._settle_maps(maps_by_parent, stats)
        await self._settle(matches, stats)
        await self._settle_football(football, stats)
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

    @staticmethod
    def _map_winner(score_a: int | None, score_b: int | None) -> str | None:
        """Победитель карты по раундам: до 13, с разницей минимум 2 (овертайм — до 16+)."""
        if score_a is None or score_b is None or score_a == score_b:
            return None
        high, low = max(score_a, score_b), min(score_a, score_b)
        if high < 13 or high - low < 2:
            return None
        return "a" if score_a > score_b else "b"

    async def _settle_maps(self, maps_by_parent: dict[int, list[FonbetMap]], stats: dict) -> None:
        """Раскладываем ставки по картам, как только карта доиграна."""
        by_event: dict[int, FonbetMap] = {}
        for cards in maps_by_parent.values():
            for card in cards:
                by_event[card.event_id] = card

        for row in await self.db.maps_by_status(["open", "live"]):
            card = by_event.get(int(row["fonbet_id"]))
            if card is None:
                # карта ушла из линии, а счёта нет — пусть подтвердит админ через /maps
                await self.db.set_map_status(int(row["id"]), "awaiting")
                continue
            if not card.finished:
                continue
            winner = self._map_winner(card.score_a, card.score_b)
            if winner is None:
                await self.db.set_map_status(int(row["id"]), "awaiting")
                continue
            try:
                result = await self.db.settle_map(int(row["id"]), winner, source="fonbet")
            except Exception:
                log.exception("Не смог рассчитать карту #%s", row["id"])
                continue
            stats["maps_settled"] += 1
            await notify_map_bettors(self.bot, self.db, result, score=card.score_text)
            log.info(
                "Карта #%s рассчитана: %s (%s)", row["id"], result["team"], card.score_text
            )

    # --------------------------------------------------------------- футбол
    @staticmethod
    def _prepare_three_way(match: FootballMatch) -> tuple[float | None, float | None, float | None]:
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

    async def _settle_football(
        self, matches: list[FootballMatch], stats: dict[str, Any]
    ) -> None:
        """Считаем футбольные матчи по счёту.

        Счёт берём из линии, пока матч там есть. Завершённые матчи Fonbet из линии
        убирает и результат после этого не отдаёт — тогда финальным считаем
        последний счёт, который мы видели в live (он уже лежит в базе).
        """
        by_fonbet = {match.event_id: match for match in matches}
        now = int(time.time())

        for row in await self.db.matches_by_status(["open", "live", "awaiting"]):
            if str(row["sport"]) != "football":
                continue
            match = by_fonbet.get(int(row["fonbet_id"]))
            if match is not None:
                if int(row["last_result_check"] or 0):
                    # матч вернулся в линию — «тишину» начинаем считать заново
                    await self.db.mark_result_check(int(row["id"]), 0)
                if match.finished and match.score_a is not None and match.score_b is not None:
                    await self._settle_football_row(row, match.score_a, match.score_b, stats)
                continue

            # события нет в линии: либо оно ещё не появилось, либо матч уже сыгран
            score_a, score_b = row["score_a"], row["score_b"]
            if (
                str(row["status"]) in ("live", "awaiting")
                and score_a is not None
                and score_b is not None
                and now - int(row["start_time"]) > FOOTBALL_FINISH_MINUTES * 60
            ):
                last_seen = int(row["last_result_check"] or 0)
                if not last_seen:
                    # первый раз не увидели матч — отметим время и подождём пять минут
                    await self.db.mark_result_check(int(row["id"]))
                elif now - last_seen >= FOOTBALL_GONE_QUIET_SECONDS:
                    await self._settle_football_row(row, int(score_a), int(score_b), stats)
                # отметку не двигаем: иначе окно тишины никогда не истечёт
                continue

            if str(row["status"]) != "awaiting" and now - int(row["start_time"]) > 3 * 3600:
                await self._mark_awaiting(row, None)
                stats["awaiting"] += 1

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

            if expired and score_a is None and score_b is None:
                # счёта не видели ни разу: результата взять негде — возвращаем ставки
                if played_out <= format_minutes(tournament) + FORMAT_GRACE_MINUTES:
                    continue
                try:
                    result = await self.db.cancel_match(int(row["id"]))
                except Exception:
                    log.exception("Не смог вернуть ставки по матчу #%s", row["id"])
                    continue
                stats["cancelled"] = stats.get("cancelled", 0) + 1
                await notify_bettors(self.bot, self.db, result, void=True)
                await self._edit_card(int(row["id"]))
                log.info("CS2 #%s без счёта после формата — отменён, ставки возвращены", row["id"])
                continue

            # формат истёк, результата нет — дальше только вручную
            if expired and str(row["status"]) != "awaiting":
                await self._mark_awaiting(row, None)
                stats["awaiting"] += 1

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
        """Серия закончилась раньше — ставки на несыгранные карты возвращаем."""
        for card in await self.db.match_maps(match_id):
            if str(card["status"]) not in ("open", "live", "awaiting"):
                continue
            try:
                result = await self.db.cancel_map(int(card["id"]))
            except Exception:
                log.exception("Не смог вернуть ставки по карте #%s", card["id"])
                continue
            if result["bets"]:
                await notify_map_bettors(self.bot, self.db, result, void=True)
                log.info("Карта #%s не игралась — возвращено %s", card["id"], result["refunded"])

    async def _close_stale(self, stats: dict[str, Any]) -> None:
        """Матч давно висит без результата: считаем по картам, иначе возвращаем ставки."""
        for row in await self.db.stale_matches(settings.stale_match_hours):
            if str(row["sport"]) != "cs2":
                continue          # у футбола своя логика ожидания результата
            # вдруг результат всё-таки известен по картам — тогда считаем, а не отменяем
            winner, series_score = await self._series_from_maps(row)
            if winner is not None:
                await self._settle_and_announce(row, winner, series_score, source="maps")
                stats["settled"] = stats.get("settled", 0) + 1
                log.info("Зависший матч #%s рассчитан по картам (%s)", row["id"], series_score)
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
            log.info("Матч #%s завис без результата — отменён, ставки возвращены", row["id"])

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

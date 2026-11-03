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
from app.services.notify import notify_bettors, notify_map_bettors

log = logging.getLogger(__name__)


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
        stats = {
            "fetched": len(matches),
            "created": 0,
            "updated": 0,
            "closed": 0,
            "settled": 0,
            "awaiting": 0,
            "maps_settled": 0,
        }

        await self._store(matches, maps_by_parent, stats)
        await self._close_started(stats)
        await self._settle(matches, stats)
        await self._settle_maps(maps_by_parent, stats)

        self.last_sync_at = int(time.time())
        self.last_error = None
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
            if match.start_time and match.start_time < now - 6 * 3600:
                continue  # старая линия, не нужна
            odds_a, odds_b = self._prepare_odds(match)
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
                should_post = posted < settings.autopost_limit and match.start_time <= horizon
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

    async def _settle(self, matches: list[FonbetMatch], stats: dict[str, Any]) -> None:
        by_fonbet = {match.event_id: match for match in matches}

        for row in await self.db.matches_by_status(["live", "awaiting"]):
            match = by_fonbet.get(int(row["fonbet_id"]))
            if match is None or not match.finished:
                continue
            winner = self._winner_from_score(match)
            if winner:
                await self._settle_and_announce(row, winner, match.score_text, source="fonbet")
                stats["settled"] += 1
            else:
                await self._mark_awaiting(row, match.score_text)
                stats["awaiting"] += 1

        # матчи, которые уже ушли из линии, но результата нет
        threshold = settings.result_check_after_minutes * 60
        for row in await self.db.matches_for_result_check(threshold, check_cooldown=900):
            if int(row["fonbet_id"]) in by_fonbet:
                continue
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

        await self._edit_card(int(row["id"]))
        log.info("Матч #%s рассчитан: %s (%s)", row["id"], result["team"], score)

    async def _mark_awaiting(self, row: Any, score: str | None) -> None:
        await self.db.set_status(int(row["id"]), "awaiting")
        await self.db.mark_result_check(int(row["id"]))
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

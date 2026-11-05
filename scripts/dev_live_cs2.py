"""Дев-база: живой CS2-матч с картами — воспроизводим «в лайве нет счёта и ставок»."""

import asyncio
import time

from app.db import Database


async def main() -> None:
    db = Database("/tmp/dev_cs2.db", start_balance=1000)
    await db.connect()
    now = int(time.time())
    await db.upsert_user(777, "dev", "Антон")

    match, _ = await db.upsert_match(
        fonbet_id=111001,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=now - 30 * 60,
        odds_a=1.85,
        odds_b=1.95,
        sport="cs2",
    )
    match_id = int(match["id"])
    await db.set_status(match_id, "live")
    await db.conn.execute(
        "UPDATE matches SET score_a = ?, score_b = ? WHERE id = ?", (1, 0, match_id)
    )
    await db.conn.commit()

    for number, (odds_a, odds_b, score, status) in enumerate(
        [
            (1.55, 2.4, (13, 9), "finished"),      # карта сыграна — рассчитана
            (1.9, 1.9, (6, 8), "live"),           # идёт: счёт есть, ставки открыты
            (1.8, 2.0, (9, 3), "awaiting"),       # счёт потерялся — ждём результата
            (1.7, 2.1, (None, None), "cancelled"),  # не игралась — возврат
        ], start=1
    ):
        card, _ = await db.upsert_map(
            match_id=match_id,
            fonbet_id=222000 + number,
            map_number=number,
            odds_a=odds_a,
            odds_b=odds_b,
        )
        await db.conn.execute(
            "UPDATE match_maps SET score_a = ?, score_b = ?, status = ? WHERE id = ?",
            (score[0], score[1], status, int(card["id"])),
        )
        await db.conn.commit()
        if status == "finished":
            await db.conn.execute(
                "UPDATE match_maps SET winner = 'a' WHERE id = ?", (int(card["id"]),)
            )
            await db.conn.commit()

    # матч в линии — для сравнения
    await db.upsert_match(
        fonbet_id=111002,
        tournament="Counter-Strike. BLAST Rivals. Групповой этап. Bo3",
        team_a="Spirit",
        team_b="Vitality",
        start_time=now + 3 * 3600,
        odds_a=2.1,
        odds_b=1.7,
        sport="cs2",
    )
    print("готово, матч #", match_id)
    await db.close()


asyncio.run(main())

"""Transactional persistence, betting and room matching for Durak."""
from __future__ import annotations

import json
import secrets
import string
import time
from typing import Any

from app.db.database import InsufficientFunds, InvalidBet
from app.services import durak

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
BOT_NAMES = ("Леди Пика", "Граф Треф", "Маркиз Бубен", "Барон Червей", "Сеньор Козырь")


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw)) if raw else {}
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _players(state: dict[str, Any]) -> list[dict[str, Any]]:
    return list(state.get("players") or [])


def _has_user(state: dict[str, Any], user_id: int) -> bool:
    return any(p.get("user_id") is not None and int(p["user_id"]) == int(user_id) for p in _players(state))


def _code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


async def _find_busy(conn: Any, user_id: int, *, exclude_id: int | None = None) -> Any | None:
    cutoff = int(time.time()) - 24 * 3600
    rows = await (await conn.execute(
        "SELECT id, state_json FROM durak_games "
        "WHERE status = 'active' OR (status = 'waiting' AND updated_at >= ?) "
        "ORDER BY updated_at DESC LIMIT 100",
        (cutoff,),
    )).fetchall()
    for row in rows:
        if exclude_id is not None and int(row["id"]) == int(exclude_id):
            continue
        if _has_user(_decode(row["state_json"]), user_id):
            return row
    return None


def _config_of(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "table_type": str(state.get("table_type") or "online"),
        "rule": str(state.get("mode") or "throwin"),
        "deck_size": int(state.get("deck_size") or 36),
        "stake": int(state.get("stake") or 10),
        "player_count": int(state.get("player_count") or len(_players(state)) or 2),
    }


async def _insert_room(
    conn: Any, *, user_id: int, name: str, visibility: str, config: dict[str, Any], now: int,
    initial_players: list[dict[str, Any]],
) -> Any:
    code = _code()
    for _ in range(5):
        row = await (await conn.execute("SELECT id FROM durak_games WHERE room_code = ?", (code,))).fetchone()
        if row is None:
            break
        code = _code()
    state = {
        "version": 1,
        "mode": config["rule"],
        "deck_size": config["deck_size"],
        "stake": config["stake"],
        "player_count": config["player_count"],
        "table_type": config["table_type"],
        "players": initial_players,
        "finished": False,
    }
    inserted = await conn.execute(
        """INSERT INTO durak_games
           (owner_id, room_code, visibility, status, config_json, state_json, created_at, updated_at)
           VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?) RETURNING id""",
        (int(user_id), code, visibility, _encode(config), _encode(state), now, now),
    )
    created = await inserted.fetchone()
    game_id = int(created["id"]) if created is not None else int(inserted.lastrowid or 0)
    saved = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (game_id,))).fetchone()
    if saved is None:
        result = await (await conn.execute("SELECT * FROM durak_games WHERE room_code = ?", (code,))).fetchone()
        if result is None:
            raise InvalidBet("Не удалось создать стол")
        return result
    return saved


async def _start(conn: Any, row: Any, state: dict[str, Any], now: int) -> tuple[Any, dict[str, Any]]:
    players = _players(state)
    if len(players) < 2:
        raise InvalidBet("Для начала партии нужны минимум 2 игрока")
    if len(players) > 6:
        raise InvalidBet("За столом может быть не больше 6 игроков")
    config = _config_of(state)
    if config["table_type"] == "online" and len(players) != int(config["player_count"]):
        raise InvalidBet(f"Стол заполнен не полностью: нужно {config['player_count']} игроков")
    stake = int(config["stake"])
    humans = [p for p in players if p.get("user_id") is not None]
    if not humans:
        raise InvalidBet("За столом нет реальных игроков")
    for player in humans:
        user_id = int(player["user_id"])
        debit = await conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (stake, user_id, stake),
        )
        if debit.rowcount == 0:
            raise InsufficientFunds(f"{player['name']}: недостаточно монет для ставки")
        await row_db_ledger(conn, user_id, -stake, f"ставка · Дурак · стол #{int(row['id'])}", now)

    game = durak.new_game(
        players,
        deck_size=int(config["deck_size"]),
        mode=str(config["rule"]),
        stake=stake,
    )
    game["player_count"] = len(players)
    game["table_type"] = str(config["table_type"])
    game["visibility"] = str(row["visibility"])
    if config["table_type"] == "bots":
        durak.run_bots(game)
    await conn.execute(
        "UPDATE durak_games SET status = 'active', state_json = ?, updated_at = ? WHERE id = ?",
        (_encode(game), now, int(row["id"])),
    )
    saved = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(row["id"]),))).fetchone()
    return saved or row, game


async def row_db_ledger(conn: Any, user_id: int, delta: int, reason: str, now: int) -> None:
    await conn.execute(
        "INSERT INTO ledger (user_id, delta, reason, created_at) VALUES (?, ?, ?, ?)",
        (int(user_id), int(delta), str(reason), int(now)),
    )


async def _finish(db: Any, conn: Any, row: Any, state: dict[str, Any], now: int) -> None:
    players = _players(state)
    count = len(players)
    stake = int(state["stake"])
    if state.get("draw_reason"):
        payouts: dict[str, int] = {}
        for player in players:
            user_id = player.get("user_id")
            if user_id is None:
                continue
            payouts[str(int(user_id))] = stake
            await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (stake, int(user_id)))
            await row_db_ledger(conn, int(user_id), stake, "возврат ставки · Дурак · ничья", now)
        state["payouts"] = payouts
        state["pool"] = stake * count
        state["status"] = "draw"
        await conn.execute(
            "UPDATE durak_games SET status = 'draw', state_json = ?, updated_at = ? WHERE id = ?",
            (_encode(state), now, int(row["id"])),
        )
        return
    shares = durak.payouts_by_place(count, stake)
    payouts: dict[str, int] = {}
    for player in players:
        user_id = player.get("user_id")
        if user_id is None:
            continue
        place = int(player.get("place") or count)
        payout = int(shares[place - 1])
        payouts[str(int(user_id))] = payout
        if payout:
            await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, int(user_id)))
            await row_db_ledger(conn, int(user_id), payout, f"выплата · Дурак · {place}-е место", now)
        outcome = "win" if payout > stake else "loss" if payout < stake else "draw"
        summary = {
            "place": place,
            "player_count": count,
            "stake": stake,
            "pool": stake * count,
            "payout": payout,
            "mode": str(state["mode"]),
            "deck_size": int(state["deck_size"]),
        }
        await conn.execute(
            """INSERT INTO casino_bets
               (user_id, game, selection, label, amount, odds, outcome, win, payout, result_json, created_at)
               VALUES (?, 'durak', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id), f"place:{place}", f"Дурак · {place}-е место из {count}",
                stake, (payout / stake if stake else 0), outcome, int(outcome == "win"),
                payout, _encode(summary), now,
            ),
        )
        await db._bump_quest(conn, int(user_id), "casino", now)
    state["payouts"] = payouts
    state["pool"] = stake * count
    state["status"] = "finished"
    await conn.execute(
        "UPDATE durak_games SET status = 'finished', state_json = ?, updated_at = ? WHERE id = ?",
        (_encode(state), now, int(row["id"])),
    )


def _public(row: Any, state: dict[str, Any], user_id: int) -> dict[str, Any]:
    payload = durak.public_game(
        state,
        game_id=int(row["id"]),
        user_id=int(user_id),
        status=str(row["status"]),
        code=str(row["room_code"]),
        visibility=str(row["visibility"]),
    )
    payload["is_owner"] = int(row["owner_id"]) == int(user_id)
    return payload


async def _expire_stale_games(db: Any, now: int) -> None:
    cutoff = now - 24 * 3600
    stale = await db._fetchone(
        "SELECT id FROM durak_games WHERE status IN ('waiting', 'active') AND updated_at < ? LIMIT 1",
        (cutoff,),
    )
    if stale is None:
        return
    async with db.transaction() as conn:
        rows = await (await conn.execute(
            "SELECT * FROM durak_games WHERE status IN ('waiting', 'active') AND updated_at < ?",
            (cutoff,),
        )).fetchall()
        for row in rows:
            raw = _decode(row["state_json"])
            if str(row["status"]) == "active":
                stake = int(raw.get("stake") or 0)
                for player in _players(raw):
                    player_id = player.get("user_id")
                    if player_id is None:
                        continue
                    await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (stake, int(player_id)))
                    await row_db_ledger(conn, int(player_id), stake, "возврат ставки · Дурак · стол без ходов сутки", now)
            raw["status"] = "cancelled"
            raw["cancel_reason"] = "inactive"
            await conn.execute(
                "UPDATE durak_games SET status = 'cancelled', state_json = ?, updated_at = ? WHERE id = ?",
                (_encode(raw), now, int(row["id"])),
            )


async def state(db: Any, user_id: int) -> dict[str, Any]:
    now = int(time.time())
    await _expire_stale_games(db, now)
    cutoff = now - 24 * 3600
    rows = await db._fetchall(
        "SELECT * FROM durak_games "
        "WHERE status = 'active' OR (status = 'waiting' AND updated_at >= ?) "
        "ORDER BY updated_at DESC LIMIT 100",
        (cutoff,),
    )
    mine = None
    for row in rows:
        raw = _decode(row["state_json"])
        if _has_user(raw, user_id):
            mine = _public(row, raw, user_id)
            break
    lobbies = []
    for row in rows:
        if str(row["status"]) != "waiting" or str(row["visibility"]) != "public":
            continue
        raw = _decode(row["state_json"])
        if _has_user(raw, user_id):
            continue
        config = _config_of(raw)
        lobbies.append({
            "id": int(row["id"]),
            "mode": config["rule"],
            "deck_size": config["deck_size"],
            "stake": config["stake"],
            "players_count": len(_players(raw)),
            "player_count": config["player_count"],
            "players": [str(p["name"]) for p in _players(raw)],
        })
    last_rows = await db._fetchall(
        "SELECT * FROM durak_games WHERE status = 'finished' ORDER BY updated_at DESC LIMIT 30"
    )
    last = None
    for row in last_rows:
        raw = _decode(row["state_json"])
        if _has_user(raw, user_id):
            last = _public(row, raw, user_id)
            break
    return {"game": mine, "lobbies": lobbies[:20], "last": last}


async def create(
    db: Any, *, user_id: int, name: str, table_type: str, visibility: str,
    mode: str, deck_size: int, stake: int, player_count: int,
) -> dict[str, Any]:
    table_type = str(table_type)
    visibility = str(visibility)
    if table_type not in {"bots", "online"}:
        raise InvalidBet("Выбери игру с ботами или онлайн-стол")
    if table_type == "online" and visibility not in {"public", "private"}:
        raise InvalidBet("Выбери открытый или приватный стол")
    durak.validate(deck_size, mode, player_count, stake)
    now = int(time.time())
    config = {
        "table_type": table_type,
        "rule": str(mode),
        "deck_size": int(deck_size),
        "stake": int(stake),
        "player_count": int(player_count),
    }
    async with db.transaction() as conn:
        if await _find_busy(conn, user_id) is not None:
            raise InvalidBet("Сначала завершите или дождитесь текущую партию")
        user = await (await conn.execute("SELECT username, full_name, balance FROM users WHERE user_id = ?", (int(user_id),))).fetchone()
        if user is None:
            raise InvalidBet("Профиль игрока не найден")
        if int(user["balance"]) < int(stake):
            raise InsufficientFunds("На балансе меньше выбранной ставки")
        display_name = str(user["full_name"] or (f"@{user['username']}" if user["username"] else name or "Игрок"))[:40]

        if table_type == "online" and visibility == "public":
            candidates = await (await conn.execute(
                "SELECT * FROM durak_games WHERE status = 'waiting' AND visibility = 'public' "
                "AND updated_at >= ? ORDER BY id DESC LIMIT 50",
                (now - 24 * 3600,),
            )).fetchall()
            for candidate in candidates:
                raw = _decode(candidate["state_json"])
                old_config = _config_of(raw)
                if old_config != config or _has_user(raw, user_id) or len(_players(raw)) >= int(player_count):
                    continue
                await _join_in_tx(conn, candidate, raw, user_id, display_name, now)
                refreshed = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(candidate["id"]),))).fetchone()
                return _public(refreshed, _decode(refreshed["state_json"]), user_id)

        if table_type == "bots":
            bots = [
                {"seat": seat, "user_id": None, "name": BOT_NAMES[(seat - 1) % len(BOT_NAMES)], "bot": True, "hand": [], "place": None}
                for seat in range(1, int(player_count))
            ]
            participants = [{"seat": 0, "user_id": int(user_id), "name": display_name, "bot": False, "hand": [], "place": None}, *bots]
        else:
            participants = [{"seat": 0, "user_id": int(user_id), "name": display_name, "bot": False, "hand": [], "place": None}]
        row = await _insert_room(
            conn, user_id=user_id, name=display_name,
            visibility="private" if table_type == "bots" else visibility,
            config=config, now=now, initial_players=participants,
        )
        if table_type == "bots":
            row, game = await _start(conn, row, _decode(row["state_json"]), now)
            return _public(row, game, user_id)
        return _public(row, _decode(row["state_json"]), user_id)


async def _join_in_tx(conn: Any, row: Any, raw: dict[str, Any], user_id: int, name: str, now: int) -> None:
    if now - int(row["updated_at"]) > 24 * 3600:
        raise InvalidBet("Стол устарел — создай новый")
    if str(row["status"]) != "waiting":
        raise InvalidBet("Этот стол уже начался или закрыт")
    if _has_user(raw, user_id):
        return
    if await _find_busy(conn, user_id, exclude_id=int(row["id"])) is not None:
        raise InvalidBet("Сначала завершите или дождитесь текущую партию")
    config = _config_of(raw)
    if len(_players(raw)) >= int(config["player_count"]):
        raise InvalidBet("За столом уже нет свободных мест")
    user = await (await conn.execute("SELECT balance FROM users WHERE user_id = ?", (int(user_id),))).fetchone()
    if user is None or int(user["balance"]) < int(config["stake"]):
        raise InsufficientFunds("На балансе меньше выбранной ставки")
    raw["players"].append({
        "seat": len(raw["players"]), "user_id": int(user_id), "name": str(name)[:40],
        "bot": False, "hand": [], "place": None,
    })
    await conn.execute(
        "UPDATE durak_games SET state_json = ?, updated_at = ? WHERE id = ?",
        (_encode(raw), now, int(row["id"])),
    )
    if str(row["visibility"]) == "public" and len(raw["players"]) == int(config["player_count"]):
        refreshed = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(row["id"]),))).fetchone()
        await _start(conn, refreshed, raw, now)


async def join(db: Any, *, user_id: int, name: str, game_id: int | None = None, code: str | None = None) -> dict[str, Any]:
    now = int(time.time())
    async with db.transaction() as conn:
        if game_id is not None:
            row = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(game_id),))).fetchone()
        else:
            row = await (await conn.execute("SELECT * FROM durak_games WHERE room_code = ?", (str(code or "").strip().upper(),))).fetchone()
        if row is None:
            raise InvalidBet("Открытый стол или код не найдены")
        if str(row["visibility"]) == "private" and game_id is not None and int(row["owner_id"]) != int(user_id):
            # Joining by the share code is the private-room access path.
            raise InvalidBet("Для приватного стола нужен код приглашения")
        raw = _decode(row["state_json"])
        await _join_in_tx(conn, row, raw, user_id, name, now)
        updated = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(row["id"]),))).fetchone()
        return _public(updated, _decode(updated["state_json"]), user_id)


async def start(db: Any, *, user_id: int, game_id: int) -> dict[str, Any]:
    now = int(time.time())
    async with db.transaction() as conn:
        row = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(game_id),))).fetchone()
        if row is None or int(row["owner_id"]) != int(user_id):
            raise InvalidBet("Только создатель стола может начать партию")
        raw = _decode(row["state_json"])
        if now - int(row["updated_at"]) > 24 * 3600:
            raise InvalidBet("Стол устарел — создай новый")
        if str(row["status"]) != "waiting":
            raise InvalidBet("Стол уже начат или закрыт")
        updated, game = await _start(conn, row, raw, now)
        return _public(updated, game, user_id)


async def cancel(db: Any, *, user_id: int, game_id: int) -> None:
    now = int(time.time())
    async with db.transaction() as conn:
        row = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(game_id),))).fetchone()
        if row is None or int(row["owner_id"]) != int(user_id) or str(row["status"]) != "waiting":
            raise InvalidBet("Нельзя отменить этот стол")
        await conn.execute("UPDATE durak_games SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, int(game_id)))


async def action(
    db: Any, *, user_id: int, game_id: int, action_name: str,
    card_index: int | None = None, token: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    tap = db._clean_token(token)
    async with db.transaction() as conn:
        row = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(game_id),))).fetchone()
        if row is None or str(row["status"]) != "active":
            raise InvalidBet("Активная партия не найдена")
        raw = _decode(row["state_json"])
        player = next((p for p in _players(raw) if p.get("user_id") is not None and int(p["user_id"]) == int(user_id)), None)
        if player is None:
            raise InvalidBet("Вы не участник этой партии")
        tokens = raw.setdefault("last_tokens", {})
        if tap and tokens.get(str(user_id)) == tap:
            return {"game": _public(row, raw, user_id), "balance": await db._balance_in(conn, user_id)}
        try:
            durak.act(raw, int(player["seat"]), str(action_name), card_index=card_index)
            if raw.get("table_type") == "bots":
                durak.run_bots(raw)
        except ValueError as exc:
            raise InvalidBet(str(exc)) from exc
        if tap:
            tokens[str(user_id)] = tap
        if raw.get("finished"):
            await _finish(db, conn, row, raw, now)
            status = "draw" if raw.get("draw_reason") else "finished"
        else:
            status = "active"
            await conn.execute("UPDATE durak_games SET state_json = ?, updated_at = ? WHERE id = ?", (_encode(raw), now, int(game_id)))
        saved = await (await conn.execute("SELECT * FROM durak_games WHERE id = ?", (int(game_id),))).fetchone()
        return {"game": _public(saved, raw, user_id), "balance": await db._balance_in(conn, user_id), "finished": status == "finished"}


__all__ = ["action", "cancel", "create", "join", "start", "state"]

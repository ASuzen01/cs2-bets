"""Преобразование строк БД в JSON для интерфейса.

Всё, что уходит в браузер, собирается здесь: интерфейс не зависит от устройства БД
(SQLite/Postgres) и от того, кто вызвал — бот или приложение.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from app.db import Database
from app.services import football as football_service
from app.services import hockey as hockey_service
from app.services.football import market_label, parse_side, tournament_category

# вторая часть названия иногда не лига, а стадия: «Премьер-Лига. 12-й тур»
_ROUND_PART = re.compile(r"^(\d|тур\b|сезон|этап|раунд|плей-офф|финал|групп|общий)", re.IGNORECASE)

_CUPS = (
    ("лига чемпионов", "ЛЧ"),
    ("лига европы", "ЛЕ"),
    ("лига конференций", "ЛК"),
    # хоккей: лига стоит в начале названия, как еврокубки, — страну не показываем
    ("фонбет кхл", "КХЛ"),
    ("кхл", "КХЛ"),
    ("нхл", "НХЛ"),
    ("чемпионат мира", "ЧМ"),
    # Лига наций: дивизион — это вторая часть названия, а не «страна»
    ("лига наций", "Лига наций"),
)


def short_tournament(name: str, limit: int = 42) -> str:
    """'Counter-Strike. IEM Пекин. Групповой этап. Bo3' → 'IEM Пекин'.

    У футбольных еврокубков название начинается с турнира, а не с вида спорта:
    'Лига Чемпионов УЕФА. Общий этап' — коротко это «ЛЧ · Общий этап».
    """
    parts = [part.strip() for part in name.split(".") if part.strip()]
    head = (parts[0] if parts else "").lower()
    for needle, short in _CUPS:
        if head.startswith(needle):
            stage = parts[1] if len(parts) > 1 else ""
            return f"{short} · {stage}"[:limit] if stage else short
    if len(parts) > 1:
        base = parts[1]
        if _ROUND_PART.match(base) and not _ROUND_PART.match(parts[0]):
            base = parts[0]      # «Премьер-Лига. 12-й тур» → показываем лигу, а не тур
        bo = next(
            (part for part in parts
             if len(part) in (3, 4) and part[:2].lower() == "bo" and part[2:].isdigit()),
            "",
        )
        # формат серии (Bo3/Bo5) показываем рядом с турниром: от него зависит, сколько карт
        return f"{base} · {bo}"[:limit] if bo else base[:limit]
    return name[:limit]


def side_label(side: str, team_a: str, team_b: str) -> str:
    """Подпись исхода: команда, ничья или конкретный рынок (тотал, фора, счёт)."""
    if side == "a":
        return team_a
    if side == "b":
        return team_b
    if side == "x":
        return "Ничья"
    parsed = parse_side(side)
    if parsed:
        return market_label(parsed[0], parsed[1])
    hockey = hockey_service.parse_side(side)
    if hockey:
        return hockey_service.market_label(hockey[0], hockey[1])
    return side


def markets_for(row: Any) -> list[dict[str, Any]]:
    """Роспись матча по его виду спорта: футбол и хоккей считаются разными моделями."""
    sport = str(row["sport"]) if "sport" in row.keys() else "cs2"  # noqa: SIM118
    if sport == "football":
        return football_service.markets_of(row)
    if sport == "hockey":
        return hockey_service.markets_of(row)
    return []


def map_payload(row: Any) -> dict[str, Any]:
    """Рынок «победитель конкретной карты»."""
    score_a, score_b = row["score_a"], row["score_b"]
    return {
        "id": int(row["id"]),
        "number": int(row["map_number"]),
        "odds_a": float(row["odds_a"]) if row["odds_a"] else None,
        "odds_b": float(row["odds_b"]) if row["odds_b"] else None,
        "status": str(row["status"]),
        "winner": row["winner"],
        "score": f"{int(score_a)}:{int(score_b)}" if score_a is not None and score_b is not None else None,
    }


def match_payload(row: Any, maps: Iterable[Any] = ()) -> dict[str, Any]:
    status = str(row["status"])
    # sqlite3.Row итерируется значениями, поэтому проверяем именно ключи
    score_a = row["score_a"] if "score_a" in row.keys() else None  # noqa: SIM118
    score_b = row["score_b"] if "score_b" in row.keys() else None  # noqa: SIM118
    score = None
    if score_a is not None and score_b is not None:
        score = f"{int(score_a)}:{int(score_b)}"
    return {
        "id": int(row["id"]),
        "tournament": str(row["tournament"]),
        "short_tournament": short_tournament(str(row["tournament"])),
        "team_a": str(row["team_a"]),
        "team_b": str(row["team_b"]),
        # кэф может быть снят: в лайве Fonbet иногда убирает рынок на победителя
        "odds_a": float(row["odds_a"]) if row["odds_a"] else None,
        "odds_b": float(row["odds_b"]) if row["odds_b"] else None,
        "start_time": int(row["start_time"]),
        "status": status,
        "live": status == "live",
        "score": score,
        "score_a": int(score_a) if score_a is not None else None,
        "score_b": int(score_b) if score_b is not None else None,
        # ставки на карты: «Карта 1», «Карта 2»…
        "maps": [map_payload(card) for card in maps],
        "sport": str(row["sport"]) if "sport" in row.keys() else "cs2",  # noqa: SIM118
        "odds_x": float(row["odds_x"]) if ("odds_x" in row.keys() and row["odds_x"]) else None,  # noqa: SIM118
        # роспись футбола и хоккея: тоталы, форы, исходы. Для CS2 список пустой
        "markets": markets_for(row),
        # подменю «Клубы» / «Сборные»: у CS2 категории нет
        "category": _category(row),
    }


def _category(row: Any) -> str | None:
    """Клубы или сборные. У CS2 подменю нет — там всегда None."""
    sport = str(row["sport"]) if "sport" in row.keys() else "cs2"  # noqa: SIM118
    if sport not in ("football", "hockey"):
        return None
    return tournament_category(str(row["tournament"]))


def bet_payload(row: Any) -> dict[str, Any]:
    side = str(row["side"])
    amount = int(row["amount"])
    odds = float(row["odds"])
    cells = dict(row)
    map_number = cells.get("map_number")
    market = "map" if map_number else "match"
    return {
        "match_id": int(row["match_id"]),
        "side": side,
        "amount": amount,
        "odds": odds,
        "label": side_label(side, str(row["team_a"]), str(row["team_b"])),
        "team": side_label(side, str(row["team_a"]), str(row["team_b"])),
        "team_a": row["team_a"],
        "team_b": row["team_b"],
        "market": market,
        "market_label": f"Карта {int(map_number)}" if map_number else None,
        "potential": round(amount * odds),
    }


def express_payload(row: Any) -> dict[str, Any]:
    stake = int(row["stake"])
    odds = float(row["odds"])
    return {
        "id": int(row["id"]),
        "stake": stake,
        "odds": odds,
        "potential": round(stake * odds),
        "legs": int(row["legs"]) if "legs" in row else None,
    }


def display_name(user: Mapping[str, Any], row: Any = None) -> str:
    """Имя из базы, иначе @username, иначе id — чтобы в интерфейсе всегда было что показать."""
    if row is not None and row["full_name"]:
        return str(row["full_name"])
    if row is not None and row["username"]:
        return f"@{row['username']}"
    if user["full_name"]:
        return str(user["full_name"])
    if user["username"]:
        return f"@{user['username']}"
    return f"id{user['user_id']}"


def legs_word(count: int) -> str:
    """1 исход · 2 исхода · 5 исходов."""
    tail = count % 100
    if 11 <= tail <= 14:
        return "исходов"
    tail = count % 10
    if tail == 1:
        return "исход"
    if 2 <= tail <= 4:
        return "исхода"
    return "исходов"


# ------------------------------------------------------------ история ставок
def _single_item(row: Any) -> dict[str, Any]:
    side = str(row["side"])
    team = side_label(side, str(row["team_a"]), str(row["team_b"]))
    amount = int(row["amount"])
    odds = float(row["odds"])
    cells = dict(row)  # dict() работает и для sqlite3.Row, и для dict из Postgres
    market = str(cells.get("market") or "match")
    map_number = cells.get("map_number")
    market_label = f"Карта {int(map_number)}" if market == "map" and map_number else None
    subtitle = f"Ставка на {team}" + (f" · {market_label}" if market_label else "")
    return {
        "kind": "single",
        "id": int(row["id"]),
        "match_id": int(row["match_id"]),
        "title": f"{row['team_a']} — {row['team_b']}",
        "subtitle": subtitle,
        "market": market,
        "market_label": market_label,
        "tournament": str(row["tournament"]),
        "stake": amount,
        "odds": round(odds, 2),
        "potential": round(amount * odds),
        "team": team,
        "at": int(row["created_at"]),
        "start_time": int(row["start_time"]),
        "legs": [],
    }


def _leg_view(leg: Any) -> dict[str, Any]:
    side = str(leg["side"])
    return {
        "match_id": int(leg["match_id"]),
        "side": side,
        "odds": float(leg["odds"]),
        "team": side_label(side, str(leg["team_a"]), str(leg["team_b"])),
        "match": f"{leg['team_a']} — {leg['team_b']}",
        "result": str(leg["result"]),
        "match_status": str(leg["match_status"]),
    }


def _group_legs(legs: Iterable[Any]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for leg in legs:
        grouped.setdefault(int(leg["express_id"]), []).append(_leg_view(leg))
    return grouped


async def bets_history(db: Database, user_id: int, *, singles_limit: int = 50, express_limit: int = 20) -> dict[str, Any]:
    """Что в игре и что рассчитано — одиночные и экспрессы в одном списке."""
    single_rows = await db.user_bets_all(user_id, singles_limit)
    express_rows = await db.user_expresses_all(user_id, express_limit)
    legs_by_express = _group_legs(
        await db.express_legs_many([int(row["id"]) for row in express_rows])
    )

    open_items: list[dict[str, Any]] = []
    settled_items: list[dict[str, Any]] = []

    for row in single_rows:
        item = _single_item(row)
        cells = dict(row)
        # ставка на карту живёт по статусу карты, ставка на матч — по статусу матча
        is_map = cells.get("market") == "map" and cells.get("map_status")
        match_status = str(cells.get("map_status") if is_map else row["match_status"])
        item["match_status"] = match_status

        if match_status in ("open", "live", "awaiting"):
            # «ждём результата» — матч сыгран, но исход ещё не подтверждён: нельзя
            # показывать это как «ждёт старта», иначе выглядит, будто ставка зависла
            item["status"] = {"live": "live", "awaiting": "awaiting"}.get(match_status, "open")
            open_items.append(item)
            continue

        paid = int(row["payout"] or 0)
        item["payout"] = paid if match_status != "cancelled" else (paid or item["stake"])
        item["result"] = "void" if match_status == "cancelled" else ("won" if paid > 0 else "lost")
        if is_map:
            item["map_winner"] = cells.get("map_winner")
        settled_items.append(item)

    for row in express_rows:
        legs = legs_by_express.get(int(row["id"]), [])
        stake = int(row["stake"])
        odds = float(row["odds"])
        item = {
            "kind": "express",
            "id": int(row["id"]),
            "title": f"Экспресс · {len(legs)} {legs_word(len(legs))}",
            "subtitle": " + ".join(leg["team"] for leg in legs),
            "tournament": "",
            "stake": stake,
            "odds": round(odds, 2),
            "potential": round(stake * odds),
            "at": int(row["created_at"]),
            "legs": legs,
        }
        status = str(row["status"])
        if status == "open":
            item["status"] = "open"
            open_items.append(item)
        else:
            item["status"] = "settled"
            item["result"] = status  # won / lost / void
            item["payout"] = int(row["payout"] or 0)
            settled_items.append(item)

    open_items.sort(key=lambda item: item.get("start_time") or item["at"])
    settled_items.sort(key=lambda item: item["at"], reverse=True)

    return {
        "open": open_items,
        "settled": settled_items[:30],
        "counts": {
            "open": len(open_items),
            "settled": len(settled_items),
            "won": sum(1 for item in settled_items if item.get("result") == "won"),
            "lost": sum(1 for item in settled_items if item.get("result") == "lost"),
        },
    }


__all__ = [
    "bet_payload",
    "bets_history",
    "display_name",
    "express_payload",
    "legs_word",
    "map_payload",
    "match_payload",
    "short_tournament",
]

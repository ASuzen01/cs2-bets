"""Серверные правила игры «Пьяница».

Полная колода делится поровну. Старшая карта забирает спор; при равенстве
каждый участник кладёт карту рубашкой вверх и открывает следующую. Туз старший.
"""

from __future__ import annotations

import secrets
from collections import deque
from typing import Any

VALID_DECK_SIZES = {24, 36, 52}
MAX_BATTLES = 50_000
SUITS = ("♠", "♥", "♦", "♣")
RANKS = {
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9",
    10: "10", 11: "В", 12: "Д", 13: "К", 14: "Т",
}


def _deck(size: int) -> list[dict[str, Any]]:
    if size not in VALID_DECK_SIZES:
        raise ValueError("Выбери колоду на 24, 36 или 52 карты")
    low_rank = {24: 9, 36: 6, 52: 2}[size]
    return [
        {"value": value, "rank": RANKS[value], "suit": suit, "text": f"{RANKS[value]}{suit}"}
        for value in range(low_rank, 15)
        for suit in SUITS
    ]


def simulate_game(size: int) -> dict[str, Any]:
    """Сыграть полную партию на сервере; один из победителей забирает всю колоду."""
    cards = _deck(int(size))
    rng = secrets.SystemRandom()
    rng.shuffle(cards)
    player = deque(cards[::2])
    opponent = deque(cards[1::2])
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    rounds = 0
    wars = 0
    final_cards: tuple[dict[str, Any], dict[str, Any]] | None = None
    winner: str | None = None
    tie_reason: str | None = None

    while player and opponent and rounds < MAX_BATTLES:
        winner = None
        position = (tuple(card["text"] for card in player), tuple(card["text"] for card in opponent))
        if position in seen:
            tie_reason = "cycle"
            break  # периодическая партия: безопасная ничья, ставки возвращаются
        seen.add(position)
        rounds += 1
        first, second = player.popleft(), opponent.popleft()
        pot = [first, second]
        final_cards = (first, second)

        if first["value"] == second["value"]:
            wars += 1
            while True:
                # В споре каждый кладёт одну закрытую, затем одну открытую карту.
                # Если карт на продолжение нет, оставшийся соперник забирает спор.
                if not player and not opponent:
                    winner = None
                    break
                if len(player) < 2 or len(opponent) < 2:
                    if not player and not opponent:
                        winner = None
                        tie_reason = "war_exhausted"
                    elif not player:
                        winner = "opponent"
                    elif not opponent:
                        winner = "player"
                    elif len(player) == len(opponent):
                        winner = None
                        tie_reason = "war_exhausted"
                    else:
                        winner = "player" if len(player) > len(opponent) else "opponent"
                    if winner == "player":
                        pot.extend(opponent)
                        opponent.clear()
                    elif winner == "opponent":
                        pot.extend(player)
                        player.clear()
                    break
                pot.extend((player.popleft(), opponent.popleft()))  # закрытые карты
                first, second = player.popleft(), opponent.popleft()
                pot.extend((first, second))
                final_cards = (first, second)
                if first["value"] != second["value"]:
                    winner = "player" if first["value"] > second["value"] else "opponent"
                    break
                wars += 1

        else:
            winner = "player" if first["value"] > second["value"] else "opponent"

        if winner == "player":
            rng.shuffle(pot)
            player.extend(pot)
        elif winner == "opponent":
            rng.shuffle(pot)
            opponent.extend(pot)
        else:
            break

    if winner is None and not opponent and player:
        winner = "player"
    elif winner is None and not player and opponent:
        winner = "opponent"
    elif rounds >= MAX_BATTLES and player and opponent:
        winner = None
        tie_reason = "safety_limit"

    return {
        "deck_size": int(size),
        "winner": winner,
        "rounds": rounds,
        "wars": wars,
        "cards_player": len(player),
        "cards_opponent": len(opponent),
        "last_player_card": final_cards[0]["text"] if final_cards else None,
        "last_opponent_card": final_cards[1]["text"] if final_cards else None,
        "tie_reason": tie_reason,
    }


__all__ = ["VALID_DECK_SIZES", "simulate_game"]

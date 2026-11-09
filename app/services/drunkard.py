"""Серверные правила игры «Пьяница».

Полная колода делится поровну. Старшая карта забирает спор; при равенстве
каждый участник кладёт карту рубашкой вверх и открывает следующую. В 52-карточной
колоде двойка бьёт туза, остальные ранги сравниваются по обычному порядку.
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


def _compare_values(first: int, second: int) -> int:
    """Compare ranks: 2 beats Ace; all other ranks use normal numeric order."""
    left, right = int(first), int(second)
    if left == right:
        return 0
    if left == 2 and right == 14:
        return 1
    if left == 14 and right == 2:
        return -1
    return 1 if left > right else -1


def _winner_for(first: dict[str, Any], second: dict[str, Any]) -> str | None:
    comparison = _compare_values(int(first["value"]), int(second["value"]))
    return "player" if comparison > 0 else "opponent" if comparison < 0 else None


def new_game_state(size: int) -> dict[str, Any]:
    """Создаёт скрытые серверные руки для интерактивной партии."""
    cards = _deck(int(size))
    secrets.SystemRandom().shuffle(cards)
    return {
        "version": 1,
        "deck_size": int(size),
        "hands": {"player": cards[::2], "opponent": cards[1::2]},
        "rounds": 0,
        "wars": 0,
        "last_battle": None,
        "winner": None,
        "tie_reason": None,
    }


def advance_round(state: dict[str, Any]) -> dict[str, Any]:
    """Открывает одну обычную раздачу и, при равенстве, всю последовательность войны."""
    hands = state["hands"]
    player = hands["player"]
    opponent = hands["opponent"]
    if state.get("winner") is not None or not player or not opponent:
        return state

    pot: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    hidden_pairs = 0
    first, second = player.pop(0), opponent.pop(0)
    pot.extend((first, second))
    pairs.append({"player": first["text"], "opponent": second["text"]})
    state["rounds"] += 1
    winner = _winner_for(first, second)

    while winner is None:
        state["wars"] += 1
        if len(player) < 2 and len(opponent) < 2:
            pot.extend(player)
            pot.extend(opponent)
            player.clear()
            opponent.clear()
            state["tie_reason"] = "war_exhausted"
            state["winner"] = "draw"
            break
        if len(player) < 2:
            pot.extend(player)
            player.clear()
            winner = "opponent"
            break
        if len(opponent) < 2:
            pot.extend(opponent)
            opponent.clear()
            winner = "player"
            break

        # Each side burns one facedown card, then reveals one.
        pot.extend((player.pop(0), opponent.pop(0)))
        hidden_pairs += 1
        first, second = player.pop(0), opponent.pop(0)
        pot.extend((first, second))
        pairs.append({"player": first["text"], "opponent": second["text"]})
        winner = _winner_for(first, second)
        if winner is not None:
            break

    if state.get("winner") != "draw":
        rng = secrets.SystemRandom()
        rng.shuffle(pot)
        hands[winner].extend(pot)
        if not player:
            state["winner"] = "opponent"
        elif not opponent:
            state["winner"] = "player"

    state["last_battle"] = {
        "pairs": pairs,
        "hidden_pairs": hidden_pairs,
        "winner": state.get("winner") or winner,
        "pot_size": len(pot),
        "cards_player": len(player),
        "cards_opponent": len(opponent),
    }
    return state


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

        if _compare_values(first["value"], second["value"]) == 0:
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
                winner = _winner_for(first, second)
                if winner is not None:
                    break
                wars += 1

        else:
            winner = _winner_for(first, second)

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

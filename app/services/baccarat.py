"""Eight-deck Punto Banco baccarat with server-side dealing."""

from __future__ import annotations

import secrets
from typing import Any

DECKS = 8
SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
CARD_POINTS = {"A": 1, **{str(value): value for value in range(2, 10)}, "10": 0, "J": 0, "Q": 0, "K": 0}
PAYOUTS = {"player": 2.0, "banker": 1.95, "tie": 9.0}  # gross multiplier, including the returned stake
LABELS = {"player": "Игрок", "banker": "Банкир", "tie": "Ничья"}


def build_shoe() -> list[dict[str, Any]]:
    return [
        {"rank": rank, "suit": suit, "points": CARD_POINTS[rank]}
        for _ in range(DECKS)
        for suit in SUITS
        for rank in RANKS
    ]


def _points(hand: list[dict[str, Any]]) -> int:
    return sum(int(card["points"]) for card in hand) % 10


def banker_draws(banker_points: int, player_third: int | None) -> bool:
    """Punto Banco banker third-card tableau."""
    if banker_points <= 2:
        return True
    if player_third is None:
        return banker_points <= 5
    if banker_points == 3:
        return player_third != 8
    if banker_points == 4:
        return 2 <= player_third <= 7
    if banker_points == 5:
        return 4 <= player_third <= 7
    if banker_points == 6:
        return player_third in (6, 7)
    return False


def _draw(deck: list[dict[str, Any]]) -> dict[str, Any]:
    if not deck:
        raise ValueError("В обуви закончились карты")
    return deck.pop()


def _public_card(card: dict[str, Any]) -> dict[str, Any]:
    return {"rank": str(card["rank"]), "suit": str(card["suit"]), "points": int(card["points"])}


def play(selection: str, amount: int, *, deck: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Deal a complete baccarat hand and settle a Player/Banker/Tie wager.

    A fresh cryptographically shuffled eight-deck shoe is used for each hand.
    On Player/Banker bets a tie returns the stake (push); Tie pays 8:1 net.
    """
    choice = str(selection).strip().lower()
    stake = int(amount)
    if choice not in LABELS:
        raise ValueError("Выбери ставку: Игрок, Банкир или Ничья")
    if stake <= 0:
        raise ValueError("Сумма должна быть положительной")

    shoe = deck if deck is not None else build_shoe()
    if deck is None:
        secrets.SystemRandom().shuffle(shoe)

    player = [_draw(shoe)]
    banker = [_draw(shoe)]
    player.append(_draw(shoe))
    banker.append(_draw(shoe))
    player_total = _points(player)
    banker_total = _points(banker)
    natural = player_total in (8, 9) or banker_total in (8, 9)

    if not natural:
        player_third: int | None = None
        if player_total <= 5:
            third = _draw(shoe)
            player.append(third)
            player_third = int(third["points"])
        if banker_draws(banker_total, player_third):
            banker.append(_draw(shoe))

    player_total = _points(player)
    banker_total = _points(banker)
    outcome = "player" if player_total > banker_total else "banker" if banker_total > player_total else "tie"
    if choice == "tie":
        payout = round(stake * PAYOUTS["tie"]) if outcome == "tie" else 0
        won = outcome == "tie"
    elif outcome == "tie":
        payout = stake
        won = False
    elif choice == outcome:
        payout = round(stake * PAYOUTS[choice])
        won = True
    else:
        payout = 0
        won = False

    return {
        "game": "baccarat",
        "selection": choice,
        "label": LABELS[choice],
        "odds": PAYOUTS[choice],
        "amount": stake,
        "outcome": outcome,
        "result": outcome,
        "win": won,
        "payout": payout,
        "player_cards": [_public_card(card) for card in player],
        "banker_cards": [_public_card(card) for card in banker],
        "player_points": player_total,
        "banker_points": banker_total,
        "natural": natural,
    }


def options() -> list[dict[str, Any]]:
    return [
        {"code": code, "label": LABELS[code], "odds": PAYOUTS[code], "group": "Ставка"}
        for code in ("player", "banker", "tie")
    ]


__all__ = ["CARD_POINTS", "DECKS", "LABELS", "PAYOUTS", "banker_draws", "build_shoe", "options", "play"]

"""Серверные правила классического блэкджека.

Дилер стоит на мягкой 17; натуральный блэкджек платит 3:2. Стол, shoe,
закрытая карта дилера и все решения хранятся/исполняются на сервере.
"""

from __future__ import annotations

import secrets
from typing import Any

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
TEN_RANKS = frozenset({"10", "J", "Q", "K"})
MAX_HANDS = 4


def make_shoe() -> list[dict[str, str]]:
    """Перемешивает шесть колод; колода не уходит клиенту и не переиспользуется."""
    shoe = [
        {"rank": rank, "suit": suit}
        for _ in range(6)
        for suit in SUITS
        for rank in RANKS
    ]
    secrets.SystemRandom().shuffle(shoe)
    return shoe


def card_value(card: dict[str, str]) -> int:
    rank = card["rank"]
    if rank == "A":
        return 11
    if rank in TEN_RANKS:
        return 10
    return int(rank)


def hand_value(cards: list[dict[str, str]]) -> tuple[int, bool]:
    """Возвращает лучшую сумму и признак soft (туз всё ещё считается за 11)."""
    total = sum(card_value(card) for card in cards)
    aces = sum(card["rank"] == "A" for card in cards)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, bool(aces and total <= 21)


def is_natural(hand: dict[str, Any]) -> bool:
    cards = hand["cards"]
    return (
        len(cards) == 2
        and not hand.get("from_split", False)
        and hand_value(cards)[0] == 21
    )


def _draw(state: dict[str, Any]) -> dict[str, str]:
    if not state["shoe"]:
        state["shoe"] = make_shoe()
    return state["shoe"].pop()


def _dealer_blackjack(state: dict[str, Any]) -> bool:
    cards = state["dealer"]
    return bool(
        len(cards) == 2
        and {cards[0]["rank"], cards[1]["rank"]} & {"A"}
        and any(card["rank"] in TEN_RANKS for card in cards)
    )


def _natural_return(stake: int) -> int:
    # Виртуальная монета неделима: прибыль 3:2 округляем до ближайшей целой,
    # половину монеты вверх.
    profit = (3 * int(stake) + 1) // 2
    return int(stake) + profit


def _hand_result(hand: dict[str, Any], dealer_total: int, dealer_bust: bool) -> tuple[str, int]:
    total = hand_value(hand["cards"])[0]
    if total > 21:
        return "Перебор", 0
    if dealer_bust or total > dealer_total:
        return "Победа", 2 * int(hand["bet"])
    if total == dealer_total:
        return "Ничья", int(hand["bet"])
    return "Проигрыш", 0


def _finish_initial(state: dict[str, Any], dealer_blackjack: bool) -> None:
    player_natural = is_natural(state["hands"][0])
    main_return = 0
    if dealer_blackjack and player_natural:
        label = "Ничья · у обоих блэкджек"
        main_return = int(state["stake"])
        state["hands"][0]["result"] = "Ничья"
    elif player_natural:
        label = "Блэкджек · выплата 3:2"
        main_return = _natural_return(int(state["stake"]))
        state["hands"][0]["result"] = "Блэкджек"
    elif dealer_blackjack:
        label = "У дилера блэкджек"
        state["hands"][0]["result"] = "Проигрыш"
    else:
        state["status"] = "playing"
        state["insurance_pending"] = False
        return

    insurance_return = 3 * int(state["insurance_bet"]) if dealer_blackjack else 0
    payout = main_return + insurance_return
    state["status"] = "settled"
    state["insurance_pending"] = False
    state["dealer_blackjack"] = bool(dealer_blackjack)
    state["payout"] = payout
    state["result"] = {
        "label": label,
        "payout": payout,
        "net": payout - int(state["total_wagered"]),
        "insurance_payout": insurance_return,
        "hands": [{
            "label": state["hands"][0].get("result", "Проигрыш"),
            "bet": int(state["hands"][0]["bet"]),
            "payout": main_return,
            "net": main_return - int(state["hands"][0]["bet"]),
        }],
    }


def _dealer_peek(state: dict[str, Any]) -> None:
    blackjack = _dealer_blackjack(state)
    _finish_initial(state, blackjack)


def new_game(stake: int) -> dict[str, Any]:
    """Случайно раздаёт начальные карты и обрабатывает естественный blackjack."""
    stake = int(stake)
    state: dict[str, Any] = {
        "shoe": make_shoe(),
        "dealer": [],
        "hands": [],
        "active_hand": 0,
        "status": "playing",
        "revision": 0,
        "stake": stake,
        "total_wagered": stake,
        "insurance_bet": 0,
        "insurance_pending": False,
        "dealer_blackjack": False,
        "payout": 0,
        "result": None,
        "created_at": 0,
    }
    player_first = _draw(state)
    dealer_up = _draw(state)
    player_second = _draw(state)
    dealer_hole = _draw(state)
    state["hands"] = [{
        "cards": [player_first, player_second],
        "bet": stake,
        "stood": False,
        "busted": False,
        "from_split": False,
        "split_aces": False,
        "doubled": False,
    }]
    state["dealer"] = [dealer_up, dealer_hole]

    if dealer_up["rank"] == "A":
        state["status"] = "insurance"
        state["insurance_pending"] = True
    elif dealer_up["rank"] in TEN_RANKS:
        _dealer_peek(state)
    elif is_natural(state["hands"][0]):
        _finish_initial(state, False)
    return state


def _play_dealer_and_finish(state: dict[str, Any]) -> None:
    # Если все руки перебрали, дилеру достаточно открыть закрытую карту.
    if any(not hand["busted"] for hand in state["hands"]):
        total, _ = hand_value(state["dealer"])
        while total < 17:
            state["dealer"].append(_draw(state))
            total, _ = hand_value(state["dealer"])
    dealer_total = hand_value(state["dealer"])[0]
    dealer_bust = dealer_total > 21
    results: list[dict[str, Any]] = []
    payout = 0
    for hand in state["hands"]:
        label, hand_payout = _hand_result(hand, dealer_total, dealer_bust)
        hand["result"] = label
        hand["stood"] = True
        payout += hand_payout
        results.append({
            "label": label,
            "bet": int(hand["bet"]),
            "payout": hand_payout,
            "net": hand_payout - int(hand["bet"]),
        })
    insurance_return = 0
    state["payout"] = payout
    state["status"] = "settled"
    state["insurance_pending"] = False
    state["dealer_blackjack"] = False
    state["result"] = {
        "label": "Дилер перебрал" if dealer_bust else f"У дилера {dealer_total}",
        "payout": payout,
        "net": payout - int(state["total_wagered"]),
        "insurance_payout": insurance_return,
        "hands": results,
    }


def _advance(state: dict[str, Any]) -> None:
    for index in range(int(state["active_hand"]), len(state["hands"])):
        hand = state["hands"][index]
        if not hand["stood"] and not hand["busted"]:
            state["active_hand"] = index
            return
    _play_dealer_and_finish(state)


def apply_action(state: dict[str, Any], action: str) -> int:
    """Применяет серверное решение; возвращает дополнительную сумму к списанию."""
    action = str(action).strip().lower()
    status = state["status"]

    def done(extra: int = 0) -> int:
        state["revision"] = int(state.get("revision", 0)) + 1
        return extra

    if status == "insurance":
        if action == "insurance":
            amount = int(state["stake"]) // 2
            if amount <= 0:
                raise ValueError("Для страховки ставка слишком мала")
            state["insurance_bet"] = amount
            state["total_wagered"] += amount
            _dealer_peek(state)
            return done(amount)
        if action == "decline_insurance":
            _dealer_peek(state)
            return done()
        raise ValueError("Сначала реши, брать ли страховку")

    if status != "playing":
        raise ValueError("Эта раздача уже завершена")
    index = int(state["active_hand"])
    hand = state["hands"][index]
    if action == "hit":
        hand["cards"].append(_draw(state))
        total = hand_value(hand["cards"])[0]
        if total > 21:
            hand["busted"] = True
        elif total == 21:
            hand["stood"] = True
        _advance(state)
        return done()
    if action == "stand":
        hand["stood"] = True
        _advance(state)
        return done()
    if action == "double":
        if len(hand["cards"]) != 2 or hand["split_aces"] or hand["doubled"]:
            raise ValueError("Удвоить можно только на первых двух картах")
        extra = int(hand["bet"])
        hand["bet"] += extra
        hand["doubled"] = True
        state["total_wagered"] += extra
        hand["cards"].append(_draw(state))
        total = hand_value(hand["cards"])[0]
        if total > 21:
            hand["busted"] = True
        else:
            hand["stood"] = True
        _advance(state)
        return done(extra)
    if action == "split":
        cards = hand["cards"]
        if len(cards) != 2 or cards[0]["rank"] != cards[1]["rank"]:
            raise ValueError("Разделить можно только пару одинаковых карт")
        if hand["split_aces"] or len(state["hands"]) >= MAX_HANDS:
            raise ValueError("Больше разделить нельзя")
        extra = int(hand["bet"])
        state["total_wagered"] += extra
        rank = cards[0]["rank"]
        first = {**hand, "cards": [cards[0], _draw(state)], "bet": int(hand["bet"]),
                 "stood": rank == "A", "busted": False, "from_split": True,
                 "split_aces": rank == "A", "doubled": False}
        second = {**hand, "cards": [cards[1], _draw(state)], "bet": int(hand["bet"]),
                  "stood": rank == "A", "busted": False, "from_split": True,
                  "split_aces": rank == "A", "doubled": False}
        state["hands"][index:index + 1] = [first, second]
        state["active_hand"] = index
        _advance(state)
        return done(extra)
    raise ValueError("Неизвестное действие блэкджека")


def public_state(game_id: int, state: dict[str, Any], balance: int) -> dict[str, Any]:
    """Скрывает hole card и shoe, пока раунд не закончен."""
    settled = state["status"] == "settled"
    dealer_cards = (
        [dict(card) for card in state["dealer"]]
        if settled
        else [dict(state["dealer"][0]), {"hidden": True}]
    )
    hand_rows = []
    for index, hand in enumerate(state["hands"]):
        total, soft = hand_value(hand["cards"])
        can_split = (
            state["status"] == "playing"
            and index == int(state["active_hand"])
            and len(hand["cards"]) == 2
            and hand["cards"][0]["rank"] == hand["cards"][1]["rank"]
            and not hand["split_aces"]
            and len(state["hands"]) < MAX_HANDS
        )
        hand_rows.append({
            "cards": [dict(card) for card in hand["cards"]],
            "bet": int(hand["bet"]),
            "total": total,
            "soft": soft,
            "busted": bool(hand["busted"]),
            "stood": bool(hand["stood"]),
            "active": state["status"] == "playing" and index == int(state["active_hand"]),
            "split_aces": bool(hand["split_aces"]),
            "result": hand.get("result"),
            "can_split": can_split,
            "can_double": (
                state["status"] == "playing"
                and index == int(state["active_hand"])
                and len(hand["cards"]) == 2
                and not hand["split_aces"]
                and not hand["doubled"]
                and balance >= int(hand["bet"])
            ),
            "can_hit": state["status"] == "playing" and index == int(state["active_hand"]),
            "can_stand": state["status"] == "playing" and index == int(state["active_hand"]),
        })
    insurance_amount = int(state["stake"]) // 2
    return {
        "id": int(game_id),
        "status": str(state["status"]),
        "revision": int(state.get("revision", 0)),
        "stake": int(state["stake"]),
        "total_wagered": int(state["total_wagered"]),
        "payout": int(state["payout"]),
        "active_hand": int(state["active_hand"]),
        "hands": hand_rows,
        "dealer": {
            "cards": dealer_cards,
            "hidden": not settled,
            "total": hand_value(state["dealer"])[0] if settled else None,
            "soft": hand_value(state["dealer"])[1] if settled else False,
        },
        "insurance": {
            "pending": bool(state["insurance_pending"]),
            "amount": insurance_amount,
            "can_take": (
                state["status"] == "insurance"
                and insurance_amount > 0
                and balance >= insurance_amount
            ),
            "taken": int(state["insurance_bet"]),
            "payout": int(state["result"]["insurance_payout"])
            if state.get("result") else 0,
        },
        "result": state.get("result"),
        "balance": int(balance),
    }

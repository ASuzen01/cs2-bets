"""Server-authoritative rules and state machine for the multiplayer Durak game."""
from __future__ import annotations

import secrets
from typing import Any

SUITS = ("♠", "♥", "♦", "♣")
SUIT_NAMES = {"♠": "пики", "♥": "черви", "♦": "бубны", "♣": "трефы"}
RANKS = {6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "В", 12: "Д", 13: "К", 14: "Т"}
VALID_DECKS = {24, 36, 52}
VALID_MODES = {"throwin", "transfer", "neighbors"}
PAYOUT_BPS = {
    2: (10_000, 0),
    3: (6_667, 3_333, 0),
    4: (6_667, 2_222, 1_111, 0),
    5: (5_000, 3_000, 1_500, 500, 0),
    6: (5_000, 2_000, 1_500, 1_000, 500, 0),
}


def validate(deck_size: int, mode: str, players: int, stake: int) -> None:
    if int(deck_size) not in VALID_DECKS:
        raise ValueError("Выбери колоду на 24, 36 или 52 карты")
    if str(mode) not in VALID_MODES:
        raise ValueError("Выбери режим: подкидной, переводной или соседи")
    if not 2 <= int(players) <= 6:
        raise ValueError("За столом должно быть от 2 до 6 игроков")
    if int(stake) < 10:
        raise ValueError("Минимальная ставка — 10 монет")


def _make_deck(size: int) -> list[dict[str, Any]]:
    low = {24: 9, 36: 6, 52: 2}[int(size)]
    return [
        {"rank_value": value, "rank": RANKS.get(value, str(value)), "suit": suit}
        for value in range(low, 15)
        for suit in SUITS
    ]


def _active(state: dict[str, Any]) -> list[int]:
    return [int(p["seat"]) for p in state["players"] if p.get("place") is None]


def _next_seat(state: dict[str, Any], seat: int, *, exclude: set[int] | None = None) -> int | None:
    skipped = exclude or set()
    active = set(_active(state))
    count = len(state["players"])
    for step in range(1, count + 1):
        candidate = (int(seat) + step) % count
        if candidate in active and candidate not in skipped:
            return candidate
    return None


def _clockwise(state: dict[str, Any], start: int, allowed: set[int]) -> list[int]:
    count = len(state["players"])
    return [seat for step in range(count) if (seat := (start + step) % count) in allowed]


def _card_text(card: dict[str, Any]) -> str:
    return f"{card['rank']}{card['suit']}"


def _low_trump_holder(players: list[dict[str, Any]], trump_suit: str) -> int:
    choices = [
        (int(card["rank_value"]), int(player["seat"]))
        for player in players
        for card in player["hand"]
        if card["suit"] == trump_suit
    ]
    return min(choices)[1] if choices else secrets.randbelow(len(players))


def new_game(
    participants: list[dict[str, Any]], *, deck_size: int, mode: str, stake: int,
) -> dict[str, Any]:
    """Deal a game. Participants carry user_id/name/bot; hands stay server-side."""
    validate(deck_size, mode, len(participants), stake)
    deck = _make_deck(int(deck_size))
    secrets.SystemRandom().shuffle(deck)
    trump = dict(deck[-1])
    players = []
    for seat, source in enumerate(participants):
        players.append({
            "seat": seat,
            "user_id": int(source["user_id"]) if source.get("user_id") is not None else None,
            "name": str(source.get("name") or ("Игрок" if seat == 0 else f"Игрок {seat + 1}"))[:40],
            "bot": bool(source.get("bot")),
            "hand": [],
            "place": None,
        })
    # Keep the face-up trump card at the bottom of the stock until the final draw.
    initial_hand = min(6, max(0, (len(deck) - 1) // len(players)))
    for _ in range(initial_hand):
        for player in players:
            player["hand"].append(deck.pop(0))
    attacker = _low_trump_holder(players, trump["suit"])
    defender = _next_seat({"players": players}, attacker)
    return {
        "version": 1,
        "mode": str(mode),
        "deck_size": int(deck_size),
        "stake": int(stake),
        "players": players,
        "stock": deck,
        "trump": trump,
        "trump_suit": str(trump["suit"]),
        "discard_count": 0,
        "table": [],
        "attacker": int(attacker),
        "defender": int(defender if defender is not None else attacker),
        "phase": "lead",
        "turn": int(attacker),
        "queue": [],
        "queue_kind": None,
        "ranked": [],
        "seen_positions": [],
        "draw_reason": None,
        "round": 1,
        "last_event": "Козырь открыт. Начинает игрок с младшим козырем.",
        "last_tokens": {},
        "finished": False,
    }


def _matching_ranks(state: dict[str, Any]) -> set[int]:
    ranks = set()
    for attack in state["table"]:
        ranks.add(int(attack["card"]["rank_value"]))
        if attack.get("cover"):
            ranks.add(int(attack["cover"]["rank_value"]))
    return ranks


def can_beat(card: dict[str, Any], attack: dict[str, Any], trump_suit: str) -> bool:
    if card["suit"] == attack["suit"]:
        return int(card["rank_value"]) > int(attack["rank_value"])
    return card["suit"] == trump_suit and attack["suit"] != trump_suit


def _take_from_hand(player: dict[str, Any], index: int) -> dict[str, Any]:
    try:
        return player["hand"].pop(int(index))
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("Этой карты уже нет на руке") from exc


def _throw_queue(state: dict[str, Any], *, opening: bool = False) -> list[int]:
    defender = int(state["defender"])
    active = set(_active(state)) - {defender}
    players = state["players"]
    if opening:
        return _clockwise(state, (int(state["attacker"]) + 1) % len(players), active)
    if state["mode"] == "neighbors":
        left = _next_seat(state, defender)
        right = None
        if left is not None:
            right = _next_seat(state, left, exclude={defender})
        allowed = {seat for seat in (left, right) if seat is not None and seat in active}
        return _clockwise(state, (defender + 1) % len(players), allowed)
    return _clockwise(state, (int(state["attacker"]) + 1) % len(players), active)


def _start_defense(state: dict[str, Any]) -> None:
    state["phase"] = "defend"
    state["queue"] = []
    state["queue_kind"] = None
    state["turn"] = int(state["defender"])


def _open_throw_window(state: dict[str, Any], *, opening: bool = False) -> None:
    queue = _throw_queue(state, opening=opening)
    # A defender cannot be attacked with more cards than they had when the turn began.
    hand_cap = int(state.get("defender_hand_cap") or 0)
    if hand_cap:
        queue = [seat for seat in queue if len(state["players"][seat]["hand"]) > 0]
    state["queue"] = queue
    state["queue_kind"] = "opening" if opening else "neighbors"
    if queue:
        state["phase"] = "throw"
        state["turn"] = queue[0]
    elif all(entry.get("cover") for entry in state["table"]):
        _finish_round(state, took=False)
    else:
        _start_defense(state)


def _advance_queue(state: dict[str, Any]) -> None:
    if state["queue"]:
        state["queue"].pop(0)
    if state["queue"]:
        state["turn"] = int(state["queue"][0])
        return
    if state["queue_kind"] == "opening":
        _start_defense(state)
    elif all(entry.get("cover") for entry in state["table"]):
        _finish_round(state, took=False)
    else:
        _start_defense(state)


def _next_defender(state: dict[str, Any], old_defender: int) -> int:
    attacker = int(state["attacker"])
    next_seat = _next_seat(state, old_defender, exclude={attacker})
    return int(next_seat if next_seat is not None else old_defender)


def _rank_empty_players(state: dict[str, Any]) -> None:
    if state["stock"]:
        return
    start = (int(state["attacker"]) + 1) % len(state["players"])
    for seat in _clockwise(state, start, set(_active(state))):
        if not state["players"][seat]["hand"]:
            state["players"][seat]["place"] = len(state["ranked"]) + 1
            state["ranked"].append(seat)
    remaining = _active(state)
    if len(remaining) <= 1:
        if remaining:
            last = remaining[0]
            state["players"][last]["place"] = len(state["players"])
            state["ranked"].append(last)
        elif state["ranked"]:
            # Degenerate final sweep: retain deterministic seat order, last slot gets last place.
            last = state["ranked"][-1]
            state["players"][last]["place"] = len(state["players"])
        state["finished"] = True
        state["phase"] = "finished"
        state["turn"] = None
        state["queue"] = []


def _finish_round(state: dict[str, Any], *, took: bool) -> None:
    old_defender = int(state["defender"])
    if took:
        target = state["players"][old_defender]
        for entry in state["table"]:
            target["hand"].append(entry["card"])
            if entry.get("cover"):
                target["hand"].append(entry["cover"])
        state["last_event"] = f"{target['name']} взял карты со стола."
        new_attacker = old_defender
    else:
        state["discard_count"] += sum(1 + int(entry.get("cover") is not None) for entry in state["table"])
        state["last_event"] = f"{state['players'][old_defender]['name']} отбился."
        new_attacker = old_defender
    state["table"] = []
    state["attacker"] = new_attacker
    next_defender = _next_seat(state, new_attacker)
    state["defender"] = int(next_defender if next_defender is not None else new_attacker)

    # Подбор карт: атакующий и дальше по часовой стрелке, до шести карт.
    draw_order = _clockwise(state, new_attacker, set(_active(state)))
    for seat in draw_order:
        hand = state["players"][seat]["hand"]
        while len(hand) < 6 and state["stock"]:
            hand.append(state["stock"].pop(0))
    _rank_empty_players(state)
    if not state["finished"]:
        active = set(_active(state))
        if int(state["attacker"]) not in active:
            next_attacker = _next_seat(state, int(state["attacker"]))
            state["attacker"] = int(next_attacker if next_attacker is not None else min(active))
        if int(state["defender"]) not in active or int(state["defender"]) == int(state["attacker"]):
            next_defender = _next_seat(state, int(state["attacker"]))
            state["defender"] = int(next_defender if next_defender is not None else state["attacker"])
        if not state["stock"]:
            signature = repr((
                int(state["attacker"]),
                int(state["defender"]),
                tuple((int(p["seat"]), tuple((int(c["rank_value"]), str(c["suit"])) for c in p["hand"])) for p in state["players"]),
            ))
            seen = state.setdefault("seen_positions", [])
            if signature in seen or len(seen) >= 4096:
                state["finished"] = True
                state["phase"] = "finished"
                state["turn"] = None
                state["draw_reason"] = "cycle" if signature in seen else "safety_limit"
                state["last_event"] = "Повторилась позиция · ничья, ставки возвращены."
                return
            seen.append(signature)
        state["round"] += 1
        state["phase"] = "lead"
        state["turn"] = int(state["attacker"])
        state["queue"] = []
        state["queue_kind"] = None
        state["defender_hand_cap"] = len(state["players"][int(state["defender"])]["hand"])


def _legal_attack_indices(player: dict[str, Any], state: dict[str, Any], *, opening: bool) -> list[int]:
    if opening:
        return list(range(len(player["hand"])))
    if len(state["table"]) >= min(6, int(state.get("defender_hand_cap") or 6)):
        return []
    ranks = _matching_ranks(state)
    return [i for i, card in enumerate(player["hand"]) if int(card["rank_value"]) in ranks]


def _transfer_targets(state: dict[str, Any]) -> list[int]:
    current = int(state["defender"])
    attacker = int(state["attacker"])
    return [seat for seat in _active(state) if seat not in {current, attacker}]


def legal_actions(state: dict[str, Any], user_id: int) -> dict[str, Any]:
    seat = next((int(p["seat"]) for p in state["players"] if p.get("user_id") == int(user_id)), None)
    result = {"is_turn": False, "phase": state.get("phase"), "attack_indices": [], "defend": [], "can_pass": False, "can_take": False, "can_transfer": False}
    if seat is None or state.get("finished") or seat != state.get("turn"):
        return result
    result["is_turn"] = True
    player = state["players"][seat]
    if state["phase"] == "lead":
        result["attack_indices"] = list(range(len(player["hand"])))
    elif state["phase"] == "throw":
        result["attack_indices"] = _legal_attack_indices(player, state, opening=False)
        result["can_pass"] = True
    elif state["phase"] == "defend":
        result["can_take"] = True
        for attack_i, entry in enumerate(state["table"]):
            if entry.get("cover") is None:
                result["defend"] = [
                    {"hand_index": hand_i, "attack_index": attack_i}
                    for hand_i, card in enumerate(player["hand"])
                    if can_beat(card, entry["card"], state["trump_suit"])
                ]
                break
        transfer_open = bool(
            state["mode"] == "transfer"
            and _transfer_targets(state)
            and not any(entry.get("cover") for entry in state["table"])
            and len(state["table"]) < min(6, int(state.get("defender_hand_cap") or 6))
        )
        if transfer_open:
            rank_values = {int(entry["card"]["rank_value"]) for entry in state["table"]}
            result["transfer_indices"] = [i for i, c in enumerate(player["hand"]) if int(c["rank_value"]) in rank_values]
            result["can_transfer"] = bool(result["transfer_indices"])
    return result


def _move_card(player: dict[str, Any], card_index: int) -> dict[str, Any]:
    return _take_from_hand(player, card_index)


def act(state: dict[str, Any], seat: int, action: str, *, card_index: int | None = None, attack_index: int | None = None) -> dict[str, Any]:
    """Validate and apply exactly one action. Invalid or out-of-turn actions raise ValueError."""
    if state.get("finished"):
        raise ValueError("Партия уже завершена")
    if int(state.get("turn", -1)) != int(seat):
        raise ValueError("Сейчас ход другого игрока")
    player = state["players"][int(seat)]
    if player.get("place") is not None:
        raise ValueError("Вы уже завершили партию")

    if action in {"attack", "throw"}:
        opening = action == "attack" and state["phase"] == "lead"
        if opening:
            if int(seat) != int(state["attacker"]):
                raise ValueError("Первую карту кладёт атакующий")
        elif action == "throw" and state["phase"] == "throw":
            legal = _legal_attack_indices(player, state, opening=False)
            if card_index not in legal:
                raise ValueError("Подкинуть можно только карту того же достоинства")
        else:
            raise ValueError("Сейчас нельзя положить карту")
        card = _move_card(player, int(card_index if card_index is not None else -1))
        state["table"].append({"card": card, "cover": None, "by": int(seat)})
        if opening:
            defender = int(state["defender"])
            state["defender_hand_cap"] = min(6, len(state["players"][defender]["hand"]))
            state["last_event"] = f"{player['name']} начал атаку."
            _open_throw_window(state, opening=True)
        else:
            state["last_event"] = f"{player['name']} подкинул {_card_text(card)}."
            _advance_queue(state)
        return state

    if action == "pass" and state["phase"] == "throw":
        state["last_event"] = f"{player['name']} пропускает подкидывание."
        _advance_queue(state)
        return state

    if action == "defend" and state["phase"] == "defend":
        target = next((entry for entry in state["table"] if entry.get("cover") is None), None)
        if target is None:
            raise ValueError("Все карты уже отбиты")
        card = _move_card(player, int(card_index if card_index is not None else -1))
        if not can_beat(card, target["card"], state["trump_suit"]):
            player["hand"].append(card)
            raise ValueError("Этой картой нельзя отбиться")
        target["cover"] = card
        state["last_event"] = f"{player['name']} отбивается картой {_card_text(card)}."
        _open_throw_window(state)
        return state

    if action == "take" and state["phase"] == "defend":
        _finish_round(state, took=True)
        return state

    if action == "transfer" and state["phase"] == "defend":
        if not legal_actions(state, int(player.get("user_id") or -1)).get("can_transfer"):
            # Bot seats have no Telegram id; validate the conditions directly below.
            if state["mode"] != "transfer" or not _transfer_targets(state) or any(e.get("cover") for e in state["table"]):
                raise ValueError("Перевод сейчас невозможен")
        card = _move_card(player, int(card_index if card_index is not None else -1))
        ranks = {int(entry["card"]["rank_value"]) for entry in state["table"]}
        if int(card["rank_value"]) not in ranks:
            player["hand"].append(card)
            raise ValueError("Для перевода нужна карта такого же достоинства")
        if len(state["table"]) >= min(6, int(state.get("defender_hand_cap") or 6)):
            player["hand"].append(card)
            raise ValueError("Нельзя увеличить число карт в этой атаке")
        old_defender = int(state["defender"])
        state["table"].append({"card": card, "cover": None, "by": int(seat)})
        state["defender"] = _next_defender(state, old_defender)
        state["last_event"] = f"{player['name']} переводит атаку на {state['players'][state['defender']]['name']}."
        _open_throw_window(state)
        return state

    raise ValueError("Недопустимое действие")


def _bot_choice(state: dict[str, Any], seat: int) -> tuple[str, int | None]:
    player = state["players"][seat]
    if state["phase"] == "lead":
        candidates = sorted(range(len(player["hand"])), key=lambda i: (player["hand"][i]["suit"] == state["trump_suit"], player["hand"][i]["rank_value"]))
        return "attack", candidates[0] if candidates else None
    if state["phase"] == "throw":
        legal = _legal_attack_indices(player, state, opening=False)
        if legal:
            chosen = min(legal, key=lambda i: (player["hand"][i]["suit"] == state["trump_suit"], player["hand"][i]["rank_value"]))
            return "throw", chosen
        return "pass", None
    if state["phase"] == "defend":
        attack = next((entry for entry in state["table"] if entry.get("cover") is None), None)
        if attack:
            candidates = [i for i, card in enumerate(player["hand"]) if can_beat(card, attack["card"], state["trump_suit"])]
            if candidates:
                chosen = min(candidates, key=lambda i: (player["hand"][i]["suit"] == state["trump_suit"], player["hand"][i]["rank_value"]))
                return "defend", chosen
        return "take", None
    raise ValueError("Неизвестный ход бота")


def run_bots(state: dict[str, Any], *, limit: int = 200) -> dict[str, Any]:
    """Automatically resolve consecutive bot turns until a human may act."""
    steps = 0
    while not state.get("finished") and steps < limit:
        seat = state.get("turn")
        if seat is None or not state["players"][int(seat)].get("bot"):
            break
        action, card = _bot_choice(state, int(seat))
        act(state, int(seat), action, card_index=card)
        steps += 1
    return state


def public_game(state: dict[str, Any], *, game_id: int, user_id: int, status: str, code: str | None = None, visibility: str = "private") -> dict[str, Any]:
    own = next((p for p in state["players"] if p.get("user_id") == int(user_id)), None)
    if own is None:
        raise ValueError("Вы не участник этой партии")
    me = int(own["seat"])
    players = [{
        "seat": int(p["seat"]),
        "name": str(p["name"]),
        "bot": bool(p.get("bot")),
        "me": int(p["seat"]) == me,
        "hand_count": len(p["hand"]),
        "place": p.get("place"),
    } for p in state["players"]]
    actions = legal_actions(state, int(user_id)) if status == "active" else {}
    return {
        "id": int(game_id),
        "code": code,
        "visibility": visibility,
        "status": status,
        "mode": str(state["mode"]),
        "table_type": str(state.get("table_type") or "online"),
        "deck_size": int(state["deck_size"]),
        "stake": int(state["stake"]),
        "players_count": len(state["players"]),
        "player_count": int(state.get("player_count") or len(state["players"])),
        "players": players,
        "your_seat": me,
        "hand": [{**card, "text": _card_text(card)} for card in own["hand"]] if status == "active" else [],
        "stock_count": len(state.get("stock") or []),
        "trump": {**state["trump"], "text": _card_text(state["trump"])} if state.get("trump") else None,
        "trump_suit": state.get("trump_suit"),
        "discard_count": int(state.get("discard_count", 0)),
        "table": [{
            "card": {**entry["card"], "text": _card_text(entry["card"])},
            "cover": {**entry["cover"], "text": _card_text(entry["cover"])} if entry.get("cover") else None,
            "by": int(entry["by"]),
        } for entry in state.get("table", [])],
        "attacker": state.get("attacker"),
        "defender": state.get("defender"),
        "phase": state.get("phase"),
        "turn": state.get("turn"),
        "is_your_turn": status == "active" and state.get("turn") == me,
        "actions": actions,
        "round": int(state.get("round", 1)),
        "last_event": str(state.get("last_event") or ""),
        "finished": bool(state.get("finished")),
        "draw_reason": state.get("draw_reason"),
        "rankings": [{"seat": int(seat), "place": int(state["players"][int(seat)]["place"])} for seat in state.get("ranked", [])],
        "your_place": own.get("place"),
        "payout": int((state.get("payouts") or {}).get(str(user_id), 0)),
        "payouts": None,
    }


def payouts_by_place(player_count: int, stake: int) -> list[int]:
    """Split the full entry pool; indivisible coins go to largest fractional shares."""
    count = int(player_count)
    pool = count * int(stake)
    numerators = [pool * share for share in PAYOUT_BPS[count]]
    payouts = [value // 10_000 for value in numerators]
    remainder = pool - sum(payouts)
    # Largest-remainder apportionment keeps the integer payouts closest to the
    # requested percentages. Equal fractions are awarded from the better place.
    order = sorted(range(count), key=lambda i: (numerators[i] % 10_000, -i), reverse=True)
    for index in order[:remainder]:
        payouts[index] += 1
    return payouts


def payout_for_place(player_count: int, stake: int, place: int) -> int:
    return payouts_by_place(player_count, stake)[int(place) - 1]


__all__ = [
    "PAYOUT_BPS", "SUIT_NAMES", "VALID_DECKS", "VALID_MODES", "act", "can_beat",
    "new_game", "payout_for_place", "public_game", "run_bots", "validate",
]

from __future__ import annotations

from app.services import durak


def participants(count: int):
    return [{"user_id": 101, "name": "Гость"}] + [
        {"name": f"Бот {i}", "bot": True} for i in range(1, count)
    ]


def test_deck_sizes_keep_the_face_up_trump_out_of_hands() -> None:
    for size, low in ((24, 9), (36, 6), (52, 2)):
        state = durak.new_game(participants(6), deck_size=size, mode="throwin", stake=10)
        assert sum(len(player["hand"]) for player in state["players"]) <= size - 1
        assert state["trump"] not in [card for player in state["players"] for card in player["hand"]]
        assert min(card["rank_value"] for player in state["players"] for card in player["hand"]) >= low
        assert state["stock"][-1] == state["trump"]


def test_trump_and_suit_rules_for_defending() -> None:
    attack = {"rank_value": 10, "suit": "♠"}
    assert durak.can_beat({"rank_value": 11, "suit": "♠"}, attack, "♥")
    assert not durak.can_beat({"rank_value": 9, "suit": "♠"}, attack, "♥")
    assert durak.can_beat({"rank_value": 6, "suit": "♥"}, attack, "♥")
    assert not durak.can_beat({"rank_value": 14, "suit": "♦"}, attack, "♥")
    assert not durak.can_beat({"rank_value": 9, "suit": "♠"}, {"rank_value": 10, "suit": "♥"}, "♥")


def test_neighbor_mode_allows_everyone_on_opening_then_only_defender_neighbors() -> None:
    state = durak.new_game(participants(4), deck_size=36, mode="neighbors", stake=10)
    state["attacker"] = 0
    state["defender"] = 2
    assert durak._throw_queue(state, opening=True) == [1, 3, 0]
    assert durak._throw_queue(state, opening=False) == [3, 0]


def _transfer_state(target_hand_size: int):
    state = durak.new_game(participants(3), deck_size=24, mode="transfer", stake=10)
    state["mode"] = "transfer"
    state["attacker"] = 0
    state["defender"] = 1
    state["turn"] = 1
    state["phase"] = "defend"
    state["defender_hand_cap"] = 6
    state["players"][1]["user_id"] = 202
    state["players"][1]["hand"] = [
        {"rank_value": 10, "rank": "10", "suit": "♠"},
        {"rank_value": 10, "rank": "10", "suit": "♥"},
    ]
    state["players"][2]["hand"] = [
        {"rank_value": 7 + i, "rank": str(7 + i), "suit": "♦"} for i in range(target_hand_size)
    ]
    state["table"] = [{
        "card": {"rank_value": 10, "rank": "10", "suit": "♣"},
        "cover": None,
        "by": 0,
    }]
    return state


def test_transfer_is_disabled_when_next_defender_has_too_few_cards() -> None:
    state = _transfer_state(target_hand_size=1)
    actions = durak.legal_actions(state, 202)

    assert actions["is_turn"]
    assert not actions["can_transfer"]
    assert actions.get("transfer_indices", []) == []
    try:
        durak.act(state, 1, "transfer", card_index=0)
    except ValueError as exc:
        assert "не хватит карт" in str(exc)
    else:
        raise AssertionError("Transfer must be rejected if the next defender cannot take the attack")
    assert len(state["table"]) == 1
    assert len(state["players"][1]["hand"]) == 2


def test_transfer_uses_new_defenders_hand_as_the_attack_cap() -> None:
    state = _transfer_state(target_hand_size=2)
    actions = durak.legal_actions(state, 202)
    assert actions["can_transfer"]
    assert actions["transfer_indices"] == [0, 1]

    durak.act(state, 1, "transfer", card_index=0)

    assert state["defender"] == 2
    assert state["defender_hand_cap"] == 2
    assert len(state["table"]) == 2
    assert durak._legal_attack_indices(state["players"][1], state, opening=False) == []


def test_transfer_to_player_with_more_than_six_cards_keeps_six_card_attack_limit() -> None:
    state = _transfer_state(target_hand_size=8)
    assert durak.legal_actions(state, 202)["can_transfer"]

    durak.act(state, 1, "transfer", card_index=0)

    assert state["defender"] == 2
    assert len(state["players"][2]["hand"]) == 8
    assert state["defender_hand_cap"] == 6
    while len(state["table"]) < 6:
        state["table"].append({"card": state["table"][0]["card"], "cover": None, "by": 0})
    assert durak._legal_attack_indices(state["players"][1], state, opening=False) == []


def test_rank_payout_schedules_and_rounding_remainder() -> None:
    expected = {
        2: [100, 0],
        3: [100, 50, 0],
        4: [133, 45, 22, 0],
        5: [125, 75, 38, 12, 0],
        6: [150, 60, 45, 30, 15, 0],
    }
    for count, values in expected.items():
        payouts = durak.payouts_by_place(count, 50)
        assert payouts == values
        assert sum(payouts) == count * 50


def test_server_bot_players_can_finish_a_full_multiplayer_game() -> None:
    for count in range(2, 7):
        for mode in durak.VALID_MODES:
            state = durak.new_game(
                [{"name": f"Бот {i + 1}", "bot": True} for i in range(count)],
                deck_size=24,
                mode=mode,
                stake=10,
            )
            state["table_type"] = "bots"
            for _ in range(100):
                durak.run_bots(state, limit=2_000)
                if state["finished"]:
                    break
            assert state["finished"]
            if state.get("draw_reason"):
                assert not state["ranked"]
                assert state["phase"] == "finished"
            else:
                assert sorted(int(player["place"]) for player in state["players"]) == list(range(1, count + 1))


def test_public_game_never_exposes_other_hands() -> None:
    state = durak.new_game(participants(3), deck_size=36, mode="throwin", stake=100)
    payload = durak.public_game(state, game_id=1, user_id=101, status="active")
    assert payload["hand"] == [{**card, "text": f"{card['rank']}{card['suit']}"} for card in state["players"][0]["hand"]]
    assert all("hand" not in player for player in payload["players"])

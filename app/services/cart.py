"""Корзина экспресса: черновик исходов в FSM-состоянии пользователя."""

from __future__ import annotations

from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


class ExpressFlow(StatesGroup):
    collecting = State()


async def get_draft(state: FSMContext) -> list[dict[str, Any]]:
    data = await state.get_data()
    return list(data.get("legs") or [])


async def save_draft(state: FSMContext, legs: list[dict[str, Any]]) -> None:
    await state.set_state(ExpressFlow.collecting)
    await state.update_data(legs=legs)


async def clear_draft(state: FSMContext) -> None:
    await state.clear()


def leg_view(match: Any, side: str, short_tournament) -> dict[str, Any]:
    return {
        "match_id": int(match["id"]),
        "side": side,
        "odds": float(match["odds_a"] if side == "a" else match["odds_b"]),
        "team": str(match["team_a"] if side == "a" else match["team_b"]),
        "match": short_tournament(str(match["tournament"])),
    }

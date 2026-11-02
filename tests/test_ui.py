"""Тесты кнопочного интерфейса: клавиатуры, корзина экспресса, хелперы."""

from __future__ import annotations

from typing import Any

import pytest

from app import keyboards
from app.handlers.ui import _amounts
from app.services.cart import ExpressFlow, clear_draft, get_draft, leg_view, save_draft


class FakeState:
    """Минимальная замена FSMContext (без Redis/MemoryStorage)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.state: str | None = None

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, state: Any) -> None:
        self.state = str(state)

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


MATCH = {
    "id": 7,
    "tournament": "Counter-Strike. IEM Пекин. Групповой этап. Bo3",
    "team_a": "Aurora",
    "team_b": "G2 Esports",
    "start_time": 1_760_000_000,
    "odds_a": 1.65,
    "odds_b": 2.25,
    "status": "open",
}


def callbacks(markup) -> list[str]:  # noqa: ANN001
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def labels(markup) -> list[str]:  # noqa: ANN001
    return [button.text for row in markup.inline_keyboard for button in row]


# ------------------------------------------------------------------ клавиатуры
def test_main_menu_has_all_sections() -> None:
    menu = callbacks(keyboards.main_menu_kb(is_admin=False))
    for code in ("ml:0", "b", "bn", "xc", "tp", "hs", "rl"):
        assert code in menu
    assert "ast" not in menu, "админские кнопки не должны утекать обычным игрокам"


def test_admin_menu_extends() -> None:
    menu = callbacks(keyboards.main_menu_kb(is_admin=True))
    assert {"as", "ast", "ap"} <= set(menu)


def test_match_card_buttons_for_both_sides() -> None:
    buttons = callbacks(keyboards.match_kb(MATCH))
    assert "ms:7:a" in buttons and "ms:7:b" in buttons
    assert "xa:7:a" in buttons and "xa:7:b" in buttons
    assert any(label.startswith("1️⃣ Aurora") for label in labels(keyboards.match_kb(MATCH)))


def test_amounts_keyboard_callback_context() -> None:
    markup = keyboards.amounts_kb(
        [50, 100],
        lambda amount: f"ma:7:a:{amount}",
        manual_callback="ma_manual",
        back_callback="mc:7",
        all_callback="ma:7:a:all",
    )
    data = callbacks(markup)
    assert "ma:7:a:50" in data and "ma:7:a:100" in data
    assert {"ma_manual", "mc:7", "ma:7:a:all"} <= set(data)


def test_match_card_hides_shifted_presets() -> None:
    assert _amounts(1250) == [50, 100, 250, 500, 1000]
    assert _amounts(120) == [50, 100]
    assert _amounts(5) == [10], "минимальная сумма показывается всегда"


def test_cart_keyboard_has_amounts_and_clear() -> None:
    legs = [
        {"match_id": 7, "side": "a", "odds": 1.65, "team": "Aurora", "match": "IEM Пекин"},
        {"match_id": 8, "side": "b", "odds": 2.8, "team": "TYLOO", "match": "IEM Пекин"},
    ]
    markup = keyboards.cart_kb(legs, [100, 250])
    data = callbacks(markup)
    assert "xm:100" in data and "xm:250" in data
    assert {"xclear", "ml:0", "menu", "xm_manual"} <= set(data)


# -------------------------------------------------------------------- корзина
@pytest.mark.asyncio
async def test_draft_add_and_clear() -> None:
    state = FakeState()
    assert await get_draft(state) == []

    leg = leg_view(MATCH, "b", lambda tournament: "IEM Пекин")
    await save_draft(state, [leg])
    assert state.state == str(ExpressFlow.collecting)

    stored = await get_draft(state)
    assert stored[0]["match_id"] == 7
    assert stored[0]["side"] == "b"
    assert stored[0]["odds"] == pytest.approx(2.25)
    assert stored[0]["team"] == "G2 Esports"

    await clear_draft(state)
    assert await get_draft(state) == []

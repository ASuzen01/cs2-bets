"""The /start greeting uses the app's four-leaf-clover brand emblem."""

from __future__ import annotations

from PIL import Image

from app.assets import MASCOT_PATH, WELCOME_EMBLEM_PATH
from app.services.notify import send_with_mascot


def test_welcome_emblem_is_a_valid_png_and_distinct_from_game_mascot() -> None:
    assert WELCOME_EMBLEM_PATH.is_file()
    assert WELCOME_EMBLEM_PATH != MASCOT_PATH
    with Image.open(WELCOME_EMBLEM_PATH) as image:
        assert image.format == "PNG"
        assert image.size == (768, 768)


async def test_welcome_can_send_custom_emblem() -> None:
    class FakeBot:
        photo = None

        async def send_photo(self, **kwargs):
            self.photo = kwargs["photo"].path

    bot = FakeBot()
    await send_with_mascot(bot, 123, "Привет!", photo_path=WELCOME_EMBLEM_PATH)
    assert bot.photo == str(WELCOME_EMBLEM_PATH)

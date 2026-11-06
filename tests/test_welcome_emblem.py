"""Every bot image using MASCOT_PATH now uses the app's clover emblem."""

from __future__ import annotations

from PIL import Image

from app.assets import MASCOT_PATH
from app.services.notify import send_with_mascot


def test_shared_mascot_asset_is_the_green_clover_emblem() -> None:
    assert MASCOT_PATH.is_file()
    with Image.open(MASCOT_PATH) as image:
        assert image.format == "PNG"
        assert image.size == (768, 768)


async def test_shared_mascot_sender_uses_the_clover_emblem() -> None:
    class FakeBot:
        photo = None

        async def send_photo(self, **kwargs):
            self.photo = kwargs["photo"].path

    bot = FakeBot()
    await send_with_mascot(bot, 123, "Привет!")
    assert bot.photo == str(MASCOT_PATH)

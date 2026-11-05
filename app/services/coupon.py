"""Купон ставки картинкой: игрок отправляет его в чат и хвастается перед друзьями.

Рисуем на сервере (Pillow), а не в браузере: картинка одинаковая на всех телефонах,
не зависит от вебвью и сразу уходит в чат через бота — её можно переслать, она не
потеряется при закрытии мини-аппа.

Шрифты лежат в `app/assets/fonts` (DejaVu — свободная лицензия), поэтому образ
не зависит от системных пакетов.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Final

from PIL import Image, ImageDraw, ImageFont

from app.assets import MASCOT_PATH

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
SYSTEM_FONTS = (
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/TTF"),
)

W: Final = 1080
H: Final = 1350
PAD: Final = 72

BG_TOP: Final = (15, 23, 32)
BG_BOTTOM: Final = (24, 34, 46)
CARD: Final = (26, 36, 49)
CARD_EDGE: Final = (38, 53, 70)
TEXT: Final = (234, 240, 246)
MUTED: Final = (132, 150, 168)
OK: Final = (55, 178, 77)
BAD: Final = (224, 49, 49)
LIVE: Final = (77, 171, 247)
GOLD: Final = (255, 210, 63)

STATUS: Final[dict[str, tuple[str, tuple[int, int, int]]]] = {
    "won": ("ЗАШЛА", OK),
    "lost": ("НЕ ЗАШЛА", BAD),
    "void": ("ВОЗВРАТ", MUTED),
    "open": ("В ИГРЕ", LIVE),
}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Шрифт с кириллицей: сначала свой из репозитория, потом системный."""
    for folder in (FONT_DIR, *SYSTEM_FONTS):
        path = folder / name
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def _bold(size: int):
    return _font("DejaVuSans-Bold.ttf", size)


def _regular(size: int):
    return _font("DejaVuSans.ttf", size)


def _money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " 🪙"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, limit: int) -> list[str]:
    """Перенос по словам: длинные названия команд не должны вылезать за карточку."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= limit:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _gradient() -> Image.Image:
    image = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(H):
        ratio = y / H
        colour = tuple(
            round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3)
        )
        draw.line([(0, y), (W, y)], fill=colour)
    # мягкое свечение сверху — как подсветка на экранах приложения
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse([W // 2 - 520, -380, W // 2 + 520, 420], fill=(77, 171, 247, 26))
    return Image.alpha_composite(image.convert("RGBA"), glow)


def _pill(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, colour: tuple[int, int, int]
) -> tuple[int, int]:
    font = _bold(34)
    width = draw.textlength(text, font=font)
    x, y = xy
    box = [x, y, x + width + 56, y + 66]
    draw.rounded_rectangle(box, radius=33, fill=(*colour, 46), outline=(*colour, 150), width=3)
    draw.text((x + 28, y + 33), text, font=font, fill=TEXT, anchor="lm")
    return int(box[2]), int(box[3])


def _coin(draw: ImageDraw.ImageDraw, xy: tuple[int, int], radius: int = 17) -> None:
    x, y = xy
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=GOLD)
    draw.ellipse(
        [x - radius + 5, y - radius + 5, x + radius - 5, y + radius - 5],
        outline=(180, 140, 20),
        width=2,
    )


def render_coupon(item: dict[str, Any]) -> bytes:
    """PNG купона. `item` — элемент из `bets_history` (одиночная ставка или экспресс)."""
    image = _gradient()
    draw = ImageDraw.Draw(image)

    # ---- шапка: маскот и бренд
    try:
        mascot = Image.open(MASCOT_PATH).convert("RGBA").resize((132, 132))
        image.alpha_composite(mascot, (PAD, PAD - 6))
        draw = ImageDraw.Draw(image)
    except Exception:      # нет картинки — купон всё равно должен собраться
        draw.ellipse([PAD, PAD, PAD + 132, PAD + 132], fill=CARD)

    draw.text((PAD + 164, PAD + 10), "Добрая лудка", font=_bold(52), fill=TEXT)
    draw.text(
        (PAD + 164, PAD + 76), "ставки на CS2 · футбол · хоккей", font=_regular(30), fill=MUTED
    )

    # ---- статус
    kind = "express" if item.get("kind") == "express" else "single"
    # «рассчитанная» ставка приходит без поля status — смотрим на result;
    # если исхода ещё нет (open/live/awaiting), купон честно остаётся «в игре»
    raw_result = str(item.get("result") or "")
    result = raw_result if raw_result in ("won", "lost", "void") else "open"
    label, colour = STATUS.get(result, ("КУПОН", LIVE))
    _pill(draw, (PAD, 236), label, colour)

    # ---- сначала считаем высоту текста, чтобы карточка была ровно по содержимому
    inner_left = PAD + 44
    inner_width = W - 2 * PAD - 88
    font_title = _bold(46)

    title = str(item.get("title") or "") or ("Экспресс" if kind == "express" else "Ставка")
    title_lines = _wrap(draw, title, font_title, inner_width)[:2]
    subtitle = str(item.get("subtitle") or item.get("tournament") or "")
    subtitle_lines = _wrap(draw, subtitle, _regular(30), inner_width)[:2] if subtitle else []
    if kind == "express":
        subtitle_lines = []      # у экспресса вместо подписи — список исходов

    legs = (item.get("legs") or []) if kind == "express" else []
    shown_legs = legs[:4]

    content_h = 44 + len(title_lines) * 58 + len(subtitle_lines) * 40
    if shown_legs:
        content_h += 22 + len(shown_legs) * 44 + (40 if len(legs) > 4 else 0)

    # блок «карточка + выплата» центрируем между плашкой статуса и подписью:
    # у короткой ставки не остаётся дыры внизу, у длинного экспресса — не лезет на подпись
    numbers_h = 210
    block_h = content_h + numbers_h + 24 + 36 + 200
    area_top = 302
    area_bottom = H - 140
    card_top = max(340, area_top + max(0, (area_bottom - area_top - block_h) // 2))
    card_bottom = card_top + content_h + numbers_h + 24

    draw.rounded_rectangle(
        [PAD, card_top, W - PAD, card_bottom], radius=44, fill=CARD, outline=CARD_EDGE, width=3
    )

    y = card_top + 44
    for line in title_lines:
        draw.text((inner_left, y), line, font=font_title, fill=TEXT)
        y += 58
    for line in subtitle_lines:
        draw.text((inner_left, y), line, font=_regular(30), fill=MUTED)
        y += 40

    if shown_legs:
        y += 22
        for leg in shown_legs:
            mark = {"won": "✓", "lost": "✕", "void": "•"}.get(str(leg.get("result") or ""), "•")
            text = f"{mark} {leg.get('team', '')} · {float(leg.get('odds') or 0):.2f}"
            draw.text((inner_left, y), text[:46], font=_regular(31), fill=TEXT)
            y += 44
        if len(legs) > 4:
            draw.text((inner_left, y), f"и ещё {len(legs) - 4}", font=_regular(28), fill=MUTED)

    # ---- кэф и ставка
    numbers_top = card_bottom - 24 - numbers_h
    draw.text((inner_left, numbers_top + 14), "КОЭФИЦИЕНТ", font=_regular(26), fill=MUTED)
    draw.text(
        (inner_left, numbers_top + 50),
        f"× {float(item.get('odds') or 0):.2f}",
        font=_bold(64),
        fill=TEXT,
    )

    stake_x = inner_left + 380
    draw.text((stake_x, numbers_top + 14), "СТАВКА", font=_regular(26), fill=MUTED)
    stake_text = f"{int(item.get('stake') or 0):,}".replace(",", " ")
    stake_width = draw.textlength(stake_text, font=_bold(64))
    _coin(draw, (int(stake_x + 18), numbers_top + 96), 16)
    draw.text((int(stake_x + 46), numbers_top + 50), stake_text, font=_bold(64), fill=TEXT)
    del stake_width

    # ---- выплата
    paid = int(item.get("payout") or 0)
    potential = int(item.get("potential") or 0)
    amount = paid if paid and result in ("won", "lost", "void") else potential
    caption = "ВЫПЛАТА" if result == "won" else ("В ИГРЕ" if result == "open" else "ИТОГ")
    if result == "lost":
        caption = "ПРОИГРЫШ"
        amount = int(item.get("stake") or 0)

    box_top = card_bottom + 36
    box_bottom = box_top + 200
    fill = (24, 48, 33) if result == "won" else ((44, 23, 23) if result == "lost" else CARD)
    edge = (47, 107, 61) if result == "won" else ((122, 43, 43) if result == "lost" else CARD_EDGE)
    draw.rounded_rectangle(
        [PAD, box_top, W - PAD, box_bottom], radius=40, fill=fill, outline=edge, width=3
    )
    draw.text((inner_left, box_top + 34), caption, font=_regular(28), fill=MUTED)
    amount_text = f"{amount:,}".replace(",", " ")
    _coin(draw, (inner_left + 20, box_top + 136), 20)
    draw.text(
        (inner_left + 58, box_top + 92),
        amount_text,
        font=_bold(68),
        fill=OK if result == "won" else (TEXT if result == "open" else BAD),
    )
    if result == "won":
        net = paid - int(item.get("stake") or 0)
        draw.text(
            (W - PAD - 44, box_top + 136),
            f"+{net:,}".replace(",", " ") + " чистыми",
            font=_regular(28),
            fill=OK,
            anchor="rm",
        )

    # ---- подпись
    draw.text(
        (W // 2, H - 96), "виртуальные монеты · без реальных ставок",
        font=_regular(28), fill=MUTED, anchor="mm",
    )
    draw.text((W // 2, H - 52), "Добрая лудка · мини-апп в Telegram",
              font=_bold(28), fill=TEXT, anchor="mm")

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# «bet» — одиночная ставка, «express» — экспресс: так купон называется в ссылке
COUPON_KINDS = {"bet": "single", "express": "express"}


async def item_of(db: Any, user_id: int, kind: str, bet_id: int) -> dict[str, Any] | None:
    """Купон только для своей ставки: чужие id не отдаём ни картинкой, ни в чат."""
    from app.web.payloads import bets_history

    wanted = COUPON_KINDS.get(kind)
    if wanted is None:
        return None
    payload = await bets_history(db, user_id)
    for item in list(payload.get("open") or []) + list(payload.get("settled") or []):
        if int(item.get("id") or 0) == int(bet_id) and str(item.get("kind") or "single") == wanted:
            return item
    return None


__all__ = ["COUPON_KINDS", "item_of", "render_coupon"]

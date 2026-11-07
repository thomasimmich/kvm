"""Draw element boxes and ids onto a screenshot (dashboard and traces)."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .types import UIElement

_COLORS = {"text": (37, 99, 235), "icon": (234, 88, 12)}


def annotate(
    image: Image.Image,
    elements: list[UIElement],
    highlight: int | None = None,
    new_ids: list[int] | None = None,
) -> Image.Image:
    img = image.convert("RGB").copy()
    draw = ImageDraw.Draw(img, "RGBA")
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    new = set(new_ids or [])
    for e in elements:
        color = _COLORS.get(e.kind, (100, 100, 100))
        width = 2
        if e.id == highlight:
            color, width = (22, 163, 74), 4
        elif e.id in new:
            color = (219, 39, 119)
        draw.rectangle(e.bbox, outline=color + (255,), width=width)
        tag = str(e.id)
        tx, ty = e.bbox[0], max(0, e.bbox[1] - 15)
        tw = draw.textlength(tag, font=font)
        draw.rectangle((tx, ty, tx + tw + 4, ty + 15), fill=color + (220,))
        draw.text((tx + 2, ty + 1), tag, fill=(255, 255, 255), font=font)
    return img

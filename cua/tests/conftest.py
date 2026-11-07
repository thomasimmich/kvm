from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from kvm_cua.perception.types import UIElement

FIXTURES = Path(__file__).parent / "fixtures"


def make_screen(
    labels: list[tuple[str, tuple[int, int, int, int]]], size=(800, 600), bg=(240, 240, 240)
) -> Image.Image:
    """A synthetic screen: one drawn box per label, so screens with different labels differ visually."""
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    for text, box in labels:
        d.rectangle(box, outline=(0, 0, 0), fill=(255, 255, 255), width=2)
        d.text((box[0] + 6, box[1] + 6), text, fill=(0, 0, 0))
    return img


def elements(labels: list[tuple[str, tuple[int, int, int, int]]]) -> list[UIElement]:
    return [
        UIElement(id=i, kind="text", label=t, bbox=b, source="fixture") for i, (t, b) in enumerate(labels)
    ]


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES

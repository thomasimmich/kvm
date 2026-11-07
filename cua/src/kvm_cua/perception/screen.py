"""Screen comparison: did an action change anything, and what is new?"""

from __future__ import annotations

import numpy as np
from PIL import Image

from .types import UIElement


def dhash(image: Image.Image, size: int = 16) -> int:
    """Difference hash: robust to compression noise, sensitive to layout changes."""
    g = np.asarray(image.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR), dtype=np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def changed_fraction(a: Image.Image, b: Image.Image, threshold: int = 24) -> float:
    """Share of pixels that changed noticeably (downscaled, grayscale)."""
    if a.size != b.size:
        return 1.0
    small = (max(1, a.size[0] // 8), max(1, a.size[1] // 8))
    ga = np.asarray(a.convert("L").resize(small), dtype=np.int16)
    gb = np.asarray(b.convert("L").resize(small), dtype=np.int16)
    return float((np.abs(ga - gb) > threshold).mean())


def screens_differ(a: Image.Image, b: Image.Image, min_fraction: float = 0.002) -> bool:
    """True when the screen changed beyond video noise (a blinking caret alone stays below the default)."""
    return changed_fraction(a, b) >= min_fraction


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix0, iy0, ix1, iy1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    if inter == 0:
        return 0.0
    area = lambda r: max(0, r[2] - r[0]) * max(0, r[3] - r[1])
    return inter / (area(a) + area(b) - inter)


def new_elements(prev: list[UIElement], cur: list[UIElement], iou_min: float = 0.5) -> list[int]:
    """IDs of elements in `cur` without a same-label, overlapping match in `prev` (e.g. a menu that opened)."""
    out = []
    for e in cur:
        match = any(
            p.label.strip().lower() == e.label.strip().lower() and iou(p.bbox, e.bbox) >= iou_min
            for p in prev
        )
        if not match:
            out.append(e.id)
    return out

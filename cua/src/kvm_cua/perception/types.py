"""What the agent knows about a screen."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from PIL import Image

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in screen pixels


@dataclass
class UIElement:
    id: int
    kind: Literal["text", "icon"]
    label: str
    bbox: Box
    interactable: bool = True
    score: float = 1.0
    source: str = ""  # which detector produced it (ocr, yolo, caption, fixture)

    @property
    def center(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox
        return (x0 + x1) / 2, (y0 + y1) / 2

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    def to_json(self) -> dict:
        d = asdict(self)
        d["bbox"] = list(self.bbox)
        return d

    @staticmethod
    def from_json(d: dict) -> UIElement:
        return UIElement(
            id=int(d["id"]),
            kind=d["kind"],
            label=d["label"],
            bbox=tuple(int(v) for v in d["bbox"]),  # type: ignore[arg-type]
            interactable=bool(d.get("interactable", True)),
            score=float(d.get("score", 1.0)),
            source=d.get("source", ""),
        )


def position_words(el: UIElement, size: tuple[int, int]) -> str:
    """'top-left', 'middle-center', … — coarse location the decision model can reason about."""
    w, h = size
    cx, cy = el.center
    row = "top" if cy < h / 3 else "bottom" if cy > 2 * h / 3 else "middle"
    col = "left" if cx < w / 3 else "right" if cx > 2 * w / 3 else "center"
    return f"{row}-{col}"


@dataclass
class Screen:
    image: Image.Image
    elements: list[UIElement]
    seq: int = 0
    phash: int = 0
    captured_at: float = 0.0
    parse_ms: int = 0
    new_element_ids: list[int] = field(default_factory=list)  # appeared since the previous screen

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def element(self, element_id: int) -> UIElement | None:
        return next((e for e in self.elements if e.id == element_id), None)

    def reading_order(self) -> list[UIElement]:
        # rows of ~1/40 screen height, then left to right
        band = max(8, self.size[1] // 40)
        return sorted(self.elements, key=lambda e: (int(e.center[1] // band), e.center[0]))

    def describe(self, max_chars: int = 20000) -> str:
        """Compact text rendering of the screen, one element per line."""
        lines = []
        new = set(self.new_element_ids)
        for e in self.reading_order():
            flag = " NEW" if e.id in new else ""
            lines.append(
                f"[{e.id}] {e.kind} '{e.label}' at {position_words(e, self.size)} "
                f"({int(e.center[0])},{int(e.center[1])}) {e.width}x{e.height}{flag}"
            )
        out, total = [], 0
        for line in lines:
            if total + len(line) + 1 > max_chars:
                out.append(f"… {len(lines) - len(out)} more elements omitted")
                break
            out.append(line)
            total += len(line) + 1
        return "\n".join(out)

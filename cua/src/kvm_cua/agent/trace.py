"""Run traces: every screen, element list and decision of a run, on disk.

A trace is both a debugging record and a regression fixture: tests can replay
its screens through FakeKvm with a FixtureParser built from its element files.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..perception.annotate import annotate
from ..perception.types import Screen


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "run"


class Trace:
    def __init__(self, root: Path, goal: str, enabled: bool = True):
        self.enabled = enabled
        self.dir = Path(root) / f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(goal)}"
        self._n = 0
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "goal.txt").write_text(goal, "utf-8")

    def screen(self, screen: Screen, highlight: int | None = None) -> str:
        """Store a screen; returns its file stem."""
        self._n += 1
        stem = f"screen_{self._n:03d}"
        if self.enabled:
            screen.image.save(self.dir / f"{stem}.png")
            annotate(screen.image, screen.elements, highlight, screen.new_element_ids).save(
                self.dir / f"{stem}.annotated.jpg", quality=80
            )
            (self.dir / f"{stem}.elements.json").write_text(
                json.dumps([e.to_json() for e in screen.elements], ensure_ascii=False, indent=1), "utf-8"
            )
        return stem

    def highlight(self, stem: str, screen: Screen, element_id: int | None) -> None:
        if self.enabled and element_id is not None:
            annotate(screen.image, screen.elements, element_id, screen.new_element_ids).save(
                self.dir / f"{stem}.annotated.jpg", quality=80
            )

    def event(self, record: dict[str, Any]) -> None:
        if self.enabled:
            with (self.dir / "decisions.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def result(self, data: dict[str, Any]) -> None:
        if self.enabled:
            (self.dir / "result.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str), "utf-8"
            )

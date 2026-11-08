"""Human steering for a running loop: guidance, answers, pause, resume, cancel.

Commands arrive from any thread (the dashboard, the remote control via the
agent server's worker dispatch). The loop reads them at safe points: before a
decision and before it looks at the screen again.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

CommandKind = Literal["guidance", "answer", "pause", "resume", "cancel"]
COMMAND_KINDS: tuple[str, ...] = ("guidance", "answer", "pause", "resume", "cancel")

# single spoken words that mean a control command rather than guidance
_KEYWORDS: dict[str, CommandKind] = {
    "pause": "pause",
    "wait": "pause",
    "warte": "pause",
    "resume": "resume",
    "continue": "resume",
    "go on": "resume",
    "weiter": "resume",
    "cancel": "cancel",
    "stop": "cancel",
    "stopp": "cancel",
    "abbrechen": "cancel",
}


@dataclass
class Command:
    kind: CommandKind
    text: str = ""
    at: float = field(default_factory=time.time)


def parse_command(raw: Any) -> Command | None:
    """A command from what a human submitted: JSON `{command, text}`, a dict with one text value,
    a control keyword ('pause', 'weiter', 'stop', …) or any other text as guidance."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s[0] in "{[":
            try:
                return parse_command(json.loads(s))
            except ValueError:
                pass
        word = re.sub(r"[^\w ]+", "", s.lower()).strip()
        if word in _KEYWORDS:
            return Command(_KEYWORDS[word])
        return Command("guidance", s)
    if isinstance(raw, dict):
        kind = str(raw.get("command") or "").strip().lower()
        text = next(
            (str(raw[k]) for k in ("text", "message", "guidance", "answer") if raw.get(k)),
            "",
        )
        if not kind:
            strings = [v for v in raw.values() if isinstance(v, str) and v.strip()]
            if not text and len(strings) == 1:
                text = strings[0]
            return parse_command(text) if text else None
        if kind not in COMMAND_KINDS:
            return None
        if kind in ("guidance", "answer") and not text.strip():
            return None
        return Command(kind, text.strip())  # type: ignore[arg-type]
    if isinstance(raw, list) and raw:
        return parse_command(raw[-1])
    return None


class GuidanceInbox:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._texts: deque[Command] = deque(maxlen=20)
        self._paused = False
        self._cancelled = False

    # --- writers (any thread) ------------------------------------------------

    def put(self, cmd: Command) -> None:
        with self._cond:
            if cmd.kind == "pause":
                self._paused = True
            elif cmd.kind == "resume":
                self._paused = False
            elif cmd.kind == "cancel":
                self._cancelled = True
                self._paused = False
            else:
                self._texts.append(cmd)
                self._paused = False  # speaking guidance ends a pause
            self._cond.notify_all()

    def reset(self) -> None:
        with self._cond:
            self._texts.clear()
            self._paused = self._cancelled = False

    # --- readers (the loop) --------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def take_texts(self) -> list[Command]:
        with self._cond:
            out = list(self._texts)
            self._texts.clear()
            return out

    def wait_while_paused(
        self, cancel: threading.Event | None = None, deadline: float | None = None
    ) -> float:
        """Block while paused; returns the seconds spent waiting."""
        t0 = time.time()
        with self._cond:
            while self._paused and not self._cancelled and not (cancel and cancel.is_set()):
                if deadline is not None and time.time() >= deadline:
                    break
                self._cond.wait(0.2)
        return time.time() - t0

    def wait_for_text(self, timeout_s: float, cancel: threading.Event | None = None) -> list[Command]:
        """Wait up to timeout_s (longer while paused) for guidance or an answer."""
        deadline = time.time() + timeout_s
        with self._cond:
            while not self._texts and not self._cancelled and not (cancel and cancel.is_set()):
                if not self._paused and time.time() >= deadline:
                    break
                self._cond.wait(0.2)
            out = list(self._texts)
            self._texts.clear()
            return out

"""Human-like mouse and keyboard on top of a KvmDevice.

The pointer travels on a slightly curved, eased path (minimum-jerk velocity
profile) whose duration grows with distance like Fitts' law, with a little
jitter and an occasional small overshoot. The device plays the whole path with
real timing, so network jitter does not distort it.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .device import ABS_MAX, KeyEvent, KvmDevice, MouseEvent, WheelEvent
from .keymap import combo, text_to_events

log = logging.getLogger(__name__)

BUTTONS = {"left": 1, "right": 2, "middle": 4}
Direction = Literal["up", "down", "left", "right"]


@dataclass
class MotionProfile:
    step_ms: int = 8
    min_ms: int = 120
    max_ms: int = 900
    jitter_px: float = 1.2
    overshoot_chance: float = 0.25
    click_hold_ms: tuple[int, int] = (55, 110)
    settle_ms: tuple[int, int] = (40, 120)


def _min_jerk(t: float) -> float:
    return 10 * t**3 - 15 * t**4 + 6 * t**5


def human_path(
    start: tuple[float, float],
    end: tuple[float, float],
    rng: random.Random,
    profile: MotionProfile,
) -> list[tuple[float, float, int]]:
    """Points (x, y, delay_ms) from start to end in screen pixels; the last point is exactly `end`."""
    (x0, y0), (x1, y1) = start, end
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 2:
        return [(x1, y1, profile.step_ms)]
    duration = min(profile.max_ms, max(profile.min_ms, 90 + 140 * math.log2(1 + dist / 12)))
    steps = max(4, int(duration / profile.step_ms))

    # one control point off the straight line → gentle arc
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    nx, ny = -(y1 - y0) / dist, (x1 - x0) / dist
    bend = rng.uniform(-0.12, 0.12) * dist
    cx, cy = mx + nx * bend, my + ny * bend

    target = (x1, y1)
    overshoot = dist > 150 and rng.random() < profile.overshoot_chance
    if overshoot:
        over = rng.uniform(4, 12)
        target = (x1 + (x1 - x0) / dist * over, y1 + (y1 - y0) / dist * over)

    pts: list[tuple[float, float, int]] = []
    for i in range(1, steps + 1):
        t = _min_jerk(i / steps)
        bx = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t**2 * target[0]
        by = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t**2 * target[1]
        fade = 1 - i / steps
        bx += rng.gauss(0, profile.jitter_px) * fade
        by += rng.gauss(0, profile.jitter_px) * fade
        pts.append((bx, by, profile.step_ms))
    if overshoot:
        for i in range(1, 6):
            t = _min_jerk(i / 5)
            pts.append(
                (target[0] + (x1 - target[0]) * t, target[1] + (y1 - target[1]) * t, profile.step_ms + 4)
            )
    pts[-1] = (x1, y1, profile.step_ms)
    return pts


class Actuator:
    def __init__(
        self,
        device: KvmDevice,
        layout: str = "en-US",
        profile: MotionProfile | None = None,
        seed: int | None = None,
    ):
        self.device = device
        self.layout = layout
        self.profile = profile or MotionProfile()
        self.rng = random.Random(seed)
        self._pos: tuple[float, float] | None = None
        self._buttons = 0
        # called with the pixel target of every pointer move (e.g. to mirror it to a physical mouse)
        self.on_move: Callable[[float, float], None] | None = None

    # --- coordinates ---------------------------------------------------------

    def to_abs(self, x: float, y: float) -> tuple[int, int]:
        w, h = self.device.screen_size()
        ax = round(min(max(x, 0), w - 1) / max(w - 1, 1) * ABS_MAX)
        ay = round(min(max(y, 0), h - 1) / max(h - 1, 1) * ABS_MAX)
        return ax, ay

    @property
    def position(self) -> tuple[float, float] | None:
        return self._pos

    # --- mouse ----------------------------------------------------------------

    def move_to(self, x: float, y: float) -> None:
        if self._pos is None:
            # Unknown start (absolute mouse has no read-back): approach from a
            # nearby point so the first move is still a short, natural glide.
            w, h = self.device.screen_size()
            self._pos = (
                min(max(x + self.rng.uniform(-200, 200), 0), w - 1),
                min(max(y + self.rng.uniform(-150, 150), 0), h - 1),
            )
        path = human_path(self._pos, (x, y), self.rng, self.profile)
        self._moved(x, y)
        self.device.mouse([self._event(px, py, d) for px, py, d in path])
        self._pos = (x, y)

    def pointer(self, x: float, y: float, buttons: int = 0) -> None:
        """One raw event at once (manual control from the dashboard): no path, no timing."""
        ax, ay = self.to_abs(x, y)
        self._moved(x, y)
        self.device.mouse([MouseEvent(ax, ay, buttons & 0x1F, 0)])
        self._pos = (x, y)

    def _moved(self, x: float, y: float) -> None:
        if self.on_move is not None:
            try:
                self.on_move(x, y)
            except Exception as e:  # noqa: BLE001 — a mirror must never break the real input
                log.warning("pointer mirror failed: %s", e)

    def _event(self, x: float, y: float, delay_ms: int) -> MouseEvent:
        ax, ay = self.to_abs(x, y)
        return MouseEvent(ax, ay, self._buttons, delay_ms)

    def click(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        self.move_to(x, y)
        b = BUTTONS[button]
        events: list[MouseEvent] = []
        ax, ay = self.to_abs(x, y)
        events.append(MouseEvent(ax, ay, 0, self.rng.randint(*self.profile.settle_ms)))
        for i in range(count):
            events.append(MouseEvent(ax, ay, b, self.rng.randint(*self.profile.click_hold_ms)))
            gap = self.rng.randint(70, 120) if i < count - 1 else 10
            events.append(MouseEvent(ax, ay, 0, gap))
        self.device.mouse(events)

    def double_click(self, x: float, y: float) -> None:
        self.click(x, y, count=2)

    def drag(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.move_to(x0, y0)
        ax, ay = self.to_abs(x0, y0)
        self._buttons = 1
        self.device.mouse([MouseEvent(ax, ay, 1, 120)])
        try:
            self.move_to(x1, y1)
        finally:
            self._buttons = 0
            bx, by = self.to_abs(x1, y1)
            self.device.mouse([MouseEvent(bx, by, 0, 30)])

    def scroll(self, direction: Direction, notches: int = 3, at: tuple[float, float] | None = None) -> None:
        """Scroll like a wheel: a few notches with uneven gaps. `at` hovers first (scroll targets the
        element under the pointer)."""
        if at is not None:
            self.move_to(*at)
        dy = {"up": 1, "down": -1}.get(direction, 0)
        dx = {"right": 1, "left": -1}.get(direction, 0)
        self.device.wheel([WheelEvent(dy, dx, self.rng.randint(25, 70)) for _ in range(max(1, notches))])

    # --- keyboard -------------------------------------------------------------

    def type_text(self, text: str) -> None:
        events = text_to_events(text, self.layout)
        for i in range(0, len(events), 800):  # keep every request below the device limit
            self.device.keys(events[i : i + 800])

    def press(self, spec: str) -> None:
        """Press and release a combo like 'ctrl+l', 'enter', 'esc'."""
        down = combo(spec)
        down.delay_ms = self.rng.randint(40, 80)
        self.device.keys([down, KeyEvent(0, [], 10)])

    def pause(self, seconds: float) -> None:
        time.sleep(seconds)

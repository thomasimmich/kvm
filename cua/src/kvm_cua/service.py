"""One object that wires device, parser, decider and agent together.

The MCP server, the CLI and the dashboard all use this, so they behave the
same way. Heavy parts (OmniParser) load lazily on first use.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from .agent.loop import Agent, RunResult, find_element
from .config import Settings
from .decision.decider import Decider, JevDecider, StubDecider
from .decision.jev import JevClient
from .kvm.actuation import Actuator
from .kvm.device import HttpKvm, KvmDevice
from .perception.annotate import annotate
from .perception.parser import UIParser, make_parser
from .perception.types import Screen

log = logging.getLogger(__name__)


@dataclass
class LiveState:
    """What the dashboard shows."""

    events: deque = field(default_factory=lambda: deque(maxlen=300))
    goal: str | None = None
    running: bool = False
    highlight: int | None = None
    last_result: dict | None = None


class CuaService:
    def __init__(
        self,
        settings: Settings,
        device: KvmDevice | None = None,
        parser: UIParser | None = None,
        decider: Decider | None = None,
    ):
        self.settings = settings
        self._device = device
        self._parser = parser
        self._decider = decider
        self._agent: Agent | None = None
        self._init_lock = threading.Lock()
        self.live = LiveState()

    # --- lazy wiring -----------------------------------------------------------

    @property
    def device(self) -> KvmDevice:
        if self._device is None:
            s = self.settings
            self._device = HttpKvm(s.kvm_url, s.kvm_token, s.kvm_timeout_s)
        return self._device

    @property
    def parser(self) -> UIParser:
        with self._init_lock:
            if self._parser is None:
                s = self.settings
                self._parser = make_parser(
                    s.cua_parser, s.cua_weights_dir, s.cua_device, s.cua_caption_icons, s.cua_ocr
                )
        return self._parser

    @property
    def decider(self) -> Decider:
        if self._decider is None:
            s = self.settings
            if s.typesafe_api_key:
                self._decider = JevDecider(JevClient(s.typesafe_api_key, s.jev_model, s.typesafe_base_url))
            else:
                log.warning("TYPESAFE_API_KEY is not set — using the keyword StubDecider instead of Jev")
                self._decider = StubDecider()
        return self._decider

    @property
    def agent(self) -> Agent:
        if self._agent is None:
            s = self.settings
            self._agent = Agent(
                self.device,
                self.parser,
                self.decider,
                Actuator(self.device, layout=s.kvm_keyboard_layout),
                runs_dir=s.cua_runs_dir,
                on_event=self._on_event,
            )
        return self._agent

    def _on_event(self, ev: dict) -> None:
        self.live.events.append(ev)
        if ev.get("type") == "decision":
            self.live.highlight = ev.get("element_id")
        elif ev.get("type") == "screen":
            self.live.highlight = None

    # --- operations (blocking; call from a worker thread in async code) --------

    def run_goal(
        self,
        goal: str,
        hints=None,
        inputs=None,
        max_steps: int | None = None,
        expect_text=None,
        time_budget_s=None,
    ) -> RunResult:
        self.live.goal, self.live.running = goal, True
        try:
            res = self.agent.run_goal(
                goal, hints, inputs, max_steps or self.settings.cua_max_steps, expect_text, time_budget_s
            )
            self.live.last_result = res.to_json()
            return res
        finally:
            self.live.running = False

    def observe(self) -> Screen:
        with self.agent.lock:
            return self.agent.observe()

    def _act_then_observe(self, fn) -> Screen:
        with self.agent.lock:
            prev = self.agent.last_screen or self.agent.observe(settle=False)
            fn(prev)
            return self.agent.observe(prev=prev, after_seq=prev.seq)

    def click_element(self, element_id: int, button: str = "left", double: bool = False) -> Screen:
        def act(prev: Screen) -> None:
            el = prev.element(element_id)
            if el is None:
                raise ValueError(
                    f"element {element_id} is not on the last observed screen — call cua_observe first"
                )
            self._on_event(
                {"type": "decision", "action": f"click '{el.label}'", "element_id": el.id, "ts": time.time()}
            )
            self.agent.actuator.click(*el.center, button=button, count=2 if double else 1)

        return self._act_then_observe(act)

    def click_text(self, text: str, double: bool = False) -> tuple[Screen, str]:
        with self.agent.lock:
            screen = self.agent.observe()
            el = find_element(screen, text)
            if el is None:
                return screen, f"no element containing '{text}' is visible"
        scr = self.click_element(el.id, double=double)
        return scr, f"clicked {el.kind} '{el.label}'"

    def scroll(self, direction: Literal["up", "down", "left", "right"], notches: int = 5) -> Screen:
        def act(prev: Screen) -> None:
            w, h = prev.size
            a = self.agent.actuator
            a.scroll(direction, notches, at=a.position or (w / 2, h / 2))

        return self._act_then_observe(act)

    def type_text(self, text: str) -> Screen:
        return self._act_then_observe(lambda prev: self.agent.actuator.type_text(text))

    def press(self, combo: str) -> Screen:
        return self._act_then_observe(lambda prev: self.agent.actuator.press(combo))

    def annotated_jpeg(self) -> bytes | None:
        scr = self.agent.last_screen if self._agent else None
        if scr is None:
            return None
        img = annotate(scr.image, scr.elements, self.live.highlight, scr.new_element_ids)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return buf.getvalue()


def screen_text(screen: Screen, max_chars: int = 12000) -> str:
    w, h = screen.size
    head = f"screen {w}x{h}, {len(screen.elements)} elements (parse {screen.parse_ms} ms)"
    if screen.new_element_ids:
        head += f", {len(screen.new_element_ids)} new since last screen"
    return head + "\n" + screen.describe(max_chars)

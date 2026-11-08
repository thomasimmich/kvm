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

from .agent.inbox import Command
from .agent.loop import Agent, RunResult, find_element
from .config import Settings
from .decision.decider import Decider, JevDecider, StubDecider
from .decision.jev import JevClient
from .kvm.actuation import Actuator
from .kvm.device import HttpKvm, KvmDevice
from .narration.agent_server import AgentServerClient
from .narration.publisher import NarrationPublisher
from .narration.session import NarrationSessions
from .narration.vision import make_describer
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
        self._publisher: NarrationPublisher | None = None
        self.narration: NarrationSessions | None = None
        if settings.narration_enabled:
            self.narration = NarrationSessions(
                AgentServerClient(settings.leanscope_agent_server_url, settings.leanscope_api_token),
                settings.leanscope_repository_id,
                settings.leanscope_choreo_scenario_guid,
                settings.leanscope_session_repository_id or None,
                settings.leanscope_remote_control_url,
                settings.cua_dispatch_timeout_s,
            )
            self.narration.on_command = self.command

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
            describer, off_reason = make_describer(
                s.cua_vision_provider, s.openai_api_key, s.cua_vision_model, s.cua_vision_base_url
            )
            self._agent = Agent(
                self.device,
                self.parser,
                self.decider,
                Actuator(self.device, layout=s.kvm_keyboard_layout),
                runs_dir=s.cua_runs_dir,
                on_event=self._on_event,
                describer=describer,
            )
            self._agent.describe_off_reason = off_reason
        return self._agent

    def _on_event(self, ev: dict) -> None:
        pub = self._publisher
        if pub is not None:
            pub(ev)
        # keys starting with "_" hold images and objects for the publisher, not JSON for the dashboard
        self.live.events.append({k: v for k, v in ev.items() if not k.startswith("_")})
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
        source: Literal["mcp", "dashboard", "cli"] = "mcp",
    ) -> RunResult:
        """source: who started the run. A dashboard user can answer when the CUA is stuck, so it waits
        for them; an MCP caller alone can't, so it only waits when a narration session is live."""
        self.live.goal, self.live.running = goal, True
        steps = max_steps or self.settings.cua_max_steps
        agent = self.agent
        run = None
        t0 = time.time()
        try:
            if self.narration is not None:
                run = self.narration.begin(goal, list(hints or []), list((inputs or {}).keys()), steps)
                self._publisher = run.publisher
                if time_budget_s is not None:  # starting the session took part of the caller's time
                    time_budget_s = max(10.0, time_budget_s - (time.time() - t0))
            watched = source == "dashboard" or self._publisher is not None
            agent.human_wait_s = self.settings.cua_human_wait_s if watched else 0.0
            res = agent.run_goal(goal, hints, inputs, steps, expect_text, time_budget_s)
            if run is not None and run.handle is not None:
                res.session_url = run.handle.ui_url
            self.live.last_result = res.to_json()
            return res
        finally:
            self._publisher = None
            if run is not None:
                self.narration.end(run)  # type: ignore[union-attr]
            self.live.running = False

    def command(self, cmd: Command) -> bool:
        """Guidance, an answer, pause, resume or cancel for the running loop."""
        if self._agent is None or not self.live.running:
            return False
        self._agent.command(cmd)
        return True

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

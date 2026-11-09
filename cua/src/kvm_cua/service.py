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
from .kvm.connection import KvmConnection, explain_kvm_error, normalize_kvm_url
from .kvm.device import HttpKvm, KvmDevice, WheelEvent
from .kvm.sphero import SpheroMirror
from .narration.agent_server import AgentServerClient
from .narration.inner_voice import InnerVoice, Speaker, Thinker
from .narration.leanscope_auth import ChoreoSelection, LeanScopeAuth, LeanScopeCatalog
from .narration.persona_voice import PersonaVoiceCache
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


class Busy(RuntimeError):
    """The agent holds the mouse (a run or a manual tool call)."""


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
        self.kvm = KvmConnection(settings.cua_kvm_state_file, settings.kvm_url, settings.kvm_token)
        self.kvm_factory = lambda url, token: HttpKvm(url, token, settings.kvm_timeout_s)
        self.kvm_status: dict = {}  # last check: {ok, kind, error, ts}; empty until the first one
        self._parser = parser
        self._decider = decider
        self._agent: Agent | None = None
        self._init_lock = threading.Lock()
        self.live = LiveState()
        self._publisher: NarrationPublisher | None = None
        self.narration: NarrationSessions | None = None
        self.leanscope: LeanScopeAuth | None = None
        self.catalog: LeanScopeCatalog | None = None
        if settings.narration_enabled:
            self.leanscope = LeanScopeAuth(
                settings.leanscope_supabase_url, settings.leanscope_supabase_anon_key, settings.cua_state_file
            )
            self.catalog = LeanScopeCatalog(settings.leanscope_api_url, self.leanscope)
            self.narration = NarrationSessions(
                AgentServerClient(settings.leanscope_agent_server_url, self.leanscope.access_token),
                self.leanscope,
                settings.leanscope_remote_control_url,
                settings.cua_dispatch_timeout_s,
            )
            self.narration.on_command = self.command
        self.inner_voice = self._make_inner_voice()
        self.sphero = SpheroMirror(self._on_event, settings.cua_sphero_state_file, settings.cua_sphero_url)

    def _make_inner_voice(self) -> InnerVoice:
        s = self.settings
        off = ""
        if not s.elevenlabs_api_key:
            off = "ELEVENLABS_API_KEY is not set in .env"
        elif not s.openai_api_key:
            off = "LEANSCOPE_OPENAI_API_KEY is not set in .env (OPENAI_API_KEY works too)"
        elif self.catalog is None:
            off = "LeanScope is not configured (LEANSCOPE_AGENT_SERVER_URL), so there is no persona"
        auth = self.leanscope
        voices = PersonaVoiceCache(
            self.catalog,
            lambda: auth.selection if auth else ChoreoSelection(),
            s.cua_inner_voice_persona_guid,
            s.cua_inner_voice_id,
        )
        return InnerVoice(
            self._on_event,
            voices.get,
            Thinker(s.openai_api_key, s.cua_inner_voice_model, s.cua_vision_base_url) if not off else None,
            Speaker(s.elevenlabs_api_key, s.elevenlabs_tts_model) if not off else None,
            enabled=s.cua_inner_voice,
            off_reason=off,
        )

    # --- lazy wiring -----------------------------------------------------------

    @property
    def device(self) -> KvmDevice:
        if self._device is None:
            self._device = self.kvm_factory(self.kvm.url, self.kvm.token)
        return self._device

    # --- JetKVM address and token from the dashboard -----------------------------------

    def note_kvm(self, error: Exception | None) -> dict:
        """Records whether the last request to the JetKVM worked (the dashboard's JetKVM card)."""
        if error is None:
            self.kvm_status = {"ok": True, "kind": "", "error": "", "ts": time.time()}
        else:
            kind, text = explain_kvm_error(error, self.kvm.url)
            self.kvm_status = {"ok": kind == "signal", "kind": kind, "error": text, "ts": time.time()}
        return self.kvm_status

    def kvm_state(self) -> dict:
        return {**self.kvm.snapshot(), "status": self.kvm_status}

    def connect_kvm(self, url: str, token: str = "") -> dict:
        """Saves the JetKVM's address and token (an empty token keeps the saved one), then checks
        them. Saved even when the check fails, so the live view picks them up once the API is on."""
        url = normalize_kvm_url(url)
        if not url:
            raise ValueError("enter the LeanScope dongle's IP address, e.g. 192.168.1.50")
        token = token.strip() or self.kvm.token
        if self.live.running:
            raise Busy("the agent is running: cancel it first")
        agent = self._agent
        if agent is not None and not agent.lock.acquire(timeout=2.0):
            raise Busy("the agent is busy with an action")
        try:
            self.kvm.save(url, token)
            old, self._device, self._agent = self._device, None, None  # rebuilt with the new device on use
        finally:
            if agent is not None:
                agent.lock.release()
        close = getattr(old, "close", None)
        if close is not None:
            close()
        try:
            self.device.screen_size()  # one /state request; also fails on an empty token
            self.note_kvm(None)
        except Exception as e:  # noqa: BLE001 — explained on the dashboard
            self.note_kvm(e)
        return self.kvm_state()

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
            actuator = Actuator(self.device, layout=s.kvm_keyboard_layout)
            actuator.on_move = self._mirror_move  # agent and manual moves both reach the Sphero
            self._agent = Agent(
                self.device,
                self.parser,
                self.decider,
                actuator,
                runs_dir=s.cua_runs_dir,
                on_event=self._on_event,
                describer=describer,
            )
            self._agent.describe_off_reason = off_reason
        return self._agent

    def _mirror_move(self, x: float, y: float) -> None:
        if self.sphero.enabled:
            w, h = self.device.screen_size()
            self.sphero.push(x / max(w - 1, 1) * 100, y / max(h - 1, 1) * 100)

    def _on_event(self, ev: dict) -> None:
        pub = self._publisher
        if pub is not None:
            pub(ev)
        self.inner_voice(ev)
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
            if self.narration is not None and self.narration.auth.narrate:  # off by default (dashboard switch)
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

    def reset_live(self) -> None:
        """Empties what the dashboard shows of past runs (the activity log and the last result)."""
        live = self.live
        live.events.clear()
        live.last_result = None
        live.goal = None
        live.highlight = None

    # --- manual pointer from the dashboard's live view -------------------------------

    def _manual(self, fn) -> None:
        """Runs fn(actuator, w, h) unless the agent has the mouse. Short waits serialise the
        dashboard's own requests; a run in progress is refused at once."""
        if self.live.running:
            raise Busy("the agent is running: cancel or pause it first")
        agent = self.agent
        if not agent.lock.acquire(timeout=2.0):
            raise Busy("the agent is busy with an action")
        try:
            fn(agent.actuator, *self.device.screen_size())
        finally:
            agent.lock.release()

    def pointer_at(self, rx: float, ry: float, buttons: int = 0) -> None:
        """rx, ry: 0..1 of the screen; buttons: bit 1 left, 2 right, 4 middle."""
        rx, ry = min(max(rx, 0.0), 1.0), min(max(ry, 0.0), 1.0)
        self._manual(lambda a, w, h: a.pointer(rx * (w - 1), ry * (h - 1), int(buttons)))

    def wheel_at(self, rx: float, ry: float, dy: int, dx: int = 0) -> None:
        """Wheel notches at a point (positive dy scrolls up, like the device)."""
        rx, ry = min(max(rx, 0.0), 1.0), min(max(ry, 0.0), 1.0)

        def act(a: Actuator, w: int, h: int) -> None:
            x, y = rx * (w - 1), ry * (h - 1)
            if a.position != (x, y):
                a.pointer(x, y)
            a.device.wheel([WheelEvent(max(-127, min(127, int(dy))), max(-127, min(127, int(dx))), 0)])

        self._manual(act)

    def pointer_state(self) -> dict | None:
        """The last known pointer position in percent (None until the first move)."""
        pos = self._agent.actuator.position if self._agent else None
        if pos is None:
            return None
        try:
            w, h = self.device.screen_size()
        except Exception:  # noqa: BLE001 — no picture yet
            return None
        return {"x": pos[0] / max(w - 1, 1) * 100, "y": pos[1] / max(h - 1, 1) * 100}

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

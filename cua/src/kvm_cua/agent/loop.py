"""The CUA loop: observe → decide → act → observe → verify → continue or discard.

Element ids only live for one parse, so "already tried" is remembered by label
and box. When Jev says the last action did not help, the loop undoes it where it
can (Escape closes a popup that just opened), forgets that element for this
screen and asks again.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from PIL import Image

from ..decision.decider import Action, Decider, DecisionContext
from ..kvm.actuation import Actuator
from ..kvm.device import KvmDevice
from ..perception.parser import UIParser
from ..perception.screen import changed_fraction, dhash, hamming, iou, new_elements, screens_differ
from ..perception.types import Screen, UIElement
from .inbox import Command, GuidanceInbox
from .trace import Trace

log = logging.getLogger(__name__)

Status = Literal["done", "stuck", "max_steps", "time_budget", "cancelled", "error"]
PROGRESS_THRESHOLD = 0.5
DESCRIBE_MIN_CHANGE = 40  # dhash bits (of 256) that make a screen new enough to describe again


@dataclass
class StepRecord:
    step: int
    action: str
    option: str
    confidence: float
    alternatives: dict[str, float]
    outcome: str = ""
    progress: float | None = None
    screen: str = ""
    ms: int = 0


@dataclass
class RunResult:
    status: Status
    goal: str
    steps: list[StepRecord] = field(default_factory=list)
    message: str = ""
    trace_dir: str = ""
    final_screen: str = ""
    seconds: float = 0.0
    session_url: str = ""  # the live LeanScope session that narrated this run, if any

    def summary(self) -> str:
        lines = [
            f"status: {self.status} after {len(self.steps)} step(s), {self.seconds:.0f}s — {self.message}"
        ]
        for s in self.steps:
            p = f", progress={s.progress:.2f}" if s.progress is not None else ""
            lines.append(f"{s.step}. {s.action} → {s.outcome}{p}")
        if self.final_screen:
            lines.append("final screen (excerpt):\n" + self.final_screen)
        if self.trace_dir:
            lines.append(f"trace: {self.trace_dir}")
        if self.session_url:
            lines.append(f"live session: {self.session_url}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class _Tried:
    label: str
    bbox: tuple[int, int, int, int]
    phash: int


class Agent:
    def __init__(
        self,
        device: KvmDevice,
        parser: UIParser,
        decider: Decider,
        actuator: Actuator | None = None,
        runs_dir: Path = Path("runs"),
        save_traces: bool = True,
        settle_timeout_s: float = 4.0,
        settle_interval_s: float = 0.35,
        on_event: Callable[[dict], None] | None = None,
        describer: Callable[[Image.Image], str] | None = None,
    ):
        self.device = device
        self.parser = parser
        self.decider = decider
        self.actuator = actuator or Actuator(device)
        self.runs_dir = Path(runs_dir)
        self.save_traces = save_traces
        self.settle_timeout_s = settle_timeout_s
        self.settle_interval_s = settle_interval_s
        self.on_event = on_event
        self.describer = describer  # fast image-to-text model for a rough screen description
        self.last_screen: Screen | None = None
        self.lock = threading.Lock()  # one run (or manual action) at a time
        self._cancel = threading.Event()
        self.inbox = GuidanceInbox()
        self.human_wait_s = 0.0  # > 0: when stuck, ask the human and wait this long for an answer
        self.ask_after_discards = 3  # also ask after this many discarded attempts in a row
        self._describe_pool: ThreadPoolExecutor | None = None
        self._described_phash: int | None = None
        self._trace: Trace | None = None

    # --- perception --------------------------------------------------------------

    def observe(self, prev: Screen | None = None, after_seq: int = 0, settle: bool = True) -> Screen:
        """Screenshot once the screen stopped changing (animations, page loads), then parse it."""
        frame = self.device.screenshot(after_seq=after_seq)
        if settle:
            deadline = time.time() + self.settle_timeout_s
            while time.time() < deadline:
                time.sleep(self.settle_interval_s)
                nxt = self.device.screenshot(after_seq=frame.seq)
                stable = changed_fraction(frame.image, nxt.image) < 0.002
                frame = nxt
                if stable:
                    break
        phash = dhash(frame.image)
        self._emit({"type": "screen_raw", "seq": frame.seq, "_image": frame.image})
        self._maybe_describe(frame.image, frame.seq, phash)
        t0 = time.time()
        elements = self.parser.parse(frame.image)
        screen = Screen(
            image=frame.image,
            elements=elements,
            seq=frame.seq,
            phash=phash,
            captured_at=frame.captured_at,
            parse_ms=int((time.time() - t0) * 1000),
        )
        if prev is not None:
            screen.new_element_ids = new_elements(prev.elements, elements)
            # a full page change marks everything as new; only flag a partial change (popup, menu)
            if len(screen.new_element_ids) > 0.6 * max(1, len(elements)):
                screen.new_element_ids = []
        self.last_screen = screen
        self._emit({"type": "screen", "seq": screen.seq, "elements": len(elements), "parse_ms": screen.parse_ms,
                    "new": len(screen.new_element_ids), "_screen": screen})  # fmt: skip
        return screen

    def _maybe_describe(self, image: Image.Image, seq: int, phash: int) -> None:
        """Describe the screen in parallel to parsing, once per new view (first screen, page change)."""
        if self.describer is None:
            return
        if self._described_phash is not None and hamming(phash, self._described_phash) < DESCRIBE_MIN_CHANGE:
            return
        self._described_phash = phash
        if self._describe_pool is None:
            self._describe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="describe")
        trace, describer = self._trace, self.describer

        def work() -> None:
            t0 = time.time()
            try:
                text = describer(image).strip()
            except Exception as e:  # noqa: BLE001 — narration must never break a run
                log.warning("screen description failed: %s", e)
                return
            if not text:
                return
            ms = int((time.time() - t0) * 1000)
            if trace is not None:
                trace.event({"type": "description", "seq": seq, "ms": ms, "text": text})
            self._emit({"type": "screen_description", "seq": seq, "text": text, "ms": ms})

        self._describe_pool.submit(work)

    # --- actions -------------------------------------------------------------------

    def execute(self, action: Action, screen: Screen, inputs: dict[str, str] | None = None) -> None:
        a = self.actuator
        if action.kind == "click":
            el = screen.element(action.element_id) if action.element_id is not None else None
            if el is None:
                raise ValueError(f"element {action.element_id} is not on the current screen")
            a.click(*el.center)
        elif action.kind == "scroll":
            w, h = screen.size
            a.scroll(action.direction or "down", notches=5, at=a.position or (w / 2, h / 2))
        elif action.kind == "type":
            text = (inputs or {}).get(action.input_name or "")
            if text is None:
                raise ValueError(f"no input named {action.input_name!r}")
            a.type_text(text)
        elif action.kind == "press":
            a.press(action.key or "enter")
        elif action.kind == "dismiss":
            a.press("esc")

    def cancel(self) -> None:
        self._cancel.set()

    def command(self, cmd: Command) -> None:
        """A human command for the running loop (from the dashboard or the remote control)."""
        self.inbox.put(cmd)
        if cmd.kind == "cancel":
            self._cancel.set()
        names = {"pause": "paused", "resume": "resumed", "cancel": "cancel_requested"}
        self._emit({"type": names.get(cmd.kind, "guidance_received"), "kind": cmd.kind, "text": cmd.text})

    def _say(self, text: str) -> None:
        """A micro-level activity line ("asking Jev", "pressed Esc to undo")."""
        self._emit({"type": "activity", "text": text})

    def _ask_human(self, question: str, deadline: float | None) -> list[str]:
        wait = self.human_wait_s
        if deadline is not None:
            wait = min(wait, deadline - time.time() - 5)
        if wait <= 0:
            return []
        self._emit({"type": "ask_human", "question": question, "wait_s": round(wait)})
        texts = [c.text for c in self.inbox.wait_for_text(wait, self._cancel)]
        self._emit({"type": "ask_human_end", "answered": bool(texts), "cancelled": self._cancel.is_set()})
        return texts

    # --- the loop ------------------------------------------------------------------

    def run_goal(
        self,
        goal: str,
        hints: list[str] | None = None,
        inputs: dict[str, str] | None = None,
        max_steps: int = 15,
        expect_text: str | None = None,
        time_budget_s: float | None = None,
    ) -> RunResult:
        """time_budget_s: stop before starting a step once this much time has passed, so callers with a
        hard timeout (the agent server allows 120 s per MCP call) always get a summary back."""
        with self.lock:
            self._cancel.clear()
            self.inbox.reset()
            self._described_phash = None
            return self._run(goal, hints or [], inputs or {}, max_steps, expect_text, time_budget_s)

    def _run(
        self,
        goal: str,
        hints: list[str],
        inputs: dict[str, str],
        max_steps: int,
        expect_text: str | None,
        time_budget_s: float | None,
    ) -> RunResult:
        t_start = time.time()
        trace = Trace(self.runs_dir, goal, enabled=self.save_traces)
        result = RunResult(
            status="max_steps", goal=goal, trace_dir=str(trace.dir) if self.save_traces else ""
        )
        tried: list[_Tried] = []
        exhausted: set[str] = set()
        history: list[str] = []
        last: Action | None = None
        last_desc: str | None = None
        last_record: StepRecord | None = None
        before: Screen | None = None
        guidance: list[str] = []
        discards = 0  # discarded attempts in a row
        deadline = t_start + time_budget_s if time_budget_s is not None else None
        self._trace = trace
        self._emit({"type": "run_start", "goal": goal, "hints": hints, "max_steps": max_steps})

        def absorb() -> None:
            """Block while a human paused the run; take in what they said."""
            if self.inbox.paused:
                self._say("Paused: waiting for you")
                self.inbox.wait_while_paused(self._cancel, deadline)
            for c in self.inbox.take_texts():
                guidance.append(c.text)
                history.append(f"human: {c.text}")
                trace.event({"type": "guidance", "kind": c.kind, "text": c.text})

        def take_answer(texts: list[str]) -> None:
            for t in texts:
                guidance.append(t)
                history.append(f"human: {t}")
                trace.event({"type": "guidance", "kind": "answer", "text": t})

        try:
            self._say("Looking at the screen")
            screen = self.observe()
            stem = trace.screen(screen)
            for step in range(1, max_steps + 1):
                absorb()
                if self._cancel.is_set():
                    who = "the human" if self.inbox.cancelled else "caller"
                    result.status, result.message = "cancelled", f"cancelled by {who}"
                    break
                if time_budget_s is not None and time.time() - t_start > time_budget_s:
                    result.status = "time_budget"
                    result.message = f"time budget of {time_budget_s:.0f}s used up; call again to continue"
                    break
                if expect_text and _visible(expect_text, screen):
                    result.status, result.message = "done", f"expected text '{expect_text}' is visible"
                    break

                ctx = DecisionContext(
                    goal=goal,
                    screen=screen,
                    hints=hints,
                    inputs=inputs,
                    history=list(history),
                    excluded=_excluded_ids(screen, tried),
                    exhausted_scroll=set(exhausted),
                    last_action=last_desc,
                    before=before,
                    guidance=list(guidance),
                )
                self._say("Asking Jev for the next step")
                t0 = time.time()
                decision = self.decider.decide(ctx)
                decide_ms = int((time.time() - t0) * 1000)
                trace.event({"step": step, "option": decision.action.option, "confidence": decision.action.confidence,
                             "alternatives": decision.action.alternatives, "progress": decision.progress,
                             "decide_ms": decide_ms, "tokens": decision.tokens})  # fmt: skip

                # verdict on the previous action
                if last is not None and last_record is not None:
                    last_record.progress = decision.progress
                    if decision.progress is not None and decision.progress < PROGRESS_THRESHOLD:
                        last_record.outcome = "no progress → discarded"
                        history.append(f"{last_desc} → did not help, undone")
                        discards += 1
                        if last.kind == "click" and before is not None and last.element_id is not None:
                            el = before.element(last.element_id)
                            if el:
                                tried.append(_Tried(el.label, el.bbox, before.phash))
                        prev = screen
                        if screen.new_element_ids or last.kind == "click":
                            self._say(f"That did not help ({last_desc}): pressing Esc to undo")
                            self.actuator.press("esc")
                            screen = self.observe(prev=prev, after_seq=prev.seq)
                            stem = trace.screen(screen)
                        else:
                            self._say(f"That did not help ({last_desc}): trying something else")
                        last = last_desc = last_record = before = None
                        if discards >= self.ask_after_discards:
                            discards = 0
                            take_answer(self._ask_human(
                                f"I tried several things that did not help with '{goal}'. What should I do next?",
                                deadline))  # fmt: skip
                        continue
                    last_record.outcome = last_record.outcome or "ok"
                    history.append(f"{last_desc} → ok")
                    discards = 0
                last = last_desc = last_record = before = None

                action = decision.action
                desc = action.describe(screen)
                rec = StepRecord(
                    step,
                    desc,
                    action.option,
                    action.confidence,
                    action.alternatives,
                    screen=stem,
                    ms=decide_ms,
                )
                result.steps.append(rec)
                self._emit({"type": "decision", "step": step, "action": desc, "option": action.option,
                            "confidence": action.confidence, "alternatives": action.alternatives,
                            "element_id": action.element_id, "progress": decision.progress,
                            "_screen": screen})  # fmt: skip
                trace.highlight(stem, screen, action.element_id)

                if action.kind == "done":
                    rec.outcome = "goal reached"
                    result.status, result.message = "done", "decision model reports the goal is achieved"
                    break
                if action.kind == "stuck":
                    answer = self._ask_human(
                        f"I'm stuck: nothing on this screen seems to bring me closer to '{goal}'. "
                        "Where should I go?",
                        deadline,
                    )
                    if answer:
                        rec.outcome = "asked the human"
                        take_answer(answer)
                        continue
                    if self._cancel.is_set():
                        rec.outcome = "cancelled while waiting for an answer"
                        who = "the human" if self.inbox.cancelled else "caller"
                        result.status, result.message = "cancelled", f"cancelled by {who}"
                        break
                    rec.outcome = "gave up"
                    result.status, result.message = "stuck", "no visible element or scroll direction helps"
                    break

                prev = screen
                self.execute(action, screen, inputs)
                absorb()  # a pause right after the action holds before the screen is judged
                self._say("Looking at the screen")
                screen = self.observe(prev=prev, after_seq=prev.seq)
                stem = trace.screen(screen)
                again = _click_only_closed_popup(prev, screen, action.element_id) if action.kind == "click" else None
                if again is not None:
                    # macOS (and many menus elsewhere) swallow a click outside an open menu:
                    # it only closes the menu. Click the same element once more.
                    self.actuator.click(*again.center)
                    screen = self.observe(prev=screen, after_seq=screen.seq)
                    stem = trace.screen(screen)
                    desc += " (first click only closed the open menu, clicked again)"
                    self._say("The first click only closed the open menu: clicking again")
                    rec.action = desc
                changed = screens_differ(prev.image, screen.image)

                if action.kind == "scroll":
                    if not changed:
                        exhausted.add(action.direction or "")
                        rec.outcome = "no movement (end reached)"
                        self._say(f"Nothing moved: the {action.direction} end is reached")
                        history.append(f"{desc} → nothing moved, end reached")
                        continue
                    exhausted.discard(_OPPOSITE.get(action.direction or "", ""))
                    rec.outcome = "scrolled"
                    history.append(f"{desc} → scrolled")
                    continue
                if action.kind == "click" and not changed:
                    el = prev.element(action.element_id) if action.element_id is not None else None
                    if el:
                        tried.append(_Tried(el.label, el.bbox, prev.phash))
                    rec.outcome = "no visible effect"
                    self._say(f"No visible effect ({desc})")
                    history.append(f"{desc} → no visible effect")
                    continue
                if action.kind == "click":
                    exhausted.clear()  # a new view can scroll again

                # changed screen: let the next decision judge it (progress question)
                rec.outcome = "screen changed"
                last, last_desc, last_record, before = action, desc, rec, prev
            else:
                result.message = f"stopped after {max_steps} steps"
        except Exception as e:  # report, don't crash the server
            log.exception("run failed")
            result.status, result.message = "error", f"{type(e).__name__}: {e}"

        if self.last_screen is not None:
            result.final_screen = self.last_screen.describe(3000)
        result.seconds = time.time() - t_start
        trace.result(result.to_json())
        self._trace = None
        self._emit({"type": "run_end", "status": result.status, "message": result.message,
                    "steps": len(result.steps), "seconds": round(result.seconds, 1),
                    "_result": result})  # fmt: skip
        return result

    def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event({"ts": time.time(), **event})
            except Exception:  # dashboard problems must never break a run
                log.debug("event sink failed", exc_info=True)


_OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


def _click_only_closed_popup(prev: Screen, now: Screen, element_id: int | None) -> UIElement | None:
    """If the click landed outside a menu/popup that had just appeared, the popup is gone and the
    clicked element is still in place, the click was spent on closing the popup. Returns the
    element on the new screen to click again, else None."""
    popup = set(prev.new_element_ids)
    if element_id is None or not popup or element_id in popup:
        return None
    target = prev.element(element_id)
    if target is None:
        return None
    popup_labels = {e.label for e in prev.elements if e.id in popup}
    still_open = popup_labels & {e.label for e in now.elements}
    if len(still_open) > 0.3 * len(popup_labels):
        return None
    return next((e for e in now.elements if e.label == target.label and iou(e.bbox, target.bbox) > 0.5), None)


def _visible(text: str, screen: Screen) -> bool:
    t = text.strip().lower()
    return any(t in e.label.lower() for e in screen.elements)


def _excluded_ids(screen: Screen, tried: list[_Tried]) -> set[int]:
    out = set()
    for t in tried:
        if hamming(t.phash, screen.phash) > 40:  # a different view; the old failure does not apply
            continue
        for e in screen.elements:
            if e.label == t.label and iou(e.bbox, t.bbox) > 0.5:
                out.add(e.id)
    return out


def _fuzzy_score(query: str, label: str) -> float:
    """Best similarity of the query to any run of the same number of words in the label."""
    qw, lw = query.split(), label.split()
    n = len(qw)
    if not n or len(lw) < n:
        return SequenceMatcher(None, query, label).ratio() if lw else 0.0
    return max(SequenceMatcher(None, query, " ".join(lw[i : i + n])).ratio() for i in range(len(lw) - n + 1))


def find_element(screen: Screen, query: str, min_similarity: float = 0.75) -> UIElement | None:
    """Best element whose label contains the query (case-insensitive), shortest label wins.

    Falls back to fuzzy matching, because OCR misreads a letter now and then,
    for example when the pointer rests on the text ("Medipthek" for "Mediathek").
    """
    q = query.strip().lower()
    hits = [e for e in screen.elements if q in e.label.lower()]
    if hits:
        return min(hits, key=lambda e: len(e.label))
    scored = [(_fuzzy_score(q, e.label.lower()), e) for e in screen.elements]
    best = max(scored, key=lambda se: (se[0], -len(se[1].label)), default=None)
    return best[1] if best and best[0] >= min_similarity else None

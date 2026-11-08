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
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from ..decision.decider import Action, Decider, DecisionContext
from ..kvm.actuation import Actuator
from ..kvm.device import KvmDevice
from ..perception.parser import UIParser
from ..perception.screen import changed_fraction, dhash, hamming, iou, new_elements, screens_differ
from ..perception.types import Screen, UIElement
from .trace import Trace

log = logging.getLogger(__name__)

Status = Literal["done", "stuck", "max_steps", "time_budget", "cancelled", "error"]
PROGRESS_THRESHOLD = 0.5


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
        self.last_screen: Screen | None = None
        self.lock = threading.Lock()  # one run (or manual action) at a time
        self._cancel = threading.Event()

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
        t0 = time.time()
        elements = self.parser.parse(frame.image)
        screen = Screen(
            image=frame.image,
            elements=elements,
            seq=frame.seq,
            phash=dhash(frame.image),
            captured_at=frame.captured_at,
            parse_ms=int((time.time() - t0) * 1000),
        )
        if prev is not None:
            screen.new_element_ids = new_elements(prev.elements, elements)
            # a full page change marks everything as new; only flag a partial change (popup, menu)
            if len(screen.new_element_ids) > 0.6 * max(1, len(elements)):
                screen.new_element_ids = []
        self.last_screen = screen
        self._emit(
            {"type": "screen", "seq": screen.seq, "elements": len(elements), "parse_ms": screen.parse_ms}
        )
        return screen

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
        self._emit({"type": "run_start", "goal": goal})

        try:
            screen = self.observe()
            stem = trace.screen(screen)
            for step in range(1, max_steps + 1):
                if self._cancel.is_set():
                    result.status, result.message = "cancelled", "cancelled by caller"
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
                )
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
                        if last.kind == "click" and before is not None and last.element_id is not None:
                            el = before.element(last.element_id)
                            if el:
                                tried.append(_Tried(el.label, el.bbox, before.phash))
                        prev = screen
                        if screen.new_element_ids or last.kind == "click":
                            self.actuator.press("esc")
                            screen = self.observe(prev=prev, after_seq=prev.seq)
                            stem = trace.screen(screen)
                        last = last_desc = last_record = before = None
                        continue
                    last_record.outcome = last_record.outcome or "ok"
                    history.append(f"{last_desc} → ok")
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
                self._emit({"type": "decision", "step": step, "action": desc, "confidence": action.confidence,
                            "alternatives": action.alternatives, "element_id": action.element_id})  # fmt: skip
                trace.highlight(stem, screen, action.element_id)

                if action.kind == "done":
                    rec.outcome = "goal reached"
                    result.status, result.message = "done", "decision model reports the goal is achieved"
                    break
                if action.kind == "stuck":
                    rec.outcome = "gave up"
                    result.status, result.message = "stuck", "no visible element or scroll direction helps"
                    break

                prev = screen
                self.execute(action, screen, inputs)
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
                    rec.action = desc
                changed = screens_differ(prev.image, screen.image)

                if action.kind == "scroll":
                    if not changed:
                        exhausted.add(action.direction or "")
                        rec.outcome = "no movement (end reached)"
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
        self._emit({"type": "run_end", "status": result.status, "message": result.message})
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

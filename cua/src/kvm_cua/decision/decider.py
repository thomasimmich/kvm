"""Which element next? — the CUA's decision step.

Each turn becomes ONE Jev call with up to two typed questions:

* ``progress`` (noul, only after an action): did the last action move toward
  the goal? A "no" makes the loop discard that attempt and try another element.
* ``next`` (choice): the next action. Options are every visible element
  (``el_<id>``) plus scroll directions, typing a provided input, Enter, Escape
  (dismiss a popup that appeared), ``done`` and ``stuck``.

Jev only chooses; it never writes text. Anything to type must come in as a
named input from the caller (the choreo's LLM step).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..perception.types import Screen, UIElement, position_words
from .jev import CHOICE_MAX_OPTIONS, JevClient, choice, noul

ActionKind = Literal["click", "scroll", "type", "press", "dismiss", "done", "stuck"]
SCROLL_DIRECTIONS = ("down", "up", "right", "left")


@dataclass
class Action:
    kind: ActionKind
    option: str
    element_id: int | None = None
    direction: str | None = None
    input_name: str | None = None
    key: str | None = None
    confidence: float = 1.0
    alternatives: dict[str, float] = field(default_factory=dict)  # top options by probability

    def describe(self, screen: Screen | None = None) -> str:
        if self.kind == "click" and screen is not None and self.element_id is not None:
            el = screen.element(self.element_id)
            if el:
                return f"click {el.kind} '{el.label}' at {position_words(el, screen.size)}"
        if self.kind == "scroll":
            return f"scroll {self.direction}"
        if self.kind == "type":
            return f"type input '{self.input_name}'"
        if self.kind == "press":
            return f"press {self.key}"
        return self.kind


@dataclass
class DecisionContext:
    goal: str
    screen: Screen
    hints: list[str] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    excluded: set[int] = field(default_factory=set)  # elements already tried without success on this screen
    exhausted_scroll: set[str] = field(default_factory=set)
    last_action: str | None = None  # set → also ask the progress question
    before: Screen | None = None
    guidance: list[str] = field(default_factory=list)  # what a human said during the run, oldest first


@dataclass
class Decision:
    action: Action
    progress: float | None = None  # P(last action helped); None when not asked
    raw: dict | None = None
    tokens: int = 0


class Decider(Protocol):
    name: str

    def decide(self, ctx: DecisionContext) -> Decision: ...


# --- shared option building -------------------------------------------------

_WORD = re.compile(r"[\wäöüß]+", re.UNICODE)


def _tokens(s: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(s) if len(w) > 1}


def relevance(el: UIElement, goal_tokens: set[str]) -> float:
    t = _tokens(el.label)
    if not t:
        return 0.0
    return len(t & goal_tokens) / len(t) + 0.3 * len(t & goal_tokens)


def build_options(ctx: DecisionContext, max_elements: int = CHOICE_MAX_OPTIONS - 16) -> dict[str, str]:
    screen = ctx.screen
    new = set(screen.new_element_ids)
    els = [e for e in screen.elements if e.id not in ctx.excluded]
    if len(els) > max_elements:
        gt = _tokens(" ".join([ctx.goal, *ctx.hints]))
        els = sorted(els, key=lambda e: (-relevance(e, gt), -(e.id in new), e.id))[:max_elements]
    options: dict[str, str] = {}
    for e in sorted(els, key=lambda e: e.id):
        flag = ", just appeared" if e.id in new else ""
        options[f"el_{e.id}"] = f"Click the {e.kind} '{e.label}' at {position_words(e, screen.size)}{flag}"
    for d in SCROLL_DIRECTIONS:
        if d not in ctx.exhausted_scroll:
            options[f"scroll_{d}"] = (
                f"Scroll {d}: the element needed for the goal is probably outside the visible area"
            )
    for name, value in ctx.inputs.items():
        preview = value if len(value) <= 60 else value[:57] + "…"
        options[f"type_{name}"] = (
            f"Type the input '{name}' (\"{preview}\") into the focused text field — only if that field is focused"
        )
    options["press_enter"] = "Press Enter to submit or confirm what was just typed or selected"
    if new:
        options["dismiss"] = (
            "Press Escape to close the menu/dialog that just appeared, because it does not help"
        )
    options["done"] = "The goal is already fully achieved on the current screen; nothing else to do"
    options["stuck"] = "Nothing visible helps and scrolling will not either; give up"
    return options


def parse_option(option: str, probs: dict[str, float] | None = None, confidence: float = 1.0) -> Action:
    alts = dict(sorted((probs or {}).items(), key=lambda kv: -kv[1])[:5])
    if option.startswith("el_"):
        return Action("click", option, element_id=int(option[3:]), confidence=confidence, alternatives=alts)
    if option.startswith("scroll_"):
        return Action("scroll", option, direction=option[7:], confidence=confidence, alternatives=alts)
    if option.startswith("type_"):
        return Action("type", option, input_name=option[5:], confidence=confidence, alternatives=alts)
    if option == "press_enter":
        return Action("press", option, key="enter", confidence=confidence, alternatives=alts)
    if option in ("dismiss", "done", "stuck"):
        return Action(option, option, confidence=confidence, alternatives=alts)  # type: ignore[arg-type]
    raise ValueError(f"unknown option {option!r}")


def build_state(ctx: DecisionContext, max_screen_chars: int = 24000) -> str:
    parts = [f"GOAL: {ctx.goal}"]
    if ctx.guidance:
        parts.append(
            "HUMAN GUIDANCE (latest first, highest priority — follow it over the hints):\n"
            + "\n".join(f"- {g}" for g in reversed(ctx.guidance[-5:]))
        )
    if ctx.hints:
        parts.append("HINTS:\n" + "\n".join(f"- {h}" for h in ctx.hints))
    if ctx.inputs:
        parts.append("INPUTS AVAILABLE TO TYPE: " + ", ".join(ctx.inputs))
    if ctx.history:
        parts.append(
            "ACTIONS SO FAR:\n" + "\n".join(f"{i + 1}. {h}" for i, h in enumerate(ctx.history[-12:]))
        )
    if ctx.last_action:
        parts.append(f"LAST ACTION: {ctx.last_action}")
        new_ids = set(ctx.screen.new_element_ids)
        appeared = sorted({e.label for e in ctx.screen.elements if e.id in new_ids})
        if appeared:
            parts.append("APPEARED (new menu, dialog or page content): " + ", ".join(appeared[:40]))
        if ctx.before is not None:
            gone = {e.label for e in ctx.before.elements} - {e.label for e in ctx.screen.elements}
            if gone:
                parts.append("DISAPPEARED: " + ", ".join(sorted(gone)[:40]))
    w, h = ctx.screen.size
    parts.append(f"CURRENT SCREEN ({w}x{h}, elements as [id] kind 'label' at position (x,y) size):")
    parts.append(ctx.screen.describe(max_screen_chars))
    return "\n\n".join(parts)


NEXT_INSTRUCTIONS = (
    "You are operating a computer on behalf of a user, through mouse and keyboard only. "
    "Pick the single next action that makes the most progress toward the GOAL on the CURRENT SCREEN. "
    "Prefer elements whose label matches the goal or the hints; follow the hints' advice about the application. "
    "Do not repeat actions that already failed. Choose 'done' only when the goal is visibly achieved. "
    "When there is HUMAN GUIDANCE, the person watching knows the way: do what its latest line says. "
    "Once the app the goal needs is open, prefer elements inside its window: the app's name in the menu bar "
    "only opens a menu with About, Hide and Quit."
)

PROGRESS_INSTRUCTIONS = (
    "Judge the LAST ACTION by comparing what APPEARED, what DISAPPEARED and what is now on the CURRENT SCREEN. "
    "Did it bring the user closer to the GOAL? It did if it opened a menu, dialog, page or field that offers "
    "the next step toward the goal (for example a menu that contains the item the goal or the hints mention), "
    "or if it completed the goal."
)


class JevDecider:
    name = "jev"

    def __init__(self, client: JevClient):
        self.client = client

    def decide(self, ctx: DecisionContext) -> Decision:
        options = build_options(ctx)
        questions: dict[str, dict] = {"next": choice(NEXT_INSTRUCTIONS, options)}
        if ctx.last_action:
            questions["progress"] = noul(
                PROGRESS_INSTRUCTIONS,
                true="the screen now shows what the goal needs next, or the goal is achieved",
                false="nothing relevant happened, or the action led somewhere unrelated to the goal",
            )
        res = self.client.decide(build_state(ctx), questions)
        nxt = res.answers["next"]
        action = parse_option(nxt["choice"], nxt.get("probabilities"), float(nxt.get("confidence", 1.0)))
        progress = float(res.answers["progress"]["noul"]) if "progress" in res.answers else None
        return Decision(action, progress, raw=res.answers, tokens=res.input_tokens + res.output_tokens)


class StubDecider:
    """Deterministic keyword matcher — for tests and for running without a Jev key.

    It clicks the best label match for the goal + hints, types inputs once a
    text field was clicked, and reports progress whenever the screen changed.
    """

    name = "stub"

    def __init__(self, done_when: str | None = None):
        self.done_when = done_when.lower() if done_when else None

    def decide(self, ctx: DecisionContext) -> Decision:
        progress = None
        if ctx.last_action:
            changed = ctx.before is None or ctx.before.phash != ctx.screen.phash
            progress = 1.0 if changed else 0.0
        if self.done_when and any(self.done_when in e.label.lower() for e in ctx.screen.elements):
            return Decision(Action("done", "done"), progress)
        typed = {h for h in ctx.history if h.startswith("type input")}
        for name in ctx.inputs:
            if f"type input '{name}'" not in typed and ctx.history and ctx.history[-1].startswith("click"):
                return Decision(Action("type", f"type_{name}", input_name=name), progress)
        history = " | ".join(h for h in ctx.history if not h.startswith("human"))
        if ctx.guidance and ctx.history and ctx.history[-1].startswith("human"):
            # guidance that just arrived beats the goal's keywords (once)
            gg = _tokens(ctx.guidance[-1])
            hits = [e for e in ctx.screen.elements if e.id not in ctx.excluded and relevance(e, gg) > 0]
            if hits:
                e = max(hits, key=lambda e: relevance(e, gg))
                return Decision(Action("click", f"el_{e.id}", element_id=e.id), progress)
        gt = _tokens(" ".join([ctx.goal, *ctx.hints]))
        best, best_score = None, 0.0
        for e in ctx.screen.elements:
            if e.id in ctx.excluded or f"'{e.label}'" in history:
                continue
            s = relevance(e, gt)
            if s > best_score:
                best, best_score = e, s
        if best is not None:
            return Decision(
                Action("click", f"el_{best.id}", element_id=best.id, confidence=min(1.0, best_score)),
                progress,
            )
        for d in SCROLL_DIRECTIONS:
            if d not in ctx.exhausted_scroll:
                return Decision(Action("scroll", f"scroll_{d}", direction=d), progress)
        return Decision(Action("stuck", "stuck"), progress)

from __future__ import annotations

from conftest import elements, make_screen

from kvm_cua.agent.loop import Agent
from kvm_cua.decision.decider import Action, Decision, DecisionContext, StubDecider
from kvm_cua.kvm.actuation import Actuator
from kvm_cua.kvm.device import FakeKvm
from kvm_cua.perception.parser import FixtureParser

HOME = [("Files", (20, 20, 120, 60)), ("Settings", (680, 20, 780, 60))]
SETTINGS = [("General", (20, 100, 200, 140)), ("Wi-Fi", (20, 160, 200, 200)), ("Back", (20, 20, 100, 60))]
WIFI = [("Wi-Fi is on", (300, 100, 600, 140)), ("Back", (20, 20, 100, 60))]


def agent_for(screens_labels, decider, tmp_path, advance=True):
    imgs = [make_screen(lbls) for lbls in screens_labels]
    parser = FixtureParser([elements(lbls) for lbls in screens_labels]).bind(imgs)
    kvm = FakeKvm(imgs, advance_on_action=advance)
    agent = Agent(
        kvm,
        parser,
        decider,
        Actuator(kvm, seed=1),
        runs_dir=tmp_path,
        settle_interval_s=0.0,
        settle_timeout_s=0.0,
    )
    return agent, kvm


def test_stub_reaches_goal_via_two_clicks(tmp_path):
    agent, kvm = agent_for([HOME, SETTINGS, WIFI], StubDecider(done_when="wi-fi is on"), tmp_path)
    res = agent.run_goal("open settings and the wi-fi page", hints=["Wi-Fi is inside Settings"])
    assert res.status == "done", res.summary()
    clicked = [s.action for s in res.steps if s.action.startswith("click")]
    assert clicked == ["click text 'Settings' at top-right", "click text 'Wi-Fi' at top-left"]
    assert kvm.index == 2
    assert (tmp_path / res.trace_dir.split("/")[-1] / "result.json").exists()


def test_expect_text_stops_early(tmp_path):
    agent, _ = agent_for([HOME, SETTINGS, WIFI], StubDecider(), tmp_path)
    res = agent.run_goal("open settings", expect_text="General")
    assert res.status == "done" and "General" in res.message


class Scripted:
    """Returns a fixed sequence of decisions and records the contexts it saw."""

    name = "scripted"

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.contexts: list[DecisionContext] = []

    def decide(self, ctx):
        self.contexts.append(ctx)
        return self.decisions.pop(0)


def test_discard_presses_escape_and_excludes_element(tmp_path):
    # click 'Files' opens an unrelated view; Jev says no progress → Esc, 'Files' excluded, then 'Settings'
    dec = Scripted(
        [
            Decision(Action("click", "el_0", element_id=0)),
            Decision(Action("stuck", "stuck"), progress=0.1),
            Decision(Action("click", "el_1", element_id=1)),
            Decision(Action("done", "done"), progress=0.9),
        ]
    )
    agent, kvm = agent_for([HOME, SETTINGS, HOME, WIFI], dec, tmp_path)
    res = agent.run_goal("open settings")
    assert res.status == "done", res.summary()
    assert res.steps[0].outcome == "no progress → discarded"
    esc = [e for kind, e in kvm.events if kind == "keys" and e.keys == [0x29]]
    assert esc, "expected an Escape key press to undo"
    third = dec.contexts[2]
    assert 0 in third.excluded and "did not help" in third.history[-1]


def test_scroll_end_marks_direction_exhausted(tmp_path):
    dec = Scripted(
        [
            Decision(Action("scroll", "scroll_down", direction="down")),
            Decision(Action("stuck", "stuck")),
        ]
    )
    agent, _ = agent_for([HOME], dec, tmp_path, advance=False)
    res = agent.run_goal("find the logout button")
    assert res.status == "stuck"
    assert "down" in dec.contexts[1].exhausted_scroll
    assert res.steps[0].outcome == "no movement (end reached)"


def test_click_without_effect_is_excluded(tmp_path):
    dec = Scripted([Decision(Action("click", "el_1", element_id=1)), Decision(Action("stuck", "stuck"))])
    agent, _ = agent_for([HOME], dec, tmp_path, advance=False)
    agent.run_goal("open settings")
    assert 1 in dec.contexts[1].excluded


def test_time_budget_stops_before_next_step(tmp_path):
    dec = Scripted([Decision(Action("scroll", "scroll_down", direction="down"))] * 5)
    agent, _ = agent_for([HOME], dec, tmp_path, advance=False)
    res = agent.run_goal("anything", time_budget_s=0.0)
    assert res.status == "time_budget" and res.steps == []


def test_find_element_tolerates_ocr_typos():
    from PIL import Image

    from kvm_cua.agent.loop import find_element
    from kvm_cua.perception.types import Screen, UIElement

    els = [
        UIElement(0, "icon", "D Medipthek", (10, 10, 200, 40)),
        UIElement(1, "icon", "Sammlungen", (10, 50, 200, 80)),
        UIElement(2, "text", "Zuletzt gesichert", (10, 90, 200, 120)),
    ]
    screen = Screen(Image.new("RGB", (400, 200)), els)
    assert find_element(screen, "Mediathek").id == 0
    assert find_element(screen, "sammlungen").id == 1  # exact substring still wins
    assert find_element(screen, "Zuletzt gesichrt").id == 2
    assert find_element(screen, "Papierkorb") is None

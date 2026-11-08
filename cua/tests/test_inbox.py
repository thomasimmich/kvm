from __future__ import annotations

import threading
import time

from test_loop import HOME, SETTINGS, WIFI, Scripted, agent_for

from kvm_cua.agent.inbox import Command, parse_command
from kvm_cua.decision.decider import Action, Decision, build_state


def test_parse_command_variants():
    c = parse_command("open the Settings menu")
    assert (c.kind, c.text) == ("guidance", "open the Settings menu")
    assert parse_command("Pause!").kind == "pause"
    assert parse_command("weiter").kind == "resume"
    assert parse_command("stop").kind == "cancel"
    c = parse_command('{"command": "answer", "text": "the gear icon"}')
    assert (c.kind, c.text) == ("answer", "the gear icon")
    c = parse_command({"message": "scroll down"})
    assert (c.kind, c.text) == ("guidance", "scroll down")
    assert parse_command({"command": "resume"}).kind == "resume"
    assert parse_command({"command": "guidance"}) is None  # guidance needs text
    assert parse_command({"command": "explode", "text": "x"}) is None
    assert parse_command("") is None and parse_command(None) is None


def test_guidance_reaches_the_next_decision(tmp_path):
    agent_ref = {}

    class Guided(Scripted):
        def decide(self, ctx):
            if not self.contexts:
                agent_ref["a"].command(Command("guidance", "open Wi-Fi"))
            return super().decide(ctx)

    dec = Guided(
        [Decision(Action("click", "el_1", element_id=1)), Decision(Action("done", "done"), progress=0.9)]
    )
    agent, _ = agent_for([HOME, SETTINGS, WIFI], dec, tmp_path)
    agent_ref["a"] = agent
    res = agent.run_goal("open settings")
    assert res.status == "done", res.summary()
    second = dec.contexts[1]
    assert second.guidance == ["open Wi-Fi"]
    assert "human: open Wi-Fi" in second.history
    assert "HUMAN GUIDANCE" in build_state(second)


def test_pause_blocks_until_resume(tmp_path):
    events: list[dict] = []
    agent_ref = {}

    class Pausing(Scripted):
        def decide(self, ctx):
            if not self.contexts:
                agent_ref["a"].command(Command("pause"))
                threading.Timer(0.4, lambda: agent_ref["a"].command(Command("resume"))).start()
            return super().decide(ctx)

    dec = Pausing(
        [Decision(Action("click", "el_1", element_id=1)), Decision(Action("done", "done"), progress=0.9)]
    )
    agent, _ = agent_for([HOME, SETTINGS, WIFI], dec, tmp_path)
    agent.on_event = events.append
    agent_ref["a"] = agent
    t0 = time.time()
    res = agent.run_goal("open settings")
    assert res.status == "done"
    assert time.time() - t0 >= 0.4
    types = [e["type"] for e in events]
    assert types.index("paused") < types.index("resumed")


def test_cancel_by_the_human(tmp_path):
    agent_ref = {}

    class Cancelling(Scripted):
        def decide(self, ctx):
            agent_ref["a"].command(Command("cancel"))
            return super().decide(ctx)

    dec = Cancelling([Decision(Action("click", "el_1", element_id=1))] * 3)
    agent, _ = agent_for([HOME, SETTINGS, WIFI], dec, tmp_path)
    agent_ref["a"] = agent
    res = agent.run_goal("open settings")
    assert res.status == "cancelled" and "human" in res.message


def test_stuck_asks_the_human_and_continues_with_the_answer(tmp_path):
    events: list[dict] = []
    dec = Scripted([Decision(Action("stuck", "stuck")), Decision(Action("done", "done"))])
    agent, _ = agent_for([HOME, SETTINGS], dec, tmp_path)
    agent.on_event = events.append
    agent.human_wait_s = 3.0
    threading.Timer(0.2, lambda: agent.command(Command("answer", "it is behind the gear icon"))).start()
    res = agent.run_goal("open settings")
    assert res.status == "done", res.summary()
    assert res.steps[0].outcome == "asked the human"
    assert dec.contexts[1].guidance == ["it is behind the gear icon"]
    assert any(e["type"] == "ask_human" for e in events)


def test_stuck_without_an_answer_gives_up(tmp_path):
    dec = Scripted([Decision(Action("stuck", "stuck"))])
    agent, _ = agent_for([HOME], dec, tmp_path)
    agent.human_wait_s = 0.3
    res = agent.run_goal("open settings")
    assert res.status == "stuck" and res.steps[0].outcome == "gave up"


def test_no_waiting_when_nobody_watches(tmp_path):
    dec = Scripted([Decision(Action("stuck", "stuck"))])
    agent, _ = agent_for([HOME], dec, tmp_path)
    t0 = time.time()
    assert agent.run_goal("open settings").status == "stuck"  # human_wait_s defaults to 0
    assert time.time() - t0 < 1.0


def test_cancel_while_waiting_for_an_answer(tmp_path):
    dec = Scripted([Decision(Action("stuck", "stuck"))])
    agent, _ = agent_for([HOME], dec, tmp_path)
    agent.human_wait_s = 5.0
    threading.Timer(0.2, lambda: agent.command(Command("cancel"))).start()
    t0 = time.time()
    res = agent.run_goal("open settings")
    assert res.status == "cancelled" and "human" in res.message
    assert time.time() - t0 < 2.0

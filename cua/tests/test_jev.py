from __future__ import annotations

import json

import httpx
import pytest
from conftest import elements
from PIL import Image

from kvm_cua.decision.decider import DecisionContext, JevDecider, build_options, parse_option
from kvm_cua.decision.jev import JevClient, JevCredentialError, JevRequestError, choice
from kvm_cua.perception.types import Screen


def client(handler) -> JevClient:
    return JevClient("k", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_request_shape_and_truncation():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": {
                    "q": {
                        "type": "choice",
                        "choice": "a",
                        "probabilities": {"a": 0.9, "b": 0.1},
                        "confidence": 0.9,
                    }
                },
                "usage": {"input_tokens": 5},
            },
        )

    res = client(handler).decide("state", {"q": choice("pick", {"a": "x" * 400, "b": None})})
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "jev-latest"
    assert len(seen["body"]["questions"]["q"]["criteria"]["a"]) == 255
    assert res.answers["q"]["choice"] == "a" and res.input_tokens == 5


def test_retry_then_errors():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.2}}})

    assert (
        client(handler).decide("s", {"q": {"type": "noul", "instructions": "?"}}).answers["q"]["noul"] == 0.2
    )
    assert calls["n"] == 3
    with pytest.raises(JevCredentialError):
        client(lambda r: httpx.Response(401)).decide("s", {})
    with pytest.raises(JevRequestError):
        client(lambda r: httpx.Response(422, text="bad question")).decide("s", {})


def _screen():
    labels = [("File", (0, 0, 40, 20)), ("Settings", (700, 10, 790, 40)), ("Help", (50, 0, 90, 20))]
    return Screen(image=Image.new("RGB", (800, 600)), elements=elements(labels))


def test_options_and_exclusion():
    ctx = DecisionContext(goal="open settings", screen=_screen(), inputs={"q": "hello"}, excluded={0})
    opts = build_options(ctx)
    assert "el_0" not in opts and "el_1" in opts
    assert "top-right" in opts["el_1"]
    assert {"scroll_down", "type_q", "press_enter", "done", "stuck"} <= set(opts)
    assert "dismiss" not in opts  # nothing new on screen
    assert parse_option("el_7").element_id == 7
    assert parse_option("scroll_left").direction == "left"
    assert parse_option("type_q").input_name == "q"


def test_jev_decider_asks_progress_only_after_action():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        answers = {
            "next": {"type": "choice", "choice": "el_1", "probabilities": {"el_1": 0.8}, "confidence": 0.8}
        }
        if "progress" in body["questions"]:
            answers["progress"] = {"type": "noul", "noul": 0.3}
        return httpx.Response(200, json={"answers": answers})

    d = JevDecider(client(handler))
    first = d.decide(DecisionContext(goal="open settings", screen=_screen()))
    assert first.action.element_id == 1 and first.progress is None
    second = d.decide(DecisionContext(goal="open settings", screen=_screen(), last_action="click 'File'"))
    assert second.progress == pytest.approx(0.3)
    assert "LAST ACTION: click 'File'" in bodies[1]["state"]
    assert "GOAL: open settings" in bodies[0]["state"]


def test_state_lists_appeared_elements():
    from kvm_cua.decision.decider import build_state

    scr = _screen()
    scr.new_element_ids = [scr.elements[1].id]
    ctx = DecisionContext(goal="open settings", screen=scr, last_action="click icon 'Apple'", before=_screen())
    state = build_state(ctx)
    assert f"APPEARED (new menu, dialog or page content): {scr.elements[1].label}" in state

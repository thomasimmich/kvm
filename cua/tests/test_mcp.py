from __future__ import annotations

import pytest
from conftest import elements, make_screen
from starlette.testclient import TestClient

from kvm_cua.config import Settings
from kvm_cua.decision.decider import StubDecider
from kvm_cua.kvm.device import FakeKvm
from kvm_cua.mcp_server import BearerAuth, build_server
from kvm_cua.perception.parser import FixtureParser
from kvm_cua.service import CuaService

HOME = [("Files", (20, 20, 120, 60)), ("Settings", (680, 20, 780, 60))]
SETTINGS = [("General", (20, 100, 200, 140))]


def service(tmp_path):
    imgs = [make_screen(HOME), make_screen(SETTINGS)]
    parser = FixtureParser([elements(HOME), elements(SETTINGS)]).bind(imgs)
    s = Settings(cua_runs_dir=tmp_path, cua_sphero_state_file=tmp_path / "sphero.json", _env_file=None)
    svc = CuaService(s, device=FakeKvm(imgs), parser=parser, decider=StubDecider(done_when="general"))
    svc.agent.settle_interval_s = svc.agent.settle_timeout_s = 0.0
    return svc


async def test_tools_prompt_and_run(tmp_path):
    mcp = build_server(service(tmp_path))
    names = {t.name for t in await mcp.list_tools()}
    assert {
        "cua_run_goal",
        "cua_observe",
        "cua_click_element",
        "cua_click_text",
        "cua_scroll",
        "cua_type_text",
        "cua_press_keys",
    } <= names
    prompts = {p.name for p in await mcp.list_prompts()}
    assert "additional_agent_instructions" in prompts
    prompt = await mcp.get_prompt("additional_agent_instructions")
    assert "cua_run_goal" in str(prompt)

    out = await mcp.call_tool("cua_observe", {})
    assert "'Settings'" in str(out)
    out = await mcp.call_tool("cua_run_goal", {"goal": "open settings"})
    assert "status: done" in str(out)


def test_bearer_auth(tmp_path):
    svc = service(tmp_path)

    def app():  # the MCP session manager runs once per app instance
        return BearerAuth(build_server(svc).streamable_http_app(host="127.0.0.1"), "secret")

    with TestClient(app(), client=("10.0.0.5", 1234)) as c:
        assert c.get("/healthz").status_code == 200
        assert c.post("/mcp", json={}).status_code == 401
        assert c.get("/dashboard").status_code == 401
        assert c.get("/dashboard?token=secret").status_code == 200
        assert c.get("/dashboard/api/state", headers={"Authorization": "Bearer secret"}).status_code == 200
    with TestClient(app(), client=("127.0.0.1", 1234)) as c:
        assert c.get("/dashboard/api/state").status_code == 200
        assert c.post("/mcp", json={}).status_code == 401


def test_dashboard_pointer_wheel_and_sphero(tmp_path):
    from test_sphero import FakeSphero

    from kvm_cua.kvm.device import ABS_MAX

    svc = service(tmp_path)
    fake = FakeSphero()
    svc.sphero.client_factory = fake.factory
    kvm = svc.device
    with TestClient(build_server(svc).streamable_http_app(host="127.0.0.1")) as c:
        assert c.get("/dashboard/api/state").json()["pointer"] is None
        r = c.post("/dashboard/api/pointer", json={"x": 1.0, "y": 0.5, "buttons": 1})
        assert r.status_code == 200, r.text
        ev = [e for kind, e in kvm.events if kind == "mouse"][-1]
        assert (ev.x, ev.buttons) == (ABS_MAX, 1)
        assert c.post("/dashboard/api/pointer", json={"x": "a", "y": 0}).status_code == 400

        assert c.post("/dashboard/api/wheel", json={"x": 0.5, "y": 0.5, "dy": -2}).status_code == 200
        assert [e.dy for kind, e in kvm.events if kind == "wheel"] == [-2]

        st = c.get("/dashboard/api/state").json()
        assert st["pointer"]["x"] == pytest.approx(50, abs=0.2)  # the wheel moved the pointer first
        assert st["sphero"]["enabled"] is False

        r = c.post("/dashboard/api/sphero", json={"enabled": True, "url": "10.0.0.9"})
        assert r.json()["result"]["url"] == "http://10.0.0.9:8765"
        c.post("/dashboard/api/pointer", json={"x": 0.25, "y": 0.75})
        assert wait(lambda: fake.targets() and fake.targets()[-1] == {"x": 25.0, "y": 75.0})
        assert c.post("/dashboard/api/sphero/stop").status_code == 200
        c.post("/dashboard/api/sphero", json={"enabled": False})
        assert c.get("/dashboard/api/sphero").json()["result"]["enabled"] is False

        svc.live.running = True
        assert c.post("/dashboard/api/pointer", json={"x": 0, "y": 0}).status_code == 409


def wait(cond, timeout=2.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_dashboard_reset(tmp_path):
    svc = service(tmp_path)
    svc.live.events.append({"type": "activity", "text": "old", "ts": 0})
    svc.live.last_result = {"status": "done", "message": "old"}
    with TestClient(build_server(svc).streamable_http_app(host="127.0.0.1")) as c:
        svc.live.running = True
        assert c.post("/dashboard/api/reset").status_code == 409
        svc.live.running = False
        assert c.post("/dashboard/api/reset").json() == {"ok": True}
        st = c.get("/dashboard/api/state").json()
        assert st["events"] == [] and st["lastResult"] is None

from __future__ import annotations

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
    s = Settings(cua_runs_dir=tmp_path, _env_file=None)
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

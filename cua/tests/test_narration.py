from __future__ import annotations

import threading
import time

from conftest import elements, make_screen
from starlette.testclient import TestClient
from test_loop import HOME, SETTINGS, WIFI, agent_for

from kvm_cua.agent.inbox import Command
from kvm_cua.config import Settings
from kvm_cua.decision.decider import StubDecider
from kvm_cua.kvm.device import FakeKvm
from kvm_cua.mcp_server import BearerAuth, build_server
from kvm_cua.narration.agent_server import SessionHandle
from kvm_cua.narration.leanscope_auth import ChoreoSelection, LeanScopeAuth
from kvm_cua.narration.publisher import NarrationPublisher
from kvm_cua.narration.session import NarrationSessions
from kvm_cua.perception.parser import FixtureParser
from kvm_cua.service import CuaService

ACTIVITY = "http://agent/api/v1/activities/act-1"


class FakeAgentServer:
    """Records what the CUA writes; can be slow or fail on purpose."""

    base_url = "http://agent/"

    def __init__(self, delay_s: float = 0.0, fail_uploads: bool = False):
        self.delay_s = delay_s
        self.fail_uploads = fail_uploads
        self.calls: list[tuple] = []
        self.on_start = None  # called when the session's start gate opens (simulates the dispatch)

    def absolute(self, url):
        return url if "://" in url else "http://agent" + url

    def run_choreo(self, repository_id, scenario_guid, initial_input=None, session_repository_id=None):
        self.calls.append(("run_choreo", repository_id, scenario_guid, initial_input))
        return SessionHandle("tok-1", "scn-1", ui_url="http://agent/?sessionToken=tok-1")

    def start(self, token):
        self.calls.append(("start", token))
        if self.on_start:
            threading.Timer(0.05, self.on_start).start()

    def upload_file(self, token, data, filename, mime):
        time.sleep(self.delay_s)
        if self.fail_uploads:
            raise RuntimeError("upload refused")
        self.calls.append(("upload", filename, mime, len(data)))
        return f"http://agent/api/v1/files/{filename}"

    def patch_activity(self, url, body):
        time.sleep(self.delay_s)
        self.calls.append(("patch", url, body))

    def texts(self):
        return [c[2]["append"]["text"] for c in self.calls if c[0] == "patch" and "append" in c[2]]


def run_with_publisher(tmp_path, client, describer=None):
    agent, _ = agent_for([HOME, SETTINGS, WIFI], StubDecider(done_when="wi-fi is on"), tmp_path)
    agent.describer = describer
    pub = NarrationPublisher(client, "tok-1", ACTIVITY, max_steps=15)
    agent.on_event = pub
    res = agent.run_goal("open settings and the wi-fi page", hints=["Wi-Fi is inside Settings"])
    time.sleep(0.1)  # let the description thread finish
    pub.close()
    return res, pub


def test_publisher_narrates_screens_decisions_and_result(tmp_path):
    client = FakeAgentServer()
    res, pub = run_with_publisher(tmp_path, client, describer=lambda img: "A desktop with two buttons.")
    assert res.status == "done"
    assert client.texts()[0] == "Starting: **open settings and the wi-fi page**"
    texts = [t for t in client.texts() if t.startswith("[")]
    tags = [t.split("]")[0] + "]" for t in texts]
    assert tags[0] == "[activity]" and "[screen:1]" in tags
    assert any("Jev answered in" in t for t in texts)
    assert "[desc:1]" in tags and "[elements:1]" in tags and "[decision:1]" in tags
    assert tags[-1] == "[result]"
    # the raw screenshot is uploaded before the entry that shows it
    first_upload = next(i for i, c in enumerate(client.calls) if c[0] == "upload")
    screen_entry = next(i for i, c in enumerate(client.calls)
                        if c[0] == "patch" and c[2].get("append", {}).get("text", "").startswith("[screen:1]"))  # fmt: skip
    assert first_upload < screen_entry
    assert "![Screen 1](http://agent/api/v1/files/screen-001.jpg)" in texts[tags.index("[screen:1]")]
    last = client.calls[-1][2]
    assert last["phase"] == "succeeded" and last["summary"].startswith("done")
    assert pub.failures == 0


def test_publisher_survives_failing_uploads(tmp_path):
    client = FakeAgentServer(fail_uploads=True)
    res, pub = run_with_publisher(tmp_path, client)
    assert res.status == "done"
    assert any(t.startswith("[elements:1]") and "![" not in t for t in client.texts())
    assert pub.failures > 0


def test_slow_agent_server_does_not_slow_the_run(tmp_path):
    client = FakeAgentServer(delay_s=0.2)
    agent, _ = agent_for([HOME, SETTINGS, WIFI], StubDecider(done_when="wi-fi is on"), tmp_path)
    pub = NarrationPublisher(client, "tok-1", ACTIVITY)
    agent.on_event = pub
    t0 = time.time()
    agent.run_goal("open settings and the wi-fi page", hints=["Wi-Fi is inside Settings"])
    assert time.time() - t0 < 1.5  # ~30 writes x 0.2 s would take 6 s if they blocked
    pub._closed = True  # don't wait for the backlog in the test


def logged_in(selection: ChoreoSelection | None = None) -> LeanScopeAuth:
    auth = LeanScopeAuth("https://supabase.example", "anon")
    auth._refresh_token, auth.email = "refresh", "me@example.com"
    auth.selection = selection or ChoreoSelection("145", "choreo-guid")
    return auth


def sessions(client, auth=None):
    return NarrationSessions(client, auth or logged_in(), remote_control_url="https://rc.example/app",
                             dispatch_timeout_s=2.0)  # fmt: skip


def dispatch_body(token="tok-1", **ctx):
    return {"sessionToken": token, "config": {
        "context": {"activityGuid": "act-1", **ctx},
        "endpoints": {"activityApiUrl": ACTIVITY, "agentServerUrl": "https://agent.example/"}}}  # fmt: skip


def test_session_begin_waits_for_the_dispatch_and_routes_commands():
    client = FakeAgentServer()
    ns = sessions(client)
    got: list[Command] = []
    ns.on_command = got.append
    client.on_start = lambda: ns.on_dispatch(dispatch_body())
    run = ns.begin("open settings", ["hint"], ["password"], 15)
    assert run.status == "running" and run.activity_url == ACTIVITY
    assert client.calls[0] == ("run_choreo", "145", "choreo-guid",
                               {"goal": "open settings", "hints": ["hint"], "inputNames": ["password"]})  # fmt: skip
    assert ns.remote_control_link(run) == (
        "https://rc.example/app?agentServer=https%3A%2F%2Fagent.example&sessionToken=tok-1"
    )
    assert ns.state()["qrSvg"].startswith("<svg")

    status, body = ns.on_dispatch(dispatch_body(input={"command": "guidance", "text": "open Wi-Fi"}))
    assert status == 200 and "open Wi-Fi" in body["summary"]
    assert (got[0].kind, got[0].text) == ("guidance", "open Wi-Fi")

    assert ns.on_dispatch(dispatch_body())[0] == 409  # dispatched twice
    assert ns.on_dispatch(dispatch_body(token="other"))[0] == 409
    ns.end(run)
    assert run.status == "settled"


def test_session_without_dispatch_runs_unnarrated():
    ns = sessions(FakeAgentServer())
    ns.dispatch_timeout_s = 0.1
    run = ns.begin("open settings", [], [], 15)
    assert run.status == "no_dispatch" and run.publisher is None and "CUA_PUBLIC_URL" in run.error


def test_session_without_login_or_choreo_runs_unnarrated():
    client = FakeAgentServer()
    auth = LeanScopeAuth("https://supabase.example", "anon")
    run = sessions(client, auth).begin("open settings", [], [], 15)
    assert run.status == "not_configured" and "not logged in" in run.error and not client.calls
    run = sessions(client, logged_in(ChoreoSelection())).begin("open settings", [], [], 15)
    assert run.status == "not_configured" and "no choreo picked" in run.error and not client.calls


def test_private_callback_origin_falls_back_to_our_agent_server_url():
    ns = sessions(FakeAgentServer())
    body = dispatch_body()
    body["config"]["endpoints"]["activityApiUrl"] = '<Run "brew install cloudflared" …>'
    assert ns._activity_url(body["config"]["endpoints"], body["config"]["context"]) == (
        "http://agent/api/v1/activities/act-1"
    )


def narrated_service(tmp_path, key="", narrate=True):
    imgs = [make_screen(HOME), make_screen(SETTINGS)]
    parser = FixtureParser([elements(HOME), elements(SETTINGS)]).bind(imgs)
    s = Settings(cua_runs_dir=tmp_path, cua_worker_key=key, leanscope_agent_server_url="http://agent",
                 cua_state_file=tmp_path / "state.json", cua_vision_provider="none", _env_file=None)  # fmt: skip
    svc = CuaService(s, device=FakeKvm(imgs), parser=parser, decider=StubDecider(done_when="general"))
    svc.agent.settle_interval_s = svc.agent.settle_timeout_s = 0.0
    fake = FakeAgentServer()
    svc.narration.client = fake
    svc.narration.auth = logged_in()
    svc.narration.auth.narrate = narrate
    fake.on_start = lambda: svc.narration.on_dispatch(dispatch_body())
    return svc, fake


def test_worker_route_key_and_dispatch(tmp_path):
    svc, _ = narrated_service(tmp_path, key="k1")
    app = BearerAuth(build_server(svc).streamable_http_app(host="127.0.0.1"), "secret")
    with TestClient(app, client=("10.0.0.5", 1234)) as c:
        assert c.post("/worker/dispatch", json=dispatch_body()).status_code == 401
        r = c.post("/worker/dispatch?key=k1", json=dispatch_body())
        assert r.status_code == 409  # no run waits for this session
        r = c.post("/worker/dispatch?key=k1", json=dispatch_body(input="pause"))
        assert r.status_code == 200 and "No CUA run" in r.json()["summary"]


def test_service_run_is_narrated_and_links_the_session(tmp_path):
    svc, fake = narrated_service(tmp_path)
    res = svc.run_goal("open settings")
    assert res.status == "done"
    assert res.session_url == "http://agent/?sessionToken=tok-1"
    assert "live session: http://agent/?sessionToken=tok-1" in res.summary()
    assert fake.texts()[-1].startswith("[result]")
    assert svc.narration.current.status == "settled"
    # images stay out of the dashboard's JSON events
    assert all(not k.startswith("_") for e in svc.live.events for k in e)


def test_runs_start_no_session_while_narration_is_off(tmp_path):
    svc, fake = narrated_service(tmp_path, narrate=False)
    res = svc.run_goal("open settings")
    assert res.status == "done" and not res.session_url
    assert fake.calls == [] and svc.narration.current is None

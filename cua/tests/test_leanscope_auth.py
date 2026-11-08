from __future__ import annotations

import json
import stat
import time

import httpx
import pytest
from conftest import make_screen
from starlette.testclient import TestClient
from test_loop import HOME

from kvm_cua.config import Settings
from kvm_cua.decision.decider import StubDecider
from kvm_cua.kvm.device import FakeKvm
from kvm_cua.mcp_server import BearerAuth, build_server
from kvm_cua.narration.agent_server import AgentServerClient
from kvm_cua.narration.leanscope_auth import ChoreoSelection, LeanScopeAuth, LeanScopeCatalog, LeanScopeError
from kvm_cua.service import CuaService


class FakeLeanScope:
    """Supabase token endpoint plus LeanScope GraphQL."""

    def __init__(self):
        self.refresh_ok = True
        self.n = 0
        self.requests: list[httpx.Request] = []
        self.account_fails = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path == "/auth/v1/token":
            body = json.loads(req.content)
            grant = req.url.params["grant_type"]
            if grant == "password":
                if body["password"] != "pw":
                    return httpx.Response(400, json={"error_code": "invalid_credentials",
                                                     "msg": "Invalid login credentials"})  # fmt: skip
            elif not self.refresh_ok or body["refresh_token"] != f"refresh-{self.n}":
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})
            self.n += 1
            return httpx.Response(200, json={"access_token": f"jwt-{self.n}", "refresh_token": f"refresh-{self.n}",
                                             "expires_in": 3600})  # fmt: skip
        if req.url.path == "/graphql":
            q = json.loads(req.content)["query"]
            if "accountRepository" in q:
                if self.account_fails:
                    return httpx.Response(200, json={"errors": [{"message": "INTERNAL_SERVER_ERROR"}]})
                return httpx.Response(200, json={"data": {"accountRepository": {
                    "id": 1, "name": "me", "groupName": "Personal"}}})  # fmt: skip
            if "repositories" in q:
                return httpx.Response(200, json={"data": {"repositories": [
                    {"id": 145, "name": "Choreos", "groupName": "Team"},
                    {"id": 1, "name": "me", "groupName": "Personal"}, None]}})  # fmt: skip
            if "uxmData" in q:
                return httpx.Response(200, json={"data": {"uxmData": {"scenarios": [
                    {"_guid": "g2", "title": "Narration", "type": "Choreo",
                     "personaGoal": " Open the Wi-Fi page ", "preconditions": "Settings is in the dock\n\n Use the search "},
                    {"_guid": "g1", "title": "As-is flow", "type": "AsIs"},
                    {"_guid": "g3", "title": "Session 1", "type": "Conversation"},
                    {"_guid": "g4", "title": "Another choreo", "type": "Choreo"}]}}})  # fmt: skip
        return httpx.Response(404)


def make(tmp_path, fake=None):
    fake = fake or FakeLeanScope()
    client = httpx.Client(transport=httpx.MockTransport(fake))
    auth = LeanScopeAuth("https://supabase.example", "anon", tmp_path / "state.json", client=client)
    return auth, LeanScopeCatalog("https://api.example", auth, client=client), fake


def test_login_keeps_refresh_token_and_choice_but_never_the_password(tmp_path):
    auth, _, _ = make(tmp_path)
    auth.login("me@example.com", "pw")
    auth.set_selection(ChoreoSelection("145", "g2", "Narration"))
    assert auth.access_token() == "jwt-1"
    text = (tmp_path / "state.json").read_text()
    assert "refresh-1" in text and "pw" not in text and "jwt-1" not in text
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600
    # a restart: the stored login refreshes into a new JWT, the choice is still there
    again, _, _ = make(tmp_path, auth.client._transport.handler)  # type: ignore[attr-defined]
    assert again.logged_in and again.email == "me@example.com" and again.selection.scenario_guid == "g2"
    assert again.access_token() == "jwt-2"
    assert "refresh-2" in (tmp_path / "state.json").read_text()  # the rotated refresh token is saved


def test_wrong_password_raises_the_login_message(tmp_path):
    auth, _, _ = make(tmp_path)
    with pytest.raises(LeanScopeError, match="Invalid login credentials"):
        auth.login("me@example.com", "nope")
    assert not auth.logged_in


def test_expired_jwt_is_refreshed_and_a_revoked_login_logs_out(tmp_path):
    auth, _, fake = make(tmp_path)
    auth.login("me@example.com", "pw")
    auth._expires_at = time.time()  # expired
    assert auth.access_token() == "jwt-2"
    auth._expires_at = time.time()
    fake.refresh_ok = False
    with pytest.raises(LeanScopeError, match="log in again"):
        auth.access_token()
    assert (
        not auth.logged_in
        and "refresh" not in json.loads((tmp_path / "state.json").read_text())["refreshToken"]
    )


def test_another_user_starts_with_no_choice(tmp_path):
    auth, _, _ = make(tmp_path)
    auth.login("me@example.com", "pw")
    auth.set_selection(ChoreoSelection("145", "g2"))
    auth.login("other@example.com", "pw")
    assert not auth.selection.complete


def test_repositories_are_merged_and_choreos_filtered(tmp_path):
    auth, cat, fake = make(tmp_path)
    auth.login("me@example.com", "pw")
    repos = cat.repositories()
    assert [r["id"] for r in repos] == ["1", "145"] and repos[0]["account"]
    assert fake.requests[-1].headers["authorization"] == "Bearer jwt-1"
    fake.account_fails = True
    assert [r["id"] for r in cat.repositories()] == ["1", "145"]  # still listed by `repositories`
    assert cat.choreos("145") == [
        {"guid": "g4", "title": "Another choreo", "goal": "", "hints": []},
        {"guid": "g2", "title": "Narration", "goal": "Open the Wi-Fi page",
         "hints": ["Settings is in the dock", "Use the search"]},
    ]


def test_catalog_needs_a_login(tmp_path):
    _, cat, _ = make(tmp_path)
    with pytest.raises(LeanScopeError, match="not logged in"):
        cat.repositories()


def test_run_choreo_sends_the_fresh_jwt():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"session": {"sessionToken": "tok"}})

    c = AgentServerClient(
        "http://agent", lambda: "jwt-9", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    c.run_choreo("145", "g2")
    assert seen["auth"] == "Bearer jwt-9"


def test_dashboard_login_routes(tmp_path):
    s = Settings(cua_runs_dir=tmp_path, leanscope_agent_server_url="http://agent", cua_state_file=tmp_path / "s.json",
                 cua_vision_provider="none", _env_file=None)  # fmt: skip
    svc = CuaService(s, device=FakeKvm([make_screen(HOME)]), decider=StubDecider())
    auth, cat, _ = make(tmp_path)
    svc.leanscope, svc.catalog, svc.narration.auth = auth, cat, auth  # type: ignore[union-attr]

    def app():
        return BearerAuth(build_server(svc).streamable_http_app(host="127.0.0.1"), "secret")

    with TestClient(app(), client=("10.0.0.5", 1234)) as c:
        assert c.get("/dashboard/api/leanscope/repositories").status_code == 401  # remote without the token
    with TestClient(app(), client=("127.0.0.1", 1234)) as c:
        r = c.post("/dashboard/api/leanscope/login", json={"email": "me@example.com", "password": "nope"})
        assert r.status_code == 400 and "Invalid login" in r.json()["error"]
        assert c.post(
            "/dashboard/api/leanscope/login", json={"email": "me@example.com", "password": "pw"}
        ).json()["ok"]
        assert len(c.get("/dashboard/api/leanscope/repositories").json()["result"]) == 2
        assert len(c.get("/dashboard/api/leanscope/choreos?repositoryId=145").json()["result"]) == 2
        c.post("/dashboard/api/leanscope/selection", json={"repositoryId": "145", "scenarioGuid": "g2"})
        n = c.get("/dashboard/api/state").json()["narration"]
        assert n["loggedIn"] and n["selection"]["scenarioGuid"] == "g2"
        assert not n["narrate"]  # off by default
        c.post("/dashboard/api/leanscope/narrate", json={"on": True})
        assert c.get("/dashboard/api/state").json()["narration"]["narrate"]
        assert json.loads((tmp_path / "state.json").read_text())["narrate"] is True
        c.post("/dashboard/api/leanscope/logout")
        assert not c.get("/dashboard/api/state").json()["narration"]["loggedIn"]

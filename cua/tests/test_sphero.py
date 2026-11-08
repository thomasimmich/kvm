from __future__ import annotations

import json
import time

import httpx
import pytest

from kvm_cua.kvm.sphero import SpheroError, SpheroMirror, SpheroMouse, normalize_url


class FakeSphero:
    """An httpx transport that answers like sphero_mouse.server and records the calls."""

    def __init__(self, state: str = "pending", detail: str | None = None, down: bool = False):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.state, self.detail, self.down = state, detail, down

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=req)
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        if req.method == "POST":
            assert req.headers["content-type"] == "application/json"
        if req.url.path == "/target":
            return httpx.Response(200, json={"seq": len(self.calls), **(body or {}), "state": self.state,
                                             "detail": self.detail})  # fmt: skip
        return httpx.Response(200, json={"ok": True, "state": "cancelled"})

    def factory(self, url: str) -> SpheroMouse:
        return SpheroMouse(url, transport=httpx.MockTransport(self))

    def targets(self) -> list[dict]:
        return [b for m, p, b in self.calls if p == "/target"]


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_normalize_url():
    assert normalize_url("192.168.1.20") == "http://192.168.1.20:8765"
    assert normalize_url("http://192.168.1.20:9000/") == "http://192.168.1.20:9000"
    assert normalize_url("") == ""


def test_client_paths_and_clamping():
    fake = FakeSphero()
    m = fake.factory("10.0.0.2")
    assert m.goto(120, -5)["state"] == "pending"
    m.stop()
    m.health()
    assert fake.calls == [("POST", "/target", {"x": 100.0, "y": 0.0}), ("POST", "/stop", {}), ("GET", "/health", None)]


def test_enable_checks_health_and_persists(tmp_path):
    state = tmp_path / "sphero.json"
    down = FakeSphero(down=True)
    events: list[dict] = []
    mirror = SpheroMirror(events.append, state, client_factory=down.factory)
    with pytest.raises(SpheroError, match="not reachable"):
        mirror.enable("10.0.0.2")
    assert not mirror.enabled and events[-1]["state"] == "unreachable"

    fake = FakeSphero()
    mirror.client_factory = fake.factory
    mirror.enable("10.0.0.2")
    assert mirror.enabled and events[-1]["state"] == "connected"
    assert json.loads(state.read_text()) == {"enabled": True, "url": "http://10.0.0.2:8765"}

    mirror.disable()
    assert ("POST", "/stop", {}) in fake.calls
    assert json.loads(state.read_text())["enabled"] is False


def test_latest_target_wins_and_rate_limit(tmp_path):
    fake = FakeSphero()
    mirror = SpheroMirror(None, None, client_factory=fake.factory, max_rate_hz=5)
    mirror.enable("10.0.0.2")
    for i in range(50):  # a burst: most targets are replaced before they go out
        mirror.push(i, 50)
    assert wait_for(lambda: fake.targets() and fake.targets()[-1]["x"] == 49)
    assert len(fake.targets()) <= 3
    n = len(fake.targets())
    mirror.push(49.1, 50)  # below the minimum change: skipped
    time.sleep(0.3)
    assert len(fake.targets()) == n


def test_disabled_mirror_sends_nothing():
    fake = FakeSphero()
    mirror = SpheroMirror(None, None, client_factory=fake.factory)
    mirror.push(10, 10)
    time.sleep(0.05)
    assert fake.calls == []


def test_rejection_is_logged_once(tmp_path):
    fake = FakeSphero(state="rejected", detail="not calibrated")
    events: list[dict] = []
    mirror = SpheroMirror(events.append, None, client_factory=fake.factory, max_rate_hz=100)
    mirror.enable("10.0.0.2")
    for x in (10, 20, 30):
        mirror.push(x, 10)
        wait_for(lambda x=x: fake.targets() and fake.targets()[-1]["x"] == x)
    rejected = [e for e in events if e["state"] == "rejected"]
    assert len(rejected) == 1 and "not calibrated" in rejected[0]["text"]
    fake.state, fake.detail = "pending", None
    mirror.push(40, 10)
    assert wait_for(lambda: events[-1]["state"] == "ok")


def test_restores_enabled_state(tmp_path):
    state = tmp_path / "sphero.json"
    state.write_text(json.dumps({"enabled": True, "url": "http://10.0.0.2:8765"}))
    fake = FakeSphero()
    mirror = SpheroMirror(None, state, client_factory=fake.factory)
    assert mirror.enabled
    mirror.push(25, 75)
    assert wait_for(lambda: fake.targets() == [{"x": 25.0, "y": 75.0}])

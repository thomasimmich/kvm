"""Mirror the pointer to a physical Sphero force-feedback mouse.

The Sphero server (Johann's `sphero_mouse.server`) takes a target in percent of the screen:
`POST /target {"x": 0-100, "y": 0-100}`. A new target replaces the old one while the mouse is
still driving, so sending 5–10 targets per second is fine. The server has no authentication.

`SpheroMirror` sends from its own thread: the latest target wins and at most `max_rate_hz` go out,
so the KVM's own input never waits for the Sphero.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class SpheroError(RuntimeError):
    pass


class SpheroMouse:
    def __init__(self, base_url: str, timeout_s: float = 1.5, transport: httpx.BaseTransport | None = None):
        self.base_url = normalize_url(base_url)
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout_s, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            r = self._client.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise SpheroError(f"the Sphero server at {self.base_url} is not reachable ({e.__class__.__name__})") from e
        if r.status_code != 200:
            raise SpheroError(f"{method} {path} failed: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise SpheroError(f"{method} {path} did not answer with JSON") from e

    def health(self) -> dict:
        return self._call("GET", "/health")

    def status(self) -> dict:
        return self._call("GET", "/status")

    def goto(self, x_pct: float, y_pct: float) -> dict:
        x, y = (round(min(max(v, 0.0), 100.0), 2) for v in (x_pct, y_pct))
        return self._call("POST", "/target", {"x": x, "y": y})

    def stop(self) -> dict:
        return self._call("POST", "/stop", {})


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    if url.count(":") == 1:  # no port given: the server's default
        url += ":8765"
    return url


class SpheroMirror:
    """Sends pointer targets (percent) to the Sphero while enabled. Thread-safe."""

    def __init__(
        self,
        on_event: Callable[[dict], None] | None = None,
        state_file: Path | None = None,
        default_url: str = "",
        client_factory: Callable[[str], SpheroMouse] = SpheroMouse,
        max_rate_hz: float = 10.0,
        min_delta_pct: float = 0.3,
    ):
        self.on_event = on_event
        self.state_file = state_file
        self.client_factory = client_factory
        self.min_interval_s = 1.0 / max_rate_hz
        self.min_delta_pct = min_delta_pct
        self.enabled = False
        self.url = normalize_url(default_url)
        self.state: str | None = None  # last /target answer: pending, running, done, rejected, cancelled
        self.detail: str | None = None
        self.error = ""
        self.sent = 0
        self._client: SpheroMouse | None = None
        self._cond = threading.Condition()
        self._pending: tuple[float, float] | None = None
        self._last_sent: tuple[float, float] | None = None
        self._reported: tuple | None = None
        self._worker: threading.Thread | None = None
        self._load()
        if self.enabled and self.url:  # back on after a restart; errors show up with the first target
            self._client = self.client_factory(self.url)
            self._start_worker()
        else:
            self.enabled = False

    # --- control ------------------------------------------------------------------

    def enable(self, url: str) -> dict:
        """Checks /health first, so a wrong address fails here and not silently later."""
        url = normalize_url(url)
        if not url:
            raise SpheroError("enter the Sphero server's address, e.g. http://192.168.1.20:8765")
        client = self.client_factory(url)
        try:
            client.health()
        except SpheroError as e:
            client.close()
            self.error = str(e)
            self._report("unreachable", str(e))
            raise
        with self._cond:
            old, self._client = self._client, client
            self.url, self.enabled, self.error = url, True, ""
            self.state = self.detail = None
            self._last_sent = None
        if old is not None and old is not client:
            old.close()
        self._save()
        self._start_worker()
        self._report("connected", url)
        return self.snapshot()

    def disable(self) -> dict:
        with self._cond:
            client, self._client = self._client, None
            self.enabled = False
            self._pending = None
        if client is not None:
            try:
                client.stop()
            except SpheroError as e:
                log.info("sphero stop on disable failed: %s", e)
            client.close()
        self._save()
        self._report("off", None)
        return self.snapshot()

    def stop(self) -> dict:
        with self._cond:
            client = self._client
            self._pending = None
        if client is None:
            raise SpheroError("the Sphero is off")
        res = client.stop()
        self.state, self.detail = res.get("state") or "cancelled", res.get("detail")
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "url": self.url, "state": self.state, "detail": self.detail,
                "error": self.error, "sent": self.sent}  # fmt: skip

    # --- targets ------------------------------------------------------------------

    def push(self, x_pct: float, y_pct: float) -> None:
        if not self.enabled:
            return
        with self._cond:
            self._pending = (x_pct, y_pct)
            self._cond.notify()

    def _start_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="sphero-mirror", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None:
                    self._cond.wait()
                target, self._pending = self._pending, None
                client, last = self._client, self._last_sent
            if client is None:
                continue
            if last is not None and max(abs(target[0] - last[0]), abs(target[1] - last[1])) < self.min_delta_pct:
                continue
            t0 = time.monotonic()
            self._send(client, target)
            time.sleep(max(0.0, self.min_interval_s - (time.monotonic() - t0)))

    def _send(self, client: SpheroMouse, target: tuple[float, float]) -> None:
        try:
            res = client.goto(*target)
        except SpheroError as e:
            self.error = str(e)
            self._report("unreachable", str(e))
            return
        with self._cond:
            self._last_sent = target
        self.sent += 1
        self.error = ""
        self.state, self.detail = res.get("state"), res.get("detail")
        # pending/running/done are the normal flow; only a rejection (web app not ready) is worth a log line
        self._report("rejected" if self.state == "rejected" else "ok", self.detail if self.state == "rejected" else None)

    def _report(self, kind: str, detail: str | None) -> None:
        """One activity-log line per change, not per target. "ok" is only said after a problem."""
        key = (kind, detail)
        if key == self._reported:
            return
        prev, self._reported = self._reported, key
        if self.on_event is None or (kind == "ok" and (prev is None or prev[0] in ("ok", "connected"))):
            return
        text = {
            "connected": f"Sphero connected at {detail}",
            "off": "Sphero off",
            "ok": "Sphero follows the pointer again",
            "rejected": f"Sphero rejected the target: {detail or 'the web app is not ready'}",
            "unreachable": f"Sphero: {detail}",
        }[kind]
        self.on_event({"type": "sphero", "state": kind, "text": text, "ts": time.time()})

    # --- state file ---------------------------------------------------------------

    def _load(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            j = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable %s: %s", self.state_file, e)
            return
        self.url = normalize_url(j.get("url") or "") or self.url
        self.enabled = j.get("enabled") is True

    def _save(self) -> None:
        if self.state_file is None:
            return
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"enabled": self.enabled, "url": self.url}, f, indent=2)
        os.replace(tmp, self.state_file)

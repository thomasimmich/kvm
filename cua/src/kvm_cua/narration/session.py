"""One LeanScope session per run, and the worker endpoint the session calls back.

How a run is narrated:

1. `begin()` starts a session of the choreo picked in the dashboard (`LeanScopeAuth`, after the
   LeanScope login) through the run-choreo plugin route and opens its start gate. Without a login
   or a picked choreo the run goes ahead unnarrated (status `not_configured`).
2. The choreo's step with the 🦾 Worker fires: the engine creates the worker step and its
   activity and POSTs the worker body to `CUA_PUBLIC_URL/worker/dispatch`. `on_dispatch()`
   matches it to the waiting run by session token and answers 202 at once
   (the worker is asynchronous: waitForCompletion = false).
3. The run reports into that activity (`NarrationPublisher`). Its last write carries the
   terminal phase, which settles the worker step.
4. A worker on a need is dispatched with `context.input` on every answer the remote control
   submits: that is a command (guidance, answer, pause, resume, cancel) for the running loop.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from ..agent.inbox import Command, parse_command
from .agent_server import AgentServerClient, SessionHandle
from .leanscope_auth import LeanScopeAuth
from .publisher import NarrationPublisher

log = logging.getLogger(__name__)


@dataclass
class NarrationRun:
    goal: str
    handle: SessionHandle | None = None
    status: str = "starting"  # starting | waiting | running | settled | no_dispatch | failed | not_configured
    error: str = ""
    activity_url: str = ""
    agent_server_url: str = ""
    publisher: NarrationPublisher | None = None
    dispatched: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)

    @property
    def session_token(self) -> str:
        return self.handle.session_token if self.handle else ""


class NarrationSessions:
    def __init__(
        self,
        client: AgentServerClient,
        auth: LeanScopeAuth,
        remote_control_url: str = "",
        dispatch_timeout_s: float = 20.0,
    ):
        self.client = client
        self.auth = auth  # login and the choice of choreo (set in the dashboard)
        self.remote_control_url = remote_control_url
        self.dispatch_timeout_s = dispatch_timeout_s
        self.current: NarrationRun | None = None
        self.on_command: Callable[[Command], None] | None = None  # set by the service

    # --- run side ------------------------------------------------------------------

    def begin(self, goal: str, hints: list[str], input_names: list[str], max_steps: int) -> NarrationRun:
        """Start the session and wait for the worker dispatch. Never raises: without a dispatch the run
        goes ahead unnarrated, and the reason is on the returned run (and the dashboard)."""
        run = NarrationRun(goal)
        self.current = run
        sel = self.auth.selection
        if not self.auth.logged_in or not sel.complete:
            run.status = "not_configured"
            run.error = ("not logged in to LeanScope" if not self.auth.logged_in else "no choreo picked") + (
                ": the run goes ahead without a LeanScope session (log in and pick a choreo in the dashboard)"
            )
            log.info("narration: %s", run.error)
            return run
        try:
            # input values can be secrets (passwords to type): only their names leave the CUA
            run.handle = self.client.run_choreo(
                sel.repository_id,
                sel.scenario_guid,
                {"goal": goal, "hints": hints, "inputNames": input_names},
                sel.session_repository_id or None,
            )
            run.status = "waiting"
            self.client.start(run.session_token)
        except Exception as e:  # noqa: BLE001 — shown on the dashboard
            run.status, run.error = "failed", str(e)
            log.warning("narration session could not start: %s", e)
            return run
        if not run.dispatched.wait(self.dispatch_timeout_s):
            run.status = "no_dispatch"
            run.error = (
                f"the session's worker did not call /worker/dispatch within {self.dispatch_timeout_s:.0f} s: "
                "check the choreo's Worker dispatcher URL (CUA_PUBLIC_URL) and the agent server's callback origin"
            )
            log.warning("narration: %s", run.error)
            return run
        run.publisher = NarrationPublisher(self.client, run.session_token, run.activity_url, max_steps)
        run.status = "running"
        return run

    def end(self, run: NarrationRun) -> None:
        if run.publisher is not None:
            run.publisher.close()
            run.status = "settled"

    # --- worker endpoint side ------------------------------------------------------

    def on_dispatch(self, body: dict) -> tuple[int, dict]:
        token = body.get("sessionToken") or ""
        config = body.get("config") or {}
        ctx: dict = config.get("context") or {}
        endpoints: dict = config.get("endpoints") or {}
        activity_url = self._activity_url(endpoints, ctx)
        run = self.current
        is_current = run is not None and bool(token) and token == run.session_token

        if "input" in ctx:  # a need was answered: a command from the remote control
            cmd = parse_command(ctx.get("input"))
            if cmd is None:
                summary = "Nothing to pass on to the CUA."
            elif not is_current or run is None or run.status != "running" or self.on_command is None:
                summary = "No CUA run is active in this session."
            else:
                self.on_command(cmd)
                summary = f"Passed to the CUA: {cmd.kind}" + (f": {cmd.text}" if cmd.text else "")
            if activity_url:  # the closing line of this short need run
                try:
                    self.client.patch_activity(activity_url, {"phase": "succeeded", "summary": summary})
                except Exception as e:  # noqa: BLE001
                    log.debug("need activity patch failed: %s", e)
            return 200, {"ok": True, "summary": summary}

        if not is_current or run is None:
            return 409, {"error": "no CUA run is waiting for this session: start runs from the CUA dashboard "
                                  "or with cua_run_goal"}  # fmt: skip
        if run.dispatched.is_set():
            return 409, {"error": "this session's run was already dispatched"}
        if not activity_url:
            return 400, {"error": "the worker body carries no activity (config.context.activityGuid)"}
        run.activity_url = activity_url
        server = endpoints.get("agentServerUrl") or ""
        run.agent_server_url = server if server.startswith("http") else self.client.base_url
        run.dispatched.set()
        return 202, {"ok": True, "accepted": True}

    def _activity_url(self, endpoints: dict, ctx: dict) -> str:
        url = endpoints.get("activityApiUrl") or ""
        if url.startswith("http"):
            return url
        # a private callback origin is replaced by a hint text; we know the agent server ourselves
        guid = ctx.get("activityGuid")
        return self.client.absolute(f"/api/v1/activities/{guid}") if guid else ""

    # --- dashboard ------------------------------------------------------------------

    def remote_control_link(self, run: NarrationRun) -> str:
        if run.handle is None:
            return ""
        if not self.remote_control_url:
            return run.handle.ui_url
        server = (run.agent_server_url or self.client.base_url).rstrip("/")
        sep = "&" if "?" in self.remote_control_url else "?"
        return (
            self.remote_control_url
            + sep
            + urlencode({"agentServer": server, "sessionToken": run.session_token})
        )

    def state(self) -> dict[str, Any]:
        login = {
            "enabled": True,
            "loggedIn": self.auth.logged_in,
            "narrate": self.auth.narrate,
            "email": self.auth.email,
            "selection": self.auth.selection.to_json(),
        }
        run = self.current
        if run is None:
            return {**login, "status": "idle"}
        link = self.remote_control_link(run)
        return {
            **login,
            "status": run.status,
            "error": run.error,
            "goal": run.goal,
            "sessionToken": run.session_token,
            "uiUrl": run.handle.ui_url if run.handle else "",
            "remoteControlUrl": link,
            "qrSvg": qr_svg(link) if link else "",
            "failures": run.publisher.failures if run.publisher else 0,
        }


_qr_cache: dict[str, str] = {}


def qr_svg(text: str) -> str:
    if text not in _qr_cache:
        try:
            import segno

            if len(_qr_cache) >= 4:  # the remote control link and the dashboard's own LAN link
                _qr_cache.clear()
            _qr_cache[text] = segno.make(text, error="m").svg_inline(
                scale=4, border=2, dark="black", light="white"
            )
        except Exception as e:  # noqa: BLE001 — the link is shown as text as well
            log.debug("qr failed: %s", e)
            return ""
    return _qr_cache[text]

"""Streamable-HTTP MCP server — what the LeanScope agent server connects to.

All tool results are plain text: the agent server flattens MCP results to a
string and shows images only as "[image/png]". The CUA background knowledge is
served as the prompt `additional_agent_instructions`, which the agent server
appends to the step prompt automatically.

The same process also serves a small live dashboard under /dashboard, and
/worker/dispatch, which the agent server calls when the narration choreo's
🦾 Worker fires (see narration/session.py).
"""

from __future__ import annotations

import hmac
import io
import ipaddress
import logging
import threading
from pathlib import Path
from typing import Literal

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .agent.inbox import Command, parse_command
from .kvm.sphero import SpheroError
from .narration.leanscope_auth import ChoreoSelection, LeanScopeError
from .narration.session import qr_svg
from .service import Busy, CuaService, screen_text

log = logging.getLogger(__name__)

KNOWLEDGE_FILE = Path(__file__).resolve().parents[2] / "knowledge" / "additional_agent_instructions.md"
DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard" / "static" / "index.html"


def knowledge_text() -> str:
    try:
        return KNOWLEDGE_FILE.read_text("utf-8")
    except OSError:
        return "Use cua_run_goal to operate the remote computer."


def build_server(service: CuaService) -> MCPServer:
    mcp = MCPServer(
        name="leanscope-kvm-cua",
        title="KVM computer-using agent",
        instructions=(
            "Operates a real remote computer through a hardware KVM (screen parsing + mouse/keyboard). "
            "Start with cua_run_goal; use the single-step tools for corrections."
        ),
    )

    async def blocking(fn, *args):
        return await anyio.to_thread.run_sync(lambda: fn(*args))

    @mcp.tool(structured_output=False)
    async def cua_run_goal(
        goal: str,
        hints: list[str] | None = None,
        inputs: dict[str, str] | None = None,
        expect_text: str | None = None,
        max_steps: int = 15,
        time_budget_s: float = 100,
    ) -> str:
        """Reach a goal on the remote computer autonomously: look at the screen, pick the best element,
        click/scroll/type, check the effect, undo and retry when an attempt did not help.

        goal: concrete, observable outcome ("open the invoice INV-0815 in the ERP").
        hints: application knowledge (menu paths, what to avoid).
        inputs: named texts the agent may type into focused fields, e.g. {"search": "INV-0815"}.
        expect_text: text that is visible once the goal is reached (stops the run early).
        max_steps: upper bound of actions (default 15).
        time_budget_s: stop starting new steps after this many seconds (default 100, below the caller's
        120 s tool timeout); status time_budget means "call again with the same goal to continue".
        Returns a text summary with status done | stuck | max_steps | time_budget | error, every step,
        and the final screen.
        """
        res = await blocking(
            service.run_goal, goal, hints or [], inputs or {}, max_steps, expect_text, time_budget_s
        )
        return res.summary()

    @mcp.tool(structured_output=False)
    async def cua_observe(max_chars: int = 12000) -> str:
        """Look at the remote screen now. Returns every UI element as `[id] kind 'label' at position (x,y) WxH`.
        Ids are valid until the next observation."""
        return screen_text(await blocking(service.observe), max_chars)

    @mcp.tool(structured_output=False)
    async def cua_click_element(
        element_id: int, button: Literal["left", "right", "middle"] = "left", double: bool = False
    ) -> str:
        """Click an element (by id from the latest cua_observe) with a human-like mouse movement.
        Returns the screen after the click."""
        return screen_text(await blocking(service.click_element, element_id, button, double))

    @mcp.tool(structured_output=False)
    async def cua_click_text(text: str, double: bool = False) -> str:
        """Observe, then click the element whose label contains `text` (case-insensitive). Returns the new screen."""
        screen, msg = await blocking(service.click_text, text, double)
        return msg + "\n" + screen_text(screen)

    @mcp.tool(structured_output=False)
    async def cua_scroll(direction: Literal["up", "down", "left", "right"], notches: int = 5) -> str:
        """Scroll with the mouse wheel at the current pointer position (vertical or horizontal). Returns the new screen."""
        return screen_text(await blocking(service.scroll, direction, notches))

    @mcp.tool(structured_output=False)
    async def cua_type_text(text: str) -> str:
        """Type text into the focused field on the remote computer (keyboard layout from KVM_KEYBOARD_LAYOUT)."""
        return screen_text(await blocking(service.type_text, text))

    @mcp.tool(structured_output=False)
    async def cua_press_keys(combo: str) -> str:
        """Press a key or combination, e.g. 'enter', 'esc', 'tab', 'ctrl+l', 'alt+tab', 'cmd+space'. Returns the new screen."""
        return screen_text(await blocking(service.press, combo))

    @mcp.prompt(
        name="additional_agent_instructions",
        description="How to operate the remote computer through the KVM computer-using agent",
    )
    def additional_agent_instructions() -> str:
        return knowledge_text()

    # --- dashboard -------------------------------------------------------------

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def dashboard(_: Request) -> Response:
        return HTMLResponse(DASHBOARD_HTML.read_text("utf-8"))

    @mcp.custom_route("/dashboard/api/state", methods=["GET"])
    async def dashboard_state(_: Request) -> Response:
        live = service.live
        scr = service._agent.last_screen if service._agent else None
        return JSONResponse({
            "goal": live.goal,
            "running": live.running,
            "highlight": live.highlight,
            "events": list(live.events),
            "lastResult": live.last_result,
            "screen": None if scr is None else {
                "seq": scr.seq, "size": scr.size, "parseMs": scr.parse_ms,
                "elements": [e.to_json() for e in scr.elements], "newIds": scr.new_element_ids,
            },
            "decider": service.settings.typesafe_api_key and "jev" or "stub",
            "describer": "vision" if service._agent and service._agent.describer else "off",
            "narration": narration_state(),
            "innerVoice": service.inner_voice.state(),
            "pointer": service.pointer_state(),
            "sphero": service.sphero.snapshot(),
        })  # fmt: skip

    @mcp.custom_route("/dashboard/api/network", methods=["GET"])
    async def dashboard_network(_: Request) -> Response:
        """The dashboard's address on the local network (with the token) and its QR code, for the
        header LED. Only reached by the local browser or a client that already has the token."""
        s = service.settings
        if _is_loopback(s.cua_mcp_host):
            return JSONResponse({"lan": False, "port": s.cua_mcp_port})
        ip = s.cua_mcp_host if s.cua_mcp_host not in ("0.0.0.0", "::") else _lan_ip()
        if not ip:
            return JSONResponse({"lan": False, "port": s.cua_mcp_port, "error": "no network address found"})
        token = f"?token={s.cua_mcp_token}" if s.cua_mcp_token else ""
        url = f"http://{ip}:{s.cua_mcp_port}/dashboard{token}"
        return JSONResponse({"lan": True, "url": url, "qrSvg": qr_svg(url)})

    @mcp.custom_route("/dashboard/api/inner-voice/{clip}.mp3", methods=["GET"])
    async def inner_voice_clip(request: Request) -> Response:
        audio = service.inner_voice.clip(request.path_params["clip"])
        if audio is None:
            return PlainTextResponse("no such clip", status_code=404)
        return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})

    @mcp.custom_route("/dashboard/api/inner-voice/persona", methods=["GET"])
    async def inner_voice_persona(_: Request) -> Response:
        """The picked choreo's acting persona: name, image, and the voice the inner voice speaks with."""
        return JSONResponse({"ok": True, "result": await blocking(service.inner_voice.lookup)})

    @mcp.custom_route("/dashboard/api/inner-voice/test", methods=["POST"])
    async def inner_voice_test(_: Request) -> Response:
        """Speaks a short greeting with the persona's voice; the dashboard plays the clip."""
        try:
            return JSONResponse({"ok": True, "result": await blocking(service.inner_voice.test)})
        except Exception as e:  # noqa: BLE001 — shown on the dashboard
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    @mcp.custom_route("/dashboard/api/inner-voice", methods=["POST"])
    async def inner_voice_switch(request: Request) -> Response:
        """{on}: whether the acting persona thinks the activity log aloud."""
        b = await request.json()
        service.inner_voice.enabled = b.get("on") is True
        return JSONResponse({"ok": True, "result": service.inner_voice.state()})

    def narration_state() -> dict:
        if service.narration is None:
            return {"enabled": False}
        return {**service.narration.state(), "workerDispatchUrl": service.settings.worker_dispatch_url}

    @mcp.custom_route("/dashboard/api/screen.jpg", methods=["GET"])
    async def dashboard_screen(_: Request) -> Response:
        data = service.annotated_jpeg()
        if data is None:
            return PlainTextResponse("no screen yet", status_code=404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/dashboard/api/live.jpg", methods=["GET"])
    async def dashboard_live(_: Request) -> Response:
        """The current screen without parsing (about 0.3 s), for the dashboard's live view."""

        def grab() -> bytes:
            buf = io.BytesIO()
            service.device.screenshot().image.save(buf, "JPEG", quality=80)
            return buf.getvalue()

        try:
            data = await blocking(grab)
        except Exception as e:  # noqa: BLE001 — shown in the dashboard
            return PlainTextResponse(str(e), status_code=503)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # --- manual pointer in the live view, and the physical Sphero mouse --------------

    async def manual(fn, *args) -> Response:
        try:
            await blocking(fn, *args)
        except Busy as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        except Exception as e:  # noqa: BLE001 — shown on the dashboard
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
        return JSONResponse({"ok": True, "result": service.pointer_state()})

    def num(b: dict, key: str) -> float:
        v = b.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"{key} must be a number")  # noqa: TRY004 — a 400 for the dashboard
        return float(v)

    @mcp.custom_route("/dashboard/api/pointer", methods=["POST"])
    async def dashboard_pointer(request: Request) -> Response:
        """{x, y, buttons}: x, y in 0..1 of the screen; buttons 1 left, 2 right, 4 middle (0 = none)."""
        b = await request.json()
        try:
            x, y, buttons = num(b, "x"), num(b, "y"), int(b.get("buttons") or 0)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return await manual(service.pointer_at, x, y, buttons)

    @mcp.custom_route("/dashboard/api/wheel", methods=["POST"])
    async def dashboard_wheel(request: Request) -> Response:
        """{x, y, dy, dx}: wheel notches at a point (dy > 0 scrolls up)."""
        b = await request.json()
        try:
            x, y = num(b, "x"), num(b, "y")
            dy, dx = int(b.get("dy") or 0), int(b.get("dx") or 0)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return await manual(service.wheel_at, x, y, dy, dx)

    @mcp.custom_route("/dashboard/api/sphero", methods=["GET", "POST"])
    async def dashboard_sphero(request: Request) -> Response:
        """GET: state. POST {enabled, url}: turn the mirror on (checks /health) or off."""
        if request.method == "GET":
            return JSONResponse({"ok": True, "result": service.sphero.snapshot()})
        b = await request.json()
        try:
            if b.get("enabled") is True:
                res = await blocking(service.sphero.enable, str(b.get("url") or ""))
                pos = service.pointer_state()
                if pos is not None:  # drive to where the pointer is now
                    service.sphero.push(pos["x"], pos["y"])
            else:
                res = await blocking(service.sphero.disable)
        except SpheroError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
        return JSONResponse({"ok": True, "result": res})

    @mcp.custom_route("/dashboard/api/sphero/stop", methods=["POST"])
    async def dashboard_sphero_stop(_: Request) -> Response:
        try:
            return JSONResponse({"ok": True, "result": await blocking(service.sphero.stop)})
        except SpheroError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    @mcp.custom_route("/dashboard/api/observe", methods=["POST"])
    async def dashboard_observe(_: Request) -> Response:
        try:
            scr = await blocking(service.observe)
            return JSONResponse({"ok": True, "elements": len(scr.elements)})
        except Exception as e:  # noqa: BLE001 — shown to the dashboard user as-is
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @mcp.custom_route("/dashboard/api/run", methods=["POST"])
    async def dashboard_run(request: Request) -> Response:
        body = await request.json()
        goal = (body.get("goal") or "").strip()
        if not goal:
            return JSONResponse({"ok": False, "error": "goal is required"}, status_code=400)
        if service.live.running:
            return JSONResponse({"ok": False, "error": "a run is in progress"}, status_code=409)

        # fire and forget: the dashboard follows progress by polling /dashboard/api/state
        threading.Thread(
            target=service.run_goal,
            args=(goal, body.get("hints") or [], body.get("inputs") or {}, body.get("maxSteps"),
                  body.get("expectText") or None),
            kwargs={"source": "dashboard"},
            daemon=True,
        ).start()  # fmt: skip
        return JSONResponse({"ok": True})

    @mcp.custom_route("/dashboard/api/cancel", methods=["POST"])
    async def dashboard_cancel(_: Request) -> Response:
        if not service.command(Command("cancel")) and service._agent:
            service._agent.cancel()
        return JSONResponse({"ok": True})

    @mcp.custom_route("/dashboard/api/reset", methods=["POST"])
    async def dashboard_reset(_: Request) -> Response:
        """Starts the dashboard from scratch: empties the activity log and the last result."""
        if service.live.running:
            return JSONResponse({"ok": False, "error": "a run is in progress"}, status_code=409)
        service.reset_live()
        return JSONResponse({"ok": True})

    async def steer(cmd: Command | None) -> Response:
        if cmd is None:
            return JSONResponse({"ok": False, "error": "say what the agent should do"}, status_code=400)
        if not service.command(cmd):
            return JSONResponse({"ok": False, "error": "no run in progress"}, status_code=409)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/dashboard/api/guidance", methods=["POST"])
    async def dashboard_guidance(request: Request) -> Response:
        """{text} or {command, text}: tells the running loop where to go (keywords like 'pause' work too)."""
        return await steer(parse_command(await request.json()))

    @mcp.custom_route("/dashboard/api/pause", methods=["POST"])
    async def dashboard_pause(_: Request) -> Response:
        return await steer(Command("pause"))

    @mcp.custom_route("/dashboard/api/resume", methods=["POST"])
    async def dashboard_resume(_: Request) -> Response:
        return await steer(Command("resume"))

    # --- the narration choreo's worker -------------------------------------------

    @mcp.custom_route("/worker/dispatch", methods=["POST"])
    async def worker_dispatch(request: Request) -> Response:
        """Called by the agent server: once when the run's worker step starts (answer 202, the run
        reports into the activity), and on every answer to the guidance need (a command)."""
        key = service.settings.cua_worker_key
        if key:
            if not hmac.compare_digest(request.query_params.get("key", "").encode(), key.encode()):
                return JSONResponse({"error": "wrong or missing ?key="}, status_code=401)
        elif not _is_loopback(request.client.host if request.client else ""):
            return JSONResponse({"error": "set CUA_WORKER_KEY to accept remote dispatches"}, status_code=401)
        if service.narration is None:
            return JSONResponse({"error": "narration is off: set LEANSCOPE_AGENT_SERVER_URL"}, status_code=503)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "expected a JSON worker body"}, status_code=400)
        status, payload = await blocking(service.narration.on_dispatch, body)
        return JSONResponse(payload, status_code=status)

    # --- LeanScope login and the choice of choreo ---------------------------------

    def leanscope_off() -> Response | None:
        if service.leanscope is None or service.catalog is None:
            return JSONResponse(
                {"ok": False, "error": "narration is off: set LEANSCOPE_AGENT_SERVER_URL"}, status_code=503
            )
        return None

    async def leanscope_call(fn, *args) -> Response:
        try:
            return JSONResponse({"ok": True, "result": await blocking(fn, *args)})
        except LeanScopeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @mcp.custom_route("/dashboard/api/leanscope/login", methods=["POST"])
    async def leanscope_login(request: Request) -> Response:
        if (off := leanscope_off()) is not None:
            return off
        body = await request.json()
        return await leanscope_call(service.leanscope.login, body.get("email") or "", body.get("password") or "")  # type: ignore[union-attr]

    @mcp.custom_route("/dashboard/api/leanscope/logout", methods=["POST"])
    async def leanscope_logout(_: Request) -> Response:
        if (off := leanscope_off()) is not None:
            return off
        return await leanscope_call(service.leanscope.logout)  # type: ignore[union-attr]

    @mcp.custom_route("/dashboard/api/leanscope/repositories", methods=["GET"])
    async def leanscope_repositories(_: Request) -> Response:
        if (off := leanscope_off()) is not None:
            return off
        return await leanscope_call(service.catalog.repositories)  # type: ignore[union-attr]

    @mcp.custom_route("/dashboard/api/leanscope/choreos", methods=["GET"])
    async def leanscope_choreos(request: Request) -> Response:
        """The repository's choreos with the goal and hints they prefill in the dashboard."""
        if (off := leanscope_off()) is not None:
            return off
        return await leanscope_call(service.catalog.choreos, request.query_params.get("repositoryId", ""))  # type: ignore[union-attr]

    @mcp.custom_route("/dashboard/api/leanscope/narrate", methods=["POST"])
    async def leanscope_narrate(request: Request) -> Response:
        """{on}: whether runs start a LeanScope session of the picked choreo (off by default)."""
        if (off := leanscope_off()) is not None:
            return off
        b = await request.json()
        return await leanscope_call(service.leanscope.set_narrate, b.get("on") is True)  # type: ignore[union-attr]

    @mcp.custom_route("/dashboard/api/leanscope/selection", methods=["POST"])
    async def leanscope_selection(request: Request) -> Response:
        """{repositoryId, scenarioGuid, scenarioTitle?, sessionRepositoryId?}: the choreo that narrates runs."""
        if (off := leanscope_off()) is not None:
            return off
        b = await request.json()
        sel = ChoreoSelection(
            repository_id=str(b.get("repositoryId") or ""),
            scenario_guid=str(b.get("scenarioGuid") or ""),
            scenario_title=str(b.get("scenarioTitle") or ""),
            session_repository_id=str(b.get("sessionRepositoryId") or ""),
        )
        return await leanscope_call(service.leanscope.set_selection, sel)  # type: ignore[union-attr]

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")

    return mcp


class BearerAuth:
    """Requires `Authorization: Bearer <token>` on every route.

    /healthz is open. The dashboard is also open to loopback clients, so the local
    browser needs no token; remote clients pass the token as ?token=… once, which
    sets a cookie.
    """

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.token:
            return await self.app(scope, receive, send)
        req = Request(scope)
        path = req.url.path
        if path == "/healthz" or path.startswith("/worker/"):  # /worker/* checks CUA_WORKER_KEY itself
            return await self.app(scope, receive, send)
        given = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if path.startswith("/dashboard"):
            if _is_loopback(req.client.host if req.client else ""):
                return await self.app(scope, receive, send)
            given = given or req.query_params.get("token", "") or req.cookies.get("cua_token", "")
        if given and hmac.compare_digest(given.encode(), self.token):
            if path.startswith("/dashboard") and req.query_params.get("token"):

                async def send_with_cookie(msg):
                    if msg["type"] == "http.response.start":
                        msg.setdefault("headers", []).append(
                            (
                                b"set-cookie",
                                b"cua_token=" + self.token + b"; HttpOnly; SameSite=Strict; Path=/dashboard",
                            )
                        )
                    await send(msg)

                return await self.app(scope, receive, send_with_cookie)
            return await self.app(scope, receive, send)
        resp = JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
        await resp(scope, receive, send)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


def _lan_ip() -> str:
    """This machine's address on the local network: the source address of a route to the
    internet (connecting a UDP socket sends nothing)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return ""


def build_app(service: CuaService):
    s = service.settings
    mcp = build_server(service)
    app = mcp.streamable_http_app(host=s.cua_mcp_host)
    if not s.cua_mcp_token and not _is_loopback(s.cua_mcp_host):
        raise SystemExit("refusing to serve on a non-loopback host without CUA_MCP_TOKEN")
    return BearerAuth(app, s.cua_mcp_token)


def serve(service: CuaService) -> None:
    import uvicorn

    s = service.settings
    log.info("MCP endpoint: http://%s:%d/mcp   dashboard: http://%s:%d/dashboard",
             s.cua_mcp_host, s.cua_mcp_port, s.cua_mcp_host, s.cua_mcp_port)  # fmt: skip
    uvicorn.run(build_app(service), host=s.cua_mcp_host, port=s.cua_mcp_port, log_level="info")

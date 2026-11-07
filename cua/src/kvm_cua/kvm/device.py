"""Access to the JetKVM: screen frames in, HID events out.

`KvmDevice` is the seam between the agent and the hardware. `HttpKvm` talks to
the firmware's /api/cua endpoints; `FakeKvm` replays saved screenshots and
records every event so the whole agent loop can be tested without a device.
"""

from __future__ import annotations

import io
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
from PIL import Image

from .decode import decode_last_frame

ABS_MAX = 32767


@dataclass
class MouseEvent:
    x: int  # 0..ABS_MAX
    y: int  # 0..ABS_MAX
    buttons: int = 0
    delay_ms: int = 0

    def to_json(self) -> dict:
        return {"x": self.x, "y": self.y, "buttons": self.buttons, "delayMs": self.delay_ms}


@dataclass
class WheelEvent:
    dy: int = 0  # positive = up
    dx: int = 0  # positive = right
    delay_ms: int = 0

    def to_json(self) -> dict:
        return {"dy": self.dy, "dx": self.dx, "delayMs": self.delay_ms}


@dataclass
class KeyEvent:
    modifier: int = 0
    keys: list[int] = field(default_factory=list)
    delay_ms: int = 0

    def to_json(self) -> dict:
        return {"modifier": self.modifier, "keys": self.keys, "delayMs": self.delay_ms}


@dataclass
class Frame:
    image: Image.Image
    seq: int  # device frame counter; pass to screenshot(after_seq=) to wait for a newer picture
    captured_at: float


class KvmDevice(Protocol):
    def screenshot(self, after_seq: int = 0) -> Frame: ...
    def screen_size(self) -> tuple[int, int]: ...
    def mouse(self, events: Sequence[MouseEvent]) -> None: ...
    def wheel(self, events: Sequence[WheelEvent]) -> None: ...
    def keys(self, events: Sequence[KeyEvent]) -> None: ...


class KvmError(RuntimeError):
    pass


# Requests carry at most this many events; the firmware limit is 1000.
_CHUNK = 500


class HttpKvm:
    def __init__(self, base_url: str, token: str, timeout_s: float = 10.0):
        if not token:
            raise KvmError("KVM_TOKEN is empty — enable the CUA API on the device first (see cua/README.md)")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/cua",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
        )
        self._size: tuple[int, int] | None = None

    def close(self) -> None:
        self._client.close()

    def state(self) -> dict:
        return self._json(self._client.get("/state"))

    def screen_size(self) -> tuple[int, int]:
        if self._size is None:
            v = self.state()["video"]
            if not v.get("width") or not v.get("height"):
                raise KvmError(f"no video signal: {v}")
            self._size = (int(v["width"]), int(v["height"]))
        return self._size

    def screenshot(self, after_seq: int = 0) -> Frame:
        params = {"after": str(after_seq)} if after_seq else None
        r = self._client.get("/frame", params=params)
        if r.status_code != 200:
            raise KvmError(f"/frame failed: HTTP {r.status_code} {r.text[:200]}")
        _check_not_web_ui(r)
        codec = r.headers.get("X-Codec", "h264")
        image = decode_last_frame(r.content, codec)
        w, h = int(r.headers.get("X-Width") or 0), int(r.headers.get("X-Height") or 0)
        if w and h:
            self._size = (w, h)
        else:
            self._size = image.size
        return Frame(image=image, seq=int(r.headers.get("X-Frame-Seq", "0")), captured_at=time.time())

    def mouse(self, events: Sequence[MouseEvent]) -> None:
        self._post_chunks("/mouse", [e.to_json() for e in events])

    def wheel(self, events: Sequence[WheelEvent]) -> None:
        self._post_chunks("/wheel", [e.to_json() for e in events])

    def keys(self, events: Sequence[KeyEvent]) -> None:
        # Not chunked: the device releases all keys at the end of each request,
        # so splitting could release a held modifier mid-sequence.
        if len(events) > 1000:
            raise KvmError("too many key events in one request (max 1000)")
        self._json(self._client.post("/keys", json={"events": [e.to_json() for e in events]}))

    def _post_chunks(self, path: str, events: list[dict]) -> None:
        for i in range(0, len(events), _CHUNK):
            chunk = events[i : i + _CHUNK]
            total_ms = sum(e.get("delayMs", 0) for e in chunk)
            self._json(self._client.post(path, json={"events": chunk}, timeout=10 + total_ms / 1000))

    @staticmethod
    def _json(r: httpx.Response) -> dict:
        if r.status_code != 200:
            raise KvmError(
                f"{r.request.method} {r.request.url.path} failed: HTTP {r.status_code} {r.text[:200]}"
            )
        _check_not_web_ui(r)
        return r.json()


def _check_not_web_ui(r: httpx.Response) -> None:
    # Firmware without the CUA API serves the web UI's index.html for unknown paths.
    if r.headers.get("content-type", "").startswith("text/html"):
        raise KvmError(
            "the device answered with its web page instead of the CUA API: it runs firmware "
            "without /api/cua (is ./dev_deploy.sh still running?)"
        )


class FakeKvm:
    """Replays a list of screenshots and records events.

    `screens` is consumed one per `screenshot()` call after an action; with
    `advance_on_action=True` the next screen is shown only once an input event
    arrived, which mimics "the click changed the screen".
    """

    def __init__(self, screens: Sequence[Image.Image | Path | str], advance_on_action: bool = True):
        self._screens = [s if isinstance(s, Image.Image) else Image.open(s).convert("RGB") for s in screens]
        if not self._screens:
            raise ValueError("FakeKvm needs at least one screen")
        self._index = 0
        self._seq = 1
        self._advance_on_action = advance_on_action
        self._pending_advance = False
        self.events: list[tuple[str, object]] = []

    @property
    def index(self) -> int:
        return self._index

    def screen_size(self) -> tuple[int, int]:
        return self._screens[0].size

    def screenshot(self, after_seq: int = 0) -> Frame:
        if self._pending_advance and self._index < len(self._screens) - 1:
            self._index += 1
        self._pending_advance = False
        self._seq += 1
        return Frame(image=self._screens[self._index].copy(), seq=self._seq, captured_at=time.time())

    def _acted(self) -> None:
        if self._advance_on_action:
            self._pending_advance = True

    def mouse(self, events: Sequence[MouseEvent]) -> None:
        self.events.extend(("mouse", e) for e in events)
        if any(e.buttons for e in events):
            self._acted()

    def wheel(self, events: Sequence[WheelEvent]) -> None:
        self.events.extend(("wheel", e) for e in events)
        self._acted()

    def keys(self, events: Sequence[KeyEvent]) -> None:
        self.events.extend(("keys", e) for e in events)
        self._acted()


def image_to_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()

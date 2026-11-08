"""Turns the loop's events into the activity of the CUA's worker step in a LeanScope session.

The agent server shows that activity live in the session (`/events` SSE, the
step's `meanTime`), so the remote control can follow the run. Every entry is one
narration line in markdown, prefixed with a tag the remote control can sort by:

    [screen:N]    the raw screenshot (image)
    [desc:N]      the rough screen description from the vision model
    [elements:N]  the annotated screenshot and the dense element list
    [decision:S]  the next action, its confidence and the verdict on the last one
    [activity]    micro steps ("asking Jev", "pressed Esc to undo")
    [human]       guidance, pause, resume or cancel that arrived
    [ask]         the CUA is stuck and waits for an answer
    [result]      the run's summary

N counts screens, S counts steps. Writes are sent one at a time, in order, from a
worker thread: a slow or failing agent server never blocks or fails a run.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
from typing import Any

from PIL import Image

from ..perception.annotate import annotate
from ..perception.types import Screen
from .agent_server import AgentServerClient

log = logging.getLogger(__name__)

MAX_ELEMENTS_CHARS = 4000
BUSY_QUEUE = 100  # drop micro activity lines when this far behind


def jpeg(image: Image.Image, max_width: int = 1280, quality: int = 80) -> bytes:
    img = image.convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class NarrationPublisher:
    def __init__(
        self,
        client: AgentServerClient,
        session_token: str,
        activity_url: str,
        max_steps: int = 15,
        decision_images: bool = True,
    ):
        self.client = client
        self.session_token = session_token
        self.activity_url = activity_url
        self.max_steps = max(1, max_steps)
        self.decision_images = decision_images
        self.failures = 0
        self._screens: dict[int, int] = {}  # frame seq → screen number
        self._step = 0
        self._q: queue.Queue[dict | None] = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._work, name="narration", daemon=True)
        self._thread.start()

    # --- the event sink (loop thread) -----------------------------------------

    def __call__(self, event: dict) -> None:
        if self._closed:
            return
        if event.get("type") == "activity" and self._q.qsize() > BUSY_QUEUE:
            return
        self._q.put(event)

    def close(self, timeout_s: float = 20.0) -> None:
        """Send what is queued, then stop."""
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        self._thread.join(timeout_s)

    # --- worker thread ----------------------------------------------------------

    def _work(self) -> None:
        while (ev := self._q.get()) is not None:
            try:
                self._handle(ev)
            except Exception as e:  # noqa: BLE001 — narration must never break a run
                self.failures += 1
                log.warning("narration %s failed: %s", ev.get("type"), e)

    def _patch(self, text: str | None = None, **fields: Any) -> None:
        body = dict(fields)
        if text:
            body["append"] = {"text": text, "sourceId": "kvm-cua"}
        self.client.patch_activity(self.activity_url, body)

    def _upload(self, image: Image.Image, name: str) -> str | None:
        try:
            return self.client.upload_file(self.session_token, jpeg(image), name, "image/jpeg")
        except Exception as e:  # noqa: BLE001 — the text still goes out without the image
            self.failures += 1
            log.warning("narration upload %s failed: %s", name, e)
            return None

    def _screen_no(self, seq: int) -> int:
        if seq not in self._screens:
            self._screens[seq] = len(self._screens) + 1
        return self._screens[seq]

    def _progress(self) -> float:
        return round(min(1.0, self._step / self.max_steps), 3)

    def _handle(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "run_start":
            self._patch(f"Starting: **{ev.get('goal', '')}**", summary=ev.get("goal", ""), phase="working",
                        progress=0)  # fmt: skip
        elif t == "activity":
            self._patch(f"[activity] {ev.get('text', '')}")
        elif t == "screen_raw":
            n = self._screen_no(ev["seq"])
            url = self._upload(ev["_image"], f"screen-{n:03d}.jpg")
            self._patch(f"[screen:{n}] Screen {n}" + (f"\n\n![Screen {n}]({url})" if url else ""))
        elif t == "screen_description":
            n = self._screen_no(ev["seq"])
            self._patch(f"[desc:{n}] {ev.get('text', '')}")
        elif t == "screen":
            self._screen_entry(ev)
        elif t == "decision":
            self._decision_entry(ev)
        elif t in ("guidance_received", "paused", "resumed", "cancel_requested"):
            self._human_entry(ev)
        elif t == "ask_human":
            self._patch(f"[ask] {ev.get('question', '')} (waiting {ev.get('wait_s', 0)} s for an answer)",
                        phase="pending")  # fmt: skip
        elif t == "ask_human_end":
            quiet = ev.get("answered") or ev.get("cancelled")
            self._patch(None if quiet else "[activity] No answer: going on alone", phase="working")
        elif t == "run_end":
            self._result_entry(ev)

    def _screen_entry(self, ev: dict) -> None:
        screen: Screen = ev["_screen"]
        n = self._screen_no(screen.seq)
        url = self._upload(annotate(screen.image, screen.elements, None, screen.new_element_ids),
                           f"screen-{n:03d}-elements.jpg")  # fmt: skip
        head = f"[elements:{n}] {len(screen.elements)} UI elements"
        if screen.new_element_ids:
            head += f" ({len(screen.new_element_ids)} new)"
        head += f", parsed in {screen.parse_ms / 1000:.1f} s"
        img = f"\n\n![Screen {n} with numbered elements]({url})" if url else ""
        self._patch(f"{head}{img}\n\n```\n{screen.describe(MAX_ELEMENTS_CHARS)}\n```")

    def _decision_entry(self, ev: dict) -> None:
        self._step = int(ev.get("step") or self._step + 1)
        conf = ev.get("confidence")
        line = f"[decision:{self._step}] Next: **{ev.get('action', '')}**"
        if conf is not None:
            line += f" ({conf * 100:.0f} % sure)"
        alts = [
            f"{k} {p * 100:.0f} %" for k, p in (ev.get("alternatives") or {}).items() if k != ev.get("option")
        ]
        if alts:
            line += ". Alternatives: " + ", ".join(alts[:3])
        prog = ev.get("progress")
        if prog is not None:
            line += f". The previous action {'helped' if prog >= 0.5 else 'did not help'} ({prog:.2f})"
        screen: Screen | None = ev.get("_screen")
        if self.decision_images and screen is not None and ev.get("element_id") is not None:
            n = self._screen_no(screen.seq)
            url = self._upload(annotate(screen.image, screen.elements, ev["element_id"], screen.new_element_ids),
                               f"step-{self._step:03d}-choice.jpg")  # fmt: skip
            if url:
                line += f"\n\n![Step {self._step}: the chosen element on screen {n}]({url})"
        summary = f"Step {self._step}/{self.max_steps}: {ev.get('action', '')}"
        self._patch(line, summary=summary, progress=self._progress(), phase="working")

    def _human_entry(self, ev: dict) -> None:
        t, text = ev["type"], ev.get("text", "")
        if t == "paused":
            self._patch("[human] Paused", phase="pausing")
        elif t == "resumed":
            self._patch("[human] Resumed", phase="working")
        elif t == "cancel_requested":
            self._patch("[human] Cancel requested")
        else:
            self._patch(f"[human] {'Answer' if ev.get('kind') == 'answer' else 'Guidance'}: {text}",
                        phase="working")  # fmt: skip

    def _result_entry(self, ev: dict) -> None:
        result = ev.get("_result")
        status, message = ev.get("status", ""), ev.get("message", "")
        summary = f"{status}: {message} ({ev.get('steps', 0)} steps, {ev.get('seconds', 0):.0f} s)"
        text = "[result] " + (result.summary() if result is not None else summary)
        phase = "succeeded" if status == "done" else "failed"
        self._patch(text, summary=summary, phase=phase, progress=1 if status == "done" else self._progress())

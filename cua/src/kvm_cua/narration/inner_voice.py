"""The acting persona's inner voice: the activity log, retold as their thoughts and spoken aloud.

The CUA's log lines are technical ("asking Jev", "click 'Settings' (82 % sure)"). An LLM retells
them as one or two short German thoughts of the persona acting in the picked choreo, in their
style (see persona_voice.py), and ElevenLabs speaks them with the persona's voice. The thoughts
go into the dashboard's log as `inner_voice` events; the dashboard plays the clips in order.

Pacing: speech is slower than the log. While a thought is being made, or its clip is still
playing (estimated from its length), new lines pile up, and the next thought covers all of them.
So the voice stays close to what happens instead of falling behind.

Like the session narration, it works on its own thread and never breaks or slows a run.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable

import httpx

from .persona_voice import PersonaVoice

log = logging.getLogger(__name__)

MAX_LINES = 12  # per thought: the newest pending lines
KEEP_CLIPS = 50
CHARS_PER_S = 14.0  # rough speaking rate, to wait for a clip to end before the next thought
EVENTS = {"run_start", "run_end", "activity", "decision", "screen_description", "guidance_received", "paused",
          "resumed", "cancel_requested", "ask_human"}  # fmt: skip

SYSTEM_PROMPT = """You are the inner voice of {name}, a real person in a usability study. A computer-using \
agent operates a computer on {name}'s behalf, and you turn its technical log into what {name} thinks \
in that moment, as if {name} were doing it themselves.

Who {name} is:
{profile}
{voice}
Rules:
- German, first person, spoken language: one or two short sentences, at most 30 words.
- Sound like {name}: their mood, motives and frustrations, not like a robot or a narrator.
- Never mention the agent, Jev, element numbers, percentages, coordinates, parsing or screenshots.
- Do not repeat your earlier thoughts. If nothing new happened, answer with an empty text."""


def event_line(ev: dict) -> str:
    """One log line as plain text for the LLM; empty for events the inner voice skips."""
    t = ev.get("type")
    if t not in EVENTS or ev.get("_inner"):
        return ""
    if t == "run_start":
        return f"The task starts. Goal: {ev.get('goal', '')}"
    if t == "run_end":
        return f"The task ended ({ev.get('status', '')}): {ev.get('message', '')}"
    if t == "activity":
        return ev.get("text", "")
    if t == "screen_description":
        return f"On the screen: {ev.get('text', '')}"
    if t == "decision":
        line = f"Next: {ev.get('action', '')}"
        if (c := ev.get("confidence")) is not None:
            line += f" (sure: {'very' if c >= 0.8 else 'fairly' if c >= 0.5 else 'hardly'})"
        if (p := ev.get("progress")) is not None:
            line += ". The previous action " + ("helped" if p >= 0.5 else "did not help")
        return line
    if t == "guidance_received":
        return f"The human watching says: {ev.get('text', '')}"
    if t == "ask_human":
        return f"Stuck, asking the human watching: {ev.get('question', '')}"
    return {"paused": "Paused by the human watching", "resumed": "Going on again",
            "cancel_requested": "The human watching wants to stop"}.get(t, "")  # fmt: skip


class Thinker:
    """Log lines → a thought, with any OpenAI-compatible chat-completions API."""

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini", base_url: str = "https://api.openai.com/v1",
                 timeout_s: float = 20.0, client: httpx.Client | None = None):  # fmt: skip
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.client = client or httpx.Client(timeout=timeout_s)

    def __call__(self, persona: PersonaVoice, lines: list[str], earlier: list[str]) -> str:
        system = SYSTEM_PROMPT.format(
            name=persona.name or "the user",
            profile=persona.profile_text or "(no profile)",
            voice=f"How {persona.name} speaks: {persona.voice_instructions}\n" if persona.voice_instructions else "",
        )
        user = ""
        if earlier:
            user += "Your last thoughts:\n" + "\n".join(f"- {t}" for t in earlier) + "\n\n"
        user += "What just happened:\n" + "\n".join(f"- {line}" for line in lines)
        body = {"model": self.model, "max_tokens": 120, "temperature": 0.8,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}  # fmt: skip
        r = self.client.post(self.url, json=body, headers=self.headers)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip().strip('"„“').strip()


class Speaker:
    """Text → MP3 with ElevenLabs."""

    def __init__(self, api_key: str, model: str = "eleven_multilingual_v2",
                 base_url: str = "https://api.elevenlabs.io", timeout_s: float = 30.0,
                 client: httpx.Client | None = None):  # fmt: skip
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.headers = {"xi-api-key": api_key, "Accept": "audio/mpeg"}
        self.client = client or httpx.Client(timeout=timeout_s)

    def __call__(self, voice_id: str, text: str) -> bytes:
        r = self.client.post(
            f"{self.base_url}/v1/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            json={"text": text, "model_id": self.model},
            headers=self.headers,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"ElevenLabs: HTTP {r.status_code} {r.text[:200]}")
        return r.content

    def voice_info(self, voice_id: str) -> dict:
        """The voice's name and labels, as ElevenLabs knows it; raises when it doesn't."""
        r = self.client.get(f"{self.base_url}/v1/voices/{voice_id}", headers=self.headers)
        if r.status_code >= 400:
            raise RuntimeError(f"ElevenLabs does not know voice {voice_id}: HTTP {r.status_code} {r.text[:200]}")
        j = r.json()
        labels = [v for v in (j.get("labels") or {}).values() if isinstance(v, str) and v]
        return {"name": j.get("name") or "", "labels": labels, "category": j.get("category") or ""}


class InnerVoice:
    def __init__(
        self,
        emit: Callable[[dict], None],
        persona: Callable[[], PersonaVoice],
        thinker: Callable[[PersonaVoice, list[str], list[str]], str] | None,
        speaker: Callable[[str, str], bytes] | None,
        enabled: bool = True,
        off_reason: str = "",
        chars_per_s: float = CHARS_PER_S,
    ):
        self.emit = emit  # back into the service's event sink (dashboard log)
        self.persona_source = persona
        self.thinker = thinker
        self.speaker = speaker
        self.enabled = enabled
        self.off_reason = off_reason  # a missing key: the inner voice can't work at all
        self.chars_per_s = chars_per_s
        self.persona: PersonaVoice | None = None
        self.failures = 0
        self.clips: OrderedDict[str, bytes] = OrderedDict()
        self._voice_info: dict[str, dict] = {}
        self._earlier: list[str] = []
        self._pending: list[str] = []
        self._busy_until = 0.0
        self._closing = False
        self._q: queue.Queue[dict | None] = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="inner-voice", daemon=True)
        self._thread.start()

    @property
    def available(self) -> bool:
        return not self.off_reason and self.thinker is not None and self.speaker is not None

    # --- the event sink (any thread) ---------------------------------------------

    def __call__(self, event: dict) -> None:
        if event.get("type") == "run_start" or (self.enabled and self.available and event_line(event)):
            self._q.put(event)

    def close(self, timeout_s: float = 5.0) -> None:
        self._q.put(None)
        self._thread.join(timeout_s)

    def clip(self, clip_id: str) -> bytes | None:
        return self.clips.get(clip_id)

    def _store(self, audio: bytes) -> str:
        clip_id = uuid.uuid4().hex[:12]
        self.clips[clip_id] = audio
        while len(self.clips) > KEEP_CLIPS:
            self.clips.popitem(last=False)
        return clip_id

    # --- dashboard (blocking: call from a worker thread) ---------------------------

    def lookup(self) -> dict:
        """The persona of the picked choreo and its voice, looked up now (cached per choreo)."""
        if self.off_reason:
            return {"available": False, "reason": self.off_reason}
        self.persona = p = self.persona_source()
        out = {"available": True, **p.to_json()}
        if p.voice_id and self.speaker is not None and hasattr(self.speaker, "voice_info"):
            if p.voice_id not in self._voice_info:
                try:
                    self._voice_info[p.voice_id] = {"voice": self.speaker.voice_info(p.voice_id)}
                except Exception as e:  # noqa: BLE001 — shown on the dashboard
                    self._voice_info[p.voice_id] = {"voiceError": str(e)}
            out.update(self._voice_info[p.voice_id])
        return out

    def test(self) -> dict:
        """Speaks a short greeting with the persona's voice: the clip to play, or the error."""
        if self.off_reason or self.speaker is None:
            raise RuntimeError(self.off_reason or "the inner voice is off")
        p = self.persona if self.persona and self.persona.ok else self.persona_source()
        self.persona = p
        if not p.ok:
            raise RuntimeError(p.reason)
        first = p.name.split()[0] if p.name else ""
        text = f"Hallo, ich bin {first}. So klingt meine innere Stimme." if first else "So klingt meine innere Stimme."
        return {"clip": self._store(self.speaker(p.voice_id, text)), "text": text}

    def state(self) -> dict:
        p = self.persona
        reason = self.off_reason or (p.reason if p else "")
        return {"available": self.available, "enabled": self.enabled, "reason": reason, "failures": self.failures,
                **({"persona": p.name, "voiceId": p.voice_id} if p else {})}  # fmt: skip

    # --- worker thread ------------------------------------------------------------

    def _note(self, text: str) -> None:
        self.emit({"type": "activity", "text": f"Inner voice: {text}", "_inner": True})

    def _work(self) -> None:
        while not self._closing:
            # with lines pending, wait only until the last clip has ended; else until something comes
            self._drain(max(0.0, self._busy_until - time.time()) if self._pending else None)
            if self._pending and not self._closing and time.time() >= self._busy_until:
                lines, self._pending = self._pending[-MAX_LINES:], []
                try:
                    self._speak(lines)
                except Exception as e:  # noqa: BLE001 — the inner voice must never break a run
                    self.failures += 1
                    log.warning("inner voice failed: %s", e)

    def _drain(self, timeout: float | None) -> None:
        """Takes the next event (waiting up to `timeout`), then everything that arrived meanwhile."""
        try:
            ev = self._q.get(timeout=timeout) if timeout != 0 else self._q.get_nowait()
        except queue.Empty:
            return
        while True:
            if ev is None:
                self._closing = True
                return
            try:
                self._take(ev)
            except Exception as e:  # noqa: BLE001 — e.g. the persona lookup; the thread must live on
                self.failures += 1
                log.warning("inner voice: %s", e)
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                return

    def _take(self, ev: dict) -> None:
        if ev.get("type") == "run_start":
            self._pending, self._earlier = [], []
            if self.available and self.enabled:
                self.persona = self.persona_source()
                if not self.persona.ok:
                    self._note(f"off: {self.persona.reason}")
            elif self.off_reason and self.enabled:
                self._note(f"off: {self.off_reason}")
        elif self.persona is None and self.enabled and self.available:  # switched on during a run
            self.persona = self.persona_source()
        if not (self.enabled and self.available and self.persona and self.persona.ok):
            return
        if line := event_line(ev):
            self._pending.append(line)

    def _speak(self, lines: list[str]) -> None:
        persona = self.persona
        if persona is None or not persona.ok or self.thinker is None or self.speaker is None:
            return
        text = self.thinker(persona, lines, self._earlier[-3:])
        if not text:
            return
        self._earlier.append(text)
        clip_id = ""
        try:
            clip_id = self._store(self.speaker(persona.voice_id, text))
        except Exception as e:  # noqa: BLE001 — the thought still shows as text
            self.failures += 1
            self._note(f"could not speak: {e}")
        self._busy_until = time.time() + len(text) / self.chars_per_s
        self.emit({"type": "inner_voice", "text": text, "clip": clip_id, "persona": persona.name})

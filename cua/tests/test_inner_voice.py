from __future__ import annotations

import threading
import time

import httpx
from conftest import make_screen
from starlette.testclient import TestClient
from test_loop import HOME

from kvm_cua.config import Settings
from kvm_cua.decision.decider import StubDecider
from kvm_cua.kvm.device import FakeKvm
from kvm_cua.mcp_server import build_server
from kvm_cua.narration.inner_voice import InnerVoice, Speaker, Thinker, event_line
from kvm_cua.narration.leanscope_auth import ChoreoSelection
from kvm_cua.narration.persona_voice import PersonaVoice, find_voice, resolve_persona_voice
from kvm_cua.perception.parser import FixtureParser
from kvm_cua.service import CuaService

HEINZ = "heinz-guid"
VOICE_RES = "795f549a-e5f9-4a81-a5ad-8ece7076a145"
VOICE_EDGE = "d74f8c85-2954-4a05-abe6-7fc68698aebf"
VOICE_PARAM = "94896ee6-bf6c-46a1-b404-e872365ef034"
SEL = ChoreoSelection(repository_id="42", scenario_guid="choreo-1")


def uxm(relationships, equipments=(), persona_voice="cedar"):
    return {"uxmData": {
        "scenarios": [{"_guid": "choreo-1", "primaryActorRef": HEINZ}],
        "personas": [{"_guid": HEINZ, "name": "Heinz", "voiceId": persona_voice, "quote": "Bitte schnell.",
                      "profile": [{"description": "ungeduldig"}], "motivations": [], "frustrations": [],
                      "sociodemographicInfo": [{"key": "Age", "description": "61"}]}],
        "equipments": list(equipments),
        "resourceRelationships": list(relationships),
    }}  # fmt: skip


def edge(guid, owner, resource, overrides=()):
    return {"_guid": guid, "ownerRef": owner, "resourceRef": resource,
            "parameterOverrides": [{"parameterRef": r, "value": v} for r, v in overrides]}  # fmt: skip


# --- persona and voice -------------------------------------------------------------


def test_voice_comes_from_the_override_on_the_persona_edge():
    data = uxm([edge(VOICE_EDGE, HEINZ, VOICE_RES, [(VOICE_PARAM, "EL1234567890abcdefgh")])])
    pv = resolve_persona_voice(lambda q, v: data, SEL)
    assert pv.ok and pv.name == "Heinz" and pv.voice_id == "EL1234567890abcdefgh"
    assert pv.path == [VOICE_EDGE]
    assert "ungeduldig" in pv.profile_text and "Age: 61" in pv.profile_text


def test_voice_through_a_voice_resource_and_its_binding():
    # persona → voice resource (no override); the voice resource's IS binding pins the voiceId
    rels = [edge(VOICE_EDGE, HEINZ, VOICE_RES), edge("is-1", VOICE_RES, "voice-type", [(VOICE_PARAM, "abc")])]
    assert find_voice(HEINZ, rels, []) == ("abc", [VOICE_EDGE, "is-1"])
    # or the voice resource declares it itself
    eq = [{"_guid": VOICE_RES, "parameters": [{"_guid": "p", "key": "voiceId", "value": "own"}]}]
    assert find_voice(HEINZ, [edge(VOICE_EDGE, HEINZ, VOICE_RES)], eq) == ("own", [VOICE_EDGE])


def test_override_matched_by_declared_key_and_depth_is_bounded():
    eq = [{"_guid": "type", "parameters": [{"_guid": "custom-param", "key": "voiceId", "value": ""}]}]
    assert find_voice(HEINZ, [edge("e", HEINZ, "x", [("custom-param", "v1")])], eq) == ("v1", ["e"])
    far = [edge("1", HEINZ, "a"), edge("2", "a", "b"), edge("3", "b", "c"), edge("4", "c", "d", [(VOICE_PARAM, "x")])]
    assert find_voice(HEINZ, far, []) == ("", [])


def test_openai_voice_name_on_the_persona_does_not_count():
    pv = resolve_persona_voice(lambda q, v: uxm([]), SEL)
    assert not pv.ok and "no ElevenLabs voice" in pv.reason
    pv = resolve_persona_voice(lambda q, v: uxm([]), SEL, fallback_voice_id="fallback")
    assert pv.ok and pv.voice_id == "fallback"
    pv = resolve_persona_voice(lambda q, v: uxm([], persona_voice="EL1234567890abcdefgh"), SEL)
    assert pv.voice_id == "EL1234567890abcdefgh"


def test_persona_override_and_errors():
    assert "not in repository" in resolve_persona_voice(lambda q, v: uxm([]), SEL, persona_override="other").reason

    def boom(q, v):
        raise RuntimeError("HTTP 500")

    assert "HTTP 500" in resolve_persona_voice(boom, SEL).reason
    assert resolve_persona_voice(lambda q, v: {}, ChoreoSelection()).reason == "no choreo picked in the dashboard"


# --- thoughts and speech -----------------------------------------------------------

HEINZ_VOICE = PersonaVoice(persona_guid=HEINZ, name="Heinz", profile_text="Name: Heinz", voice_id="v-heinz")


class Recorder:
    def __init__(self, thought="Na endlich, die Einstellungen.", slow_s=0.0, fail_tts=False):
        self.thought, self.slow_s, self.fail_tts = thought, slow_s, fail_tts
        self.thinks: list[list[str]] = []
        self.speaks: list[tuple[str, str]] = []
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def think(self, persona, lines, earlier):
        time.sleep(self.slow_s)
        self.thinks.append(lines)
        return self.thought

    def speak(self, voice_id, text):
        if self.fail_tts:
            raise RuntimeError("ElevenLabs: HTTP 401")
        self.speaks.append((voice_id, text))
        return b"ID3mp3"

    def emit(self, ev):
        with self.lock:
            self.events.append(ev)

    def inner(self, **kw):
        return InnerVoice(self.emit, lambda: HEINZ_VOICE, self.think, self.speak, **kw)


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    assert cond()


def test_lines_pile_up_while_a_thought_is_made():
    rec = Recorder(slow_s=0.3)
    iv = rec.inner(chars_per_s=1000)
    iv({"type": "run_start", "goal": "WLAN öffnen"})
    time.sleep(0.05)
    for i in range(5):
        iv({"type": "activity", "text": f"micro step {i}"})
    wait_for(lambda: len(rec.thinks) >= 2)
    iv.close()
    assert rec.thinks[0] == ["The task starts. Goal: WLAN öffnen"]
    assert rec.thinks[1] == [f"micro step {i}" for i in range(5)]  # one thought for all of them
    said = [e for e in rec.events if e["type"] == "inner_voice"]
    assert said[0]["text"] == "Na endlich, die Einstellungen." and said[0]["persona"] == "Heinz"
    assert iv.clip(said[0]["clip"]) == b"ID3mp3"
    assert rec.speaks[0][0] == "v-heinz"


def test_waits_for_the_clip_to_end():
    rec = Recorder()
    iv = rec.inner(chars_per_s=60)  # the 30-char thought "plays" for 0.5 s
    iv({"type": "run_start", "goal": "g"})
    wait_for(lambda: len(rec.thinks) == 1)
    iv({"type": "activity", "text": "a"})
    iv({"type": "activity", "text": "b"})
    time.sleep(0.2)
    assert len(rec.thinks) == 1  # still speaking
    wait_for(lambda: len(rec.thinks) == 2)
    assert rec.thinks[1] == ["a", "b"]
    iv.close()


def test_silence_and_failures_never_raise():
    rec = Recorder(thought="")
    iv = rec.inner(chars_per_s=1000)
    iv({"type": "run_start", "goal": "g"})
    wait_for(lambda: rec.thinks)
    time.sleep(0.05)
    assert not rec.speaks and not [e for e in rec.events if e["type"] == "inner_voice"]
    iv.close()

    rec = Recorder(fail_tts=True)
    iv = rec.inner(chars_per_s=1000)
    iv({"type": "run_start", "goal": "g"})
    wait_for(lambda: any(e["type"] == "inner_voice" for e in rec.events))
    said = next(e for e in rec.events if e["type"] == "inner_voice")
    assert said["clip"] == "" and said["text"]
    assert any("could not speak" in e.get("text", "") for e in rec.events if e["type"] == "activity")
    assert iv.failures == 1
    iv.close()


def test_off_reasons_are_logged_once_per_run_and_muted_drops_lines():
    rec = Recorder()
    iv = InnerVoice(rec.emit, lambda: HEINZ_VOICE, None, None, off_reason="ELEVENLABS_API_KEY is not set in .env")
    iv({"type": "run_start", "goal": "g"})
    iv({"type": "activity", "text": "x"})
    wait_for(lambda: rec.events)
    iv.close()
    assert [e["text"] for e in rec.events] == ["Inner voice: off: ELEVENLABS_API_KEY is not set in .env"]
    assert rec.events[0]["_inner"] and event_line(rec.events[0]) == ""  # never retold by itself

    rec = Recorder()
    iv = rec.inner(enabled=False)
    iv({"type": "run_start", "goal": "g"})
    iv({"type": "activity", "text": "x"})
    time.sleep(0.1)
    iv.close()
    assert not rec.thinks and not rec.events


def test_event_lines_hide_numbers():
    line = event_line({"type": "decision", "action": "click 'Settings'", "confidence": 0.82, "progress": 0.2})
    assert line == "Next: click 'Settings' (sure: very). The previous action did not help"
    assert event_line({"type": "screen_raw", "seq": 1}) == ""


def test_thinker_and_speaker_requests():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if "chat/completions" in str(req.url):
            return httpx.Response(200, json={"choices": [{"message": {"content": "„Wo ist das bloß?“"}}]})
        return httpx.Response(200, content=b"mp3")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    t = Thinker("sk", client=client)
    assert t(HEINZ_VOICE, ["Next: click 'Settings'"], ["Na gut."]) == "Wo ist das bloß?"
    body = seen[0].read().decode()
    assert "inner voice of Heinz" in body and "Na gut." in body and "German" in body
    assert Speaker("xi", client=client)("v-heinz", "Hallo") == b"mp3"
    assert seen[1].url.path == "/v1/text-to-speech/v-heinz" and seen[1].headers["xi-api-key"] == "xi"


# --- dashboard -----------------------------------------------------------------------


def test_dashboard_serves_clips_and_switch(tmp_path):
    service = CuaService(Settings(cua_runs_dir=tmp_path, _env_file=None), device=FakeKvm([make_screen(HOME)]), parser=FixtureParser([]),
                         decider=StubDecider())  # fmt: skip
    service.inner_voice.clips["abc"] = b"ID3mp3"
    app = build_server(service).streamable_http_app()
    with TestClient(app) as c:
        r = c.get("/dashboard/api/inner-voice/abc.mp3")
        assert r.status_code == 200 and r.content == b"ID3mp3" and r.headers["content-type"] == "audio/mpeg"
        assert c.get("/dashboard/api/inner-voice/nope.mp3").status_code == 404
        state = c.get("/dashboard/api/state").json()["innerVoice"]
        assert state["available"] is False and "ELEVENLABS_API_KEY" in state["reason"]
        assert c.post("/dashboard/api/inner-voice", json={"on": False}).json()["result"]["enabled"] is False
    service.inner_voice.close()


def test_persona_card_data():
    data = uxm([edge(VOICE_EDGE, HEINZ, VOICE_RES, [(VOICE_PARAM, "EL1234567890abcdefgh")])],
               [{"_guid": VOICE_RES, "line": "🗣️ Heinz", "parameters": []}])  # fmt: skip
    data["uxmData"]["personas"][0]["imageRef"] = "img-1"
    data["uxmData"]["personaImages"] = [{"_guid": "img-1", "imagePath": "https://media.leanscope.io/media/1/h.png"}]
    pv = resolve_persona_voice(lambda q, v: data, SEL)
    assert pv.image_url == "https://media.leanscope.io/media/1/h.png"
    assert pv.voice_source == "resource “🗣️ Heinz”"
    data["uxmData"]["personaImages"] = []
    assert resolve_persona_voice(lambda q, v: data, SEL).image_url.endswith("/media/000/img-1")


class InfoSpeaker:
    def __init__(self, known=True):
        self.known, self.said = known, []

    def __call__(self, voice_id, text):
        self.said.append((voice_id, text))
        return b"ID3"

    def voice_info(self, voice_id):
        if not self.known:
            raise RuntimeError(f"ElevenLabs does not know voice {voice_id}: HTTP 404")
        return {"name": "Heinz (deep)", "labels": ["old", "german"], "category": "cloned"}


def test_lookup_and_test_greeting():
    sp = InfoSpeaker()
    iv = InnerVoice(lambda ev: None, lambda: HEINZ_VOICE, lambda *a: "", sp)
    info = iv.lookup()
    assert info["persona"] == "Heinz" and info["voice"]["name"] == "Heinz (deep)"
    r = iv.test()
    assert r["text"].startswith("Hallo, ich bin Heinz.") and iv.clip(r["clip"]) == b"ID3"
    assert sp.said == [("v-heinz", r["text"])]
    iv.close()

    iv = InnerVoice(lambda ev: None, lambda: HEINZ_VOICE, lambda *a: "", InfoSpeaker(known=False))
    assert "HTTP 404" in iv.lookup()["voiceError"]
    iv.close()
    iv = InnerVoice(lambda ev: None, lambda: HEINZ_VOICE, None, None, off_reason="ELEVENLABS_API_KEY is not set")
    assert iv.lookup() == {"available": False, "reason": "ELEVENLABS_API_KEY is not set"}
    iv.close()

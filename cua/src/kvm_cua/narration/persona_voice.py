"""Who acts in the picked choreo, and which ElevenLabs voice speaks for them.

- The persona is the choreo's primary actor (`primaryActorRef`), or `CUA_INNER_VOICE_PERSONA_GUID`.
- The voice is found the way the agent server finds it (`EquipmentResolver.findModelPath`):
  breadth-first over the resource relationships the persona owns, a few edges deep. The nearest
  `voiceId` wins, whether it is overridden on a relationship (the usual case: persona → voice
  resource, `voiceId` pinned on that edge) or declared on a resource on the way.
- The persona's own `voiceId` field usually names an OpenAI voice ("cedar"), so it only counts
  when it looks like an ElevenLabs voice id. `CUA_INNER_VOICE_ID` is the last fallback.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .leanscope_auth import ChoreoSelection, LeanScopeCatalog

log = logging.getLogger(__name__)

# the `voiceId` parameter of the Speech Synthesis subtype, and its legacy predecessor `voice`
VOICE_ID_PARAM_GUIDS = {"94896ee6-bf6c-46a1-b404-e872365ef034", "da8735e6-66d5-4d77-a8c7-7ffc786cc6ed"}
VOICE_PARAM_KEYS = {"voiceId", "voice"}
MAX_DEPTH = 3
ELEVENLABS_ID = re.compile(r"[A-Za-z0-9]{20}")
# the same image chain as the agent server: repository image, shared pool, placeholder
STATIC_MEDIA_URL_PREFIX = "https://api.leanscope.io/media/000/"
FALLBACK_IMAGE = "https://app.leanscope.io/static-assets/images/persona/0000.jpg"

QUERY = """query($repositoryId: Int!) { uxmData(repositoryId: $repositoryId) {
  scenarios { _guid primaryActorRef }
  personas { _guid name imageRef instructions quote voiceId voiceInstructions
    sociodemographicInfo { key description } motivations { description }
    frustrations { description } profile { description } }
  personaImages { _guid imagePath }
  equipments { _guid line parameters { _guid key value } }
  resourceRelationships { _guid ownerRef resourceRef parameterOverrides { parameterRef value } }
} }"""


@dataclass
class PersonaVoice:
    persona_guid: str = ""
    name: str = ""
    profile_text: str = ""  # who the persona is, for the inner-voice prompt
    voice_instructions: str = ""
    voice_id: str = ""
    path: list[str] = field(default_factory=list)  # the relationships crossed to the voice
    voice_source: str = ""  # where the voice id was found, for the dashboard
    image_url: str = ""
    reason: str = ""  # why there is no inner voice; empty when there is one

    @property
    def ok(self) -> bool:
        return bool(self.voice_id) and not self.reason

    def to_json(self) -> dict:
        return {"personaGuid": self.persona_guid, "persona": self.name, "voiceId": self.voice_id,
                "voiceSource": self.voice_source, "imageUrl": self.image_url, "path": self.path,
                "reason": self.reason}  # fmt: skip


def profile_text(p: dict) -> str:
    def items(key: str) -> list[str]:
        return [x["description"] for x in p.get(key) or [] if x and x.get("description")]

    parts = [f"Name: {p.get('name') or ''}"]
    facts = [f"{x['key']}: {x['description']}" for x in p.get("sociodemographicInfo") or [] if x and x.get("key")]
    if facts:
        parts.append("Facts: " + "; ".join(facts))
    for label, key in (("Traits", "profile"), ("Motivations", "motivations"), ("Frustrations", "frustrations")):
        if v := items(key):
            parts.append(f"{label}: " + "; ".join(v))
    if p.get("quote"):
        parts.append(f"Typical quote: {p['quote']}")
    if p.get("instructions"):
        parts.append(f"How to play them: {p['instructions']}")
    return "\n".join(parts)


def persona_image_url(persona: dict, images: list[dict]) -> str:
    ref = persona.get("imageRef") or ""
    if not ref:
        return FALLBACK_IMAGE
    path = next((i.get("imagePath") for i in images if i and i.get("_guid") == ref), None)
    if isinstance(path, str) and path.startswith("http"):
        return path
    return STATIC_MEDIA_URL_PREFIX + ref  # the dashboard falls back to FALLBACK_IMAGE if this fails


def find_voice(owner: str, relationships: list[dict], equipments: list[dict]) -> tuple[str, list[str]]:
    """The nearest voice id reachable from `owner`, and the relationship guids crossed to it."""
    by_owner: dict[str, list[dict]] = {}
    for rr in relationships:
        if rr and rr.get("ownerRef"):
            by_owner.setdefault(rr["ownerRef"], []).append(rr)
    params = {e["_guid"]: e.get("parameters") or [] for e in equipments if e and e.get("_guid")}
    keys = {p["_guid"]: p.get("key") for ps in params.values() for p in ps if p and p.get("_guid")}

    def override(rr: dict) -> str:
        for o in rr.get("parameterOverrides") or []:
            ref = (o or {}).get("parameterRef") or ""
            if (ref in VOICE_ID_PARAM_GUIDS or keys.get(ref) in VOICE_PARAM_KEYS) and (o.get("value") or "").strip():
                return o["value"].strip()
        return ""

    def declared(guid: str) -> str:
        for p in params.get(guid, []):
            if p and p.get("key") in VOICE_PARAM_KEYS and (p.get("value") or "").strip():
                return p["value"].strip()
        return ""

    seen = {owner}
    frontier: list[tuple[str, list[str]]] = [(owner, [])]
    for _ in range(MAX_DEPTH):
        nxt: list[tuple[str, list[str]]] = []
        for guid, path in frontier:
            for rr in by_owner.get(guid, []):
                here = [*path, rr.get("_guid") or ""]
                if v := override(rr):
                    return v, here
                target = rr.get("resourceRef") or ""
                if not target or target in seen:
                    continue
                seen.add(target)
                if v := declared(target):
                    return v, here
                nxt.append((target, here))
        frontier = nxt
    return "", []


def resolve_persona_voice(
    query: Callable[[str, dict], dict],
    selection: ChoreoSelection,
    persona_override: str = "",
    fallback_voice_id: str = "",
) -> PersonaVoice:
    """Never raises: a problem comes back as `reason`."""
    if not selection.repository_id or not (selection.scenario_guid or persona_override):
        return PersonaVoice(reason="no choreo picked in the dashboard")
    try:
        data = query(QUERY, {"repositoryId": int(selection.repository_id)})
    except Exception as e:  # noqa: BLE001 — shown on the dashboard
        return PersonaVoice(reason=f"could not read the persona from LeanScope: {e}")
    uxm = data.get("uxmData") or {}
    guid = persona_override
    if not guid:
        scn = next((s for s in uxm.get("scenarios") or [] if s and s.get("_guid") == selection.scenario_guid), None)
        guid = (scn or {}).get("primaryActorRef") or ""
        if not guid:
            return PersonaVoice(reason="the picked choreo has no primary actor (persona)")
    p = next((x for x in uxm.get("personas") or [] if x and x.get("_guid") == guid), None)
    if p is None:
        return PersonaVoice(persona_guid=guid, reason=f"persona {guid} is not in repository {selection.repository_id}")
    pv = PersonaVoice(persona_guid=guid, name=p.get("name") or "", profile_text=profile_text(p),
                      voice_instructions=p.get("voiceInstructions") or "",
                      image_url=persona_image_url(p, uxm.get("personaImages") or []))  # fmt: skip
    rels = uxm.get("resourceRelationships") or []
    equipments = uxm.get("equipments") or []
    pv.voice_id, pv.path = find_voice(guid, rels, equipments)
    if pv.voice_id:
        # name the resource the persona attaches (the first edge): usually the voice resource
        first = next((r for r in rels if r and r.get("_guid") == pv.path[0]), {})
        line = next((e.get("line") for e in equipments if e and e.get("_guid") == first.get("resourceRef")), "")
        pv.voice_source = f"resource “{line}”" if line else f"relationship {pv.path[0]}"
    own = (p.get("voiceId") or "").strip()
    if not pv.voice_id and ELEVENLABS_ID.fullmatch(own):
        pv.voice_id, pv.voice_source = own, "the persona's voiceId field"
    if not pv.voice_id and fallback_voice_id:
        pv.voice_id, pv.voice_source = fallback_voice_id, "CUA_INNER_VOICE_ID"
    if not pv.voice_id:
        pv.reason = f"{pv.name or 'the persona'} has no ElevenLabs voice attached (set CUA_INNER_VOICE_ID to use one anyway)"
    log.info("inner voice: persona %s, voice %s via %s", pv.name, pv.voice_id or "-", pv.path or "-")
    return pv


class PersonaVoiceCache:
    """One lookup per (repository, choreo, persona)."""

    def __init__(self, catalog: LeanScopeCatalog | None, selection: Callable[[], ChoreoSelection],
                 persona_override: str = "", fallback_voice_id: str = ""):  # fmt: skip
        self.catalog = catalog
        self.selection = selection
        self.persona_override = persona_override
        self.fallback_voice_id = fallback_voice_id
        self._key: tuple | None = None
        self.current = PersonaVoice(reason="not looked up yet")

    def get(self) -> PersonaVoice:
        if self.catalog is None:
            return PersonaVoice(reason="LeanScope is not configured (LEANSCOPE_AGENT_SERVER_URL)")
        if not self.catalog.auth.logged_in:
            return PersonaVoice(reason="not logged in to LeanScope")
        sel = self.selection()
        key = (sel.repository_id, sel.scenario_guid, self.persona_override)
        if key != self._key or not self.current.ok:
            self.current = resolve_persona_voice(self.catalog._query, sel, self.persona_override,
                                                 self.fallback_voice_id)  # fmt: skip
            self._key = key
        return self.current

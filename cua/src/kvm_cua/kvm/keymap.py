"""Text and key-combo to HID reports, using the web UI's keyboard layouts.

keymaps.json is generated from ui/src/keyboardLayouts (cua/scripts/export_keymaps.sh),
so typing behaves exactly like the JetKVM paste feature, dead keys included.
"""

from __future__ import annotations

import json
import unicodedata
from functools import lru_cache
from importlib import resources

from .device import KeyEvent


@lru_cache
def _data() -> dict:
    return json.loads(resources.files("kvm_cua.data").joinpath("keymaps.json").read_text("utf-8"))


def layouts() -> list[str]:
    return sorted(_data()["layouts"])


def _code(key: str | int) -> int:
    if isinstance(key, int):
        return key
    keys = _data()["keys"]
    if key not in keys:
        raise KeyError(f"unknown key {key!r}")
    return int(keys[key])


MOD = {
    "ctrl": 0x01, "control": 0x01, "shift": 0x02, "alt": 0x04, "option": 0x04,
    "meta": 0x08, "cmd": 0x08, "win": 0x08, "super": 0x08, "altgr": 0x40,
}  # fmt: skip

# Friendly names for press("ctrl+l"), mapped to the UI's key names.
_ALIASES = {
    "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab",
    "space": "Space", "backspace": "Backspace", "delete": "Delete", "del": "Delete",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown", "insert": "Insert",
}  # fmt: skip


class UntypableError(ValueError):
    pass


def combo(spec: str) -> KeyEvent:
    """'ctrl+shift+t', 'enter', 'f5', 'alt+tab' → one key-down report (US key positions)."""
    modifier = 0
    keys: list[int] = []
    for part in [p.strip() for p in spec.split("+") if p.strip()]:
        low = part.lower()
        if low in MOD:
            modifier |= MOD[low]
            continue
        if low in _ALIASES:
            name = _ALIASES[low]
        elif len(part) == 1 and part.isalpha():
            name = f"Key{part.upper()}"
        elif len(part) == 1 and part.isdigit():
            name = f"Digit{part}"
        elif low.startswith("f") and low[1:].isdigit():
            name = f"F{low[1:]}"
        else:
            name = part
        keys.append(_code(name))
    if len(keys) > 6:
        raise ValueError("at most 6 keys in one combo")
    return KeyEvent(modifier=modifier, keys=keys)


def _mods(info: dict) -> int:
    m = 0
    if info.get("shift"):
        m |= 0x02
    if info.get("altRight"):
        m |= 0x40
    return m


def text_to_events(text: str, layout: str = "en-US", hold_ms: int = 20, gap_ms: int = 15) -> list[KeyEvent]:
    """Reports to type `text`: each stroke is a press (held hold_ms) and a release."""
    data = _data()["layouts"]
    if layout not in data:
        raise UntypableError(f"unknown keyboard layout {layout!r}; known: {', '.join(sorted(data))}")
    chars = data[layout]["chars"]
    out: list[KeyEvent] = []

    def stroke(key: str | int, modifier: int) -> None:
        out.append(KeyEvent(modifier=modifier, keys=[_code(key)], delay_ms=hold_ms))
        out.append(KeyEvent(modifier=0, keys=[], delay_ms=gap_ms))

    for ch in unicodedata.normalize("NFC", text):
        if ch == "\n":
            stroke("Enter", 0)
            continue
        if ch == "\t":
            stroke("Tab", 0)
            continue
        info = chars.get(ch)
        if info is None:
            raise UntypableError(f"character {ch!r} cannot be typed with layout {layout}")
        accent = info.get("accentKey")
        if accent:
            stroke(accent["key"], _mods(accent))
        stroke(info["key"], _mods(info))
        if info.get("deadKey"):
            stroke("Space", 0)
    return out

from __future__ import annotations

import random

from PIL import Image

from kvm_cua.kvm.actuation import Actuator, MotionProfile, human_path
from kvm_cua.kvm.decode import decode_last_frame
from kvm_cua.kvm.device import ABS_MAX, FakeKvm
from kvm_cua.kvm.keymap import combo, text_to_events


def test_decode_gop(fixtures_dir):
    img = decode_last_frame((fixtures_dir / "gop.h264").read_bytes(), "h264")
    assert img.size == (320, 240)


def test_keymap_us_shift_and_release():
    ev = text_to_events("Hi!", "en-US")
    # 3 chars → press + release each
    assert len(ev) == 6
    assert ev[0].modifier == 0x02 and ev[0].keys == [0x0B]  # shift+h
    assert ev[1].keys == [] and ev[1].modifier == 0
    assert ev[4].modifier == 0x02 and ev[4].keys == [0x1E]  # shift+1 → !


def test_keymap_de_layout_and_accents():
    z = text_to_events("z", "de-DE")
    assert z[0].keys == [0x1C]  # German z sits on the US Y key
    e_acute = text_to_events("é", "de-DE")
    assert len(e_acute) == 4  # accent key stroke, then the letter
    at = text_to_events("@", "de-DE")
    assert at[0].modifier == 0x40 and at[0].keys == [0x14]  # AltGr+Q


def test_combo():
    c = combo("ctrl+shift+t")
    assert c.modifier == 0x03 and c.keys == [0x17]
    assert combo("enter").keys == [0x28]
    assert combo("esc").keys == [0x29]
    assert combo("f5").keys == [0x3E]


def test_human_path_ends_on_target_and_is_curved():
    rng = random.Random(1)
    pts = human_path((100, 100), (900, 500), rng, MotionProfile())
    assert pts[-1][:2] == (900, 500)
    assert len(pts) > 15
    assert all(0 <= d <= 20 for *_, d in pts)


def test_actuator_abs_mapping_and_click():
    kvm = FakeKvm([Image.new("RGB", (1920, 1080))])
    a = Actuator(kvm, seed=3)
    assert a.to_abs(0, 0) == (0, 0)
    assert a.to_abs(1919, 1079) == (ABS_MAX, ABS_MAX)
    assert a.to_abs(5000, -3) == (ABS_MAX, 0)
    a.click(960, 540)
    mouse = [e for kind, e in kvm.events if kind == "mouse"]
    buttons = [e.buttons for e in mouse]
    assert 1 in buttons and buttons[-1] == 0
    down = next(e for e in mouse if e.buttons == 1)
    assert (down.x, down.y) == a.to_abs(960, 540)


def test_scroll_directions():
    kvm = FakeKvm([Image.new("RGB", (800, 600))])
    a = Actuator(kvm, seed=1)
    a.scroll("down", 2)
    a.scroll("right", 1)
    wheel = [e for kind, e in kvm.events if kind == "wheel"]
    assert [(w.dy, w.dx) for w in wheel] == [(-1, 0), (-1, 0), (0, 1)]

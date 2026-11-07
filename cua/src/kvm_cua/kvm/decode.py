"""Decode the firmware's GOP (keyframe + following frames) into the latest picture."""

from __future__ import annotations

import av
from PIL import Image

_CODECS = {"h264": "h264", "h265": "hevc", "hevc": "hevc"}


def decode_last_frame(data: bytes, codec: str = "h264") -> Image.Image:
    name = _CODECS.get(codec.lower())
    if name is None:
        raise ValueError(f"unsupported codec {codec!r}")
    ctx = av.CodecContext.create(name, "r")
    last = None
    for packet in ctx.parse(data):
        for frame in ctx.decode(packet):
            last = frame
    # flush: the parser holds back the final access unit until it sees the end
    for packet in ctx.parse(b""):
        for frame in ctx.decode(packet):
            last = frame
    try:
        for frame in ctx.decode(None):
            last = frame
    except av.error.EOFError:
        pass
    if last is None:
        raise ValueError(f"no decodable picture in {len(data)} bytes of {codec}")
    return last.to_image().convert("RGB")

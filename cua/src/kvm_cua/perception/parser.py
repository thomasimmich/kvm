"""Screen → UI elements.

`OmniParserV2` re-implements Microsoft OmniParser v2's pipeline
(https://github.com/microsoft/OmniParser) on its published weights:

1. OCR (EasyOCR) finds text boxes.
2. A YOLO model (icon_detect) finds interactable regions: buttons, icons, fields.
3. Boxes are merged: text inside an icon box becomes that icon's label, icon
   boxes inside text are dropped, duplicate icons are removed.
4. Icons still without a label get a short caption from the fine-tuned
   Florence-2 model (icon_caption), e.g. "a gear icon for settings".

Every parser implements `UIParser.parse(image) -> list[UIElement]`, so the
pipeline can be swapped (hosted GPU, Apple Vision OCR, …) without touching the
agent loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image

from .screen import iou
from .types import Box, UIElement

log = logging.getLogger(__name__)

OMNIPARSER_REPO = "microsoft/OmniParser-v2.0"
FLORENCE_PROCESSOR_REPO = "microsoft/Florence-2-base"


class UIParser(Protocol):
    name: str

    def parse(self, image: Image.Image) -> list[UIElement]: ...


class NullParser:
    """No elements; useful to exercise the loop's scroll/stuck paths."""

    name = "null"

    def parse(self, image: Image.Image) -> list[UIElement]:
        return []


class FixtureParser:
    """Returns pre-recorded elements per screen (by index or by image identity) — for tests and replays."""

    name = "fixture"

    def __init__(self, per_screen: Sequence[Sequence[UIElement]]):
        self._per_screen = [list(s) for s in per_screen]
        self._images: list[bytes] = []

    def bind(self, images: Sequence[Image.Image]) -> FixtureParser:
        self._images = [im.tobytes() for im in images]
        return self

    def parse(self, image: Image.Image) -> list[UIElement]:
        raw = image.tobytes()
        if raw in self._images:
            return [UIElement(**vars(e)) for e in self._per_screen[self._images.index(raw)]]
        raise KeyError("FixtureParser: unknown screen (call bind() with the fixture images)")

    @staticmethod
    def load(path: Path) -> list[UIElement]:
        return [UIElement.from_json(d) for d in json.loads(Path(path).read_text())]


def _device(pref: str) -> str:
    import torch

    if pref != "auto":
        return pref
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _quad_to_box(quad) -> Box:
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def _area(b: Box) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _inter(a: Box, b: Box) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


class AppleVisionOcr:
    """macOS Vision framework OCR (via ocrmac): ~0.5 s for a 1080p screen, no model download."""

    def __init__(self, languages: Sequence[str] = ("en-US", "de-DE")):
        from ocrmac import ocrmac  # noqa: F401  (fail early off-macOS)

        self.languages = list(languages)

    def read(self, image: Image.Image, min_conf: float = 0.3) -> list[tuple[Box, str, float]]:
        from ocrmac import ocrmac

        w, h = image.size
        out = []
        for text, conf, (x, y, bw, bh) in ocrmac.OCR(
            image, recognition_level="accurate", language_preference=self.languages
        ).recognize():
            text = text.strip()
            if not text or conf < min_conf:
                continue
            # Vision boxes are normalised with the origin at the bottom-left
            box = (int(x * w), int((1 - y - bh) * h), int((x + bw) * w), int((1 - y) * h))
            out.append((box, text, float(conf)))
        return out


def make_ocr(kind: str = "auto"):
    """'vision' on macOS (fast), otherwise EasyOCR (uses CUDA when available)."""
    import sys

    if kind == "vision" or (kind == "auto" and sys.platform == "darwin"):
        try:
            return AppleVisionOcr()
        except ImportError:
            if kind == "vision":
                raise
            log.warning("ocrmac not installed, falling back to EasyOCR")
    import torch

    return EasyOcr(gpu=torch.cuda.is_available())


class EasyOcr:
    def __init__(self, languages: Sequence[str] = ("en", "de"), gpu: bool = False):
        import easyocr

        # EasyOCR's GPU path is CUDA-only; on Mac it runs on CPU, which is fine for 1080p.
        self._reader = easyocr.Reader(list(languages), gpu=gpu, verbose=False)

    def read(self, image: Image.Image, min_conf: float = 0.4) -> list[tuple[Box, str, float]]:
        out = []
        for quad, text, conf in self._reader.readtext(np.asarray(image), paragraph=False, text_threshold=0.8):
            text = text.strip()
            if text and conf >= min_conf:
                out.append((_quad_to_box(quad), text, float(conf)))
        return out


class OcrOnlyParser:
    """Text boxes only — light, but misses unlabeled icons."""

    name = "ocr"

    def __init__(self, ocr: str = "auto"):
        self._ocr = make_ocr(ocr)

    def parse(self, image: Image.Image) -> list[UIElement]:
        return [
            UIElement(id=i, kind="text", label=t, bbox=b, interactable=True, score=c, source="ocr")
            for i, (b, t, c) in enumerate(self._ocr.read(image))
        ]


def merge_boxes(
    icons: list[tuple[Box, float]],
    texts: list[tuple[Box, str, float]],
    icon_iou_dup: float = 0.7,
    contain: float = 0.6,
) -> list[UIElement]:
    """OmniParser-style merge of detector boxes and OCR boxes into one element list."""
    # 1. de-duplicate icons, keeping the more confident one
    icons = sorted(icons, key=lambda x: -x[1])
    kept: list[tuple[Box, float]] = []
    for box, score in icons:
        if all(iou(box, k[0]) < icon_iou_dup for k in kept):
            kept.append((box, score))

    used_text: set[int] = set()
    elements: list[UIElement] = []
    for box, score in kept:
        # icon that is really just a word inside a text line → drop it, the text covers it
        if any(_inter(box, t[0]) >= contain * max(1, _area(box)) for t in texts):
            continue
        inside = [i for i, t in enumerate(texts) if _inter(box, t[0]) >= contain * max(1, _area(t[0]))]
        label = " ".join(texts[i][1] for i in sorted(inside, key=lambda i: (texts[i][0][1], texts[i][0][0])))
        used_text.update(inside)
        elements.append(
            UIElement(id=0, kind="icon", label=label, bbox=box, interactable=True, score=score, source="yolo")
        )
    for i, (box, text, conf) in enumerate(texts):
        if i not in used_text:
            elements.append(
                UIElement(
                    id=0, kind="text", label=text, bbox=box, interactable=True, score=conf, source="ocr"
                )
            )
    for i, e in enumerate(elements):
        e.id = i
    return elements


class OmniParserV2:
    name = "omniparser-v2"

    def __init__(
        self,
        weights_dir: Path = Path("weights"),
        device: str = "auto",
        caption_icons: bool = True,
        box_threshold: float = 0.05,
        iou_threshold: float = 0.1,
        imgsz: int = 1280,
        ocr: str = "auto",
    ):
        self.device = _device(device)
        self.box_threshold = box_threshold
        self.iou_threshold = iou_threshold
        self.imgsz = imgsz
        root = self._ensure_weights(Path(weights_dir) / "omniparser-v2")

        from ultralytics import YOLO

        t0 = time.time()
        self._yolo = YOLO(str(root / "icon_detect" / "model.pt"))
        self._ocr = make_ocr(ocr)
        self._caption = _FlorenceCaptioner(root / "icon_caption", self.device) if caption_icons else None
        log.info("OmniParser v2 loaded on %s in %.1fs", self.device, time.time() - t0)

    @staticmethod
    def _ensure_weights(root: Path) -> Path:
        if (root / "icon_detect" / "model.pt").exists():
            return root
        from huggingface_hub import snapshot_download

        log.info("downloading %s weights to %s", OMNIPARSER_REPO, root)
        snapshot_download(OMNIPARSER_REPO, allow_patterns=["icon_detect/*", "icon_caption/*"], local_dir=root)
        return root

    def parse(self, image: Image.Image) -> list[UIElement]:
        image = image.convert("RGB")
        w, h = image.size
        res = self._yolo.predict(
            source=image,
            conf=self.box_threshold,
            iou=self.iou_threshold,
            imgsz=self.imgsz,
            device=self.device if self.device != "mps" else "mps",
            verbose=False,
        )[0]
        icons: list[tuple[Box, float]] = []
        for xyxy, conf in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist()):
            x0, y0, x1, y1 = (round(v) for v in xyxy)
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
            if x1 - x0 >= 4 and y1 - y0 >= 4:
                icons.append(((x0, y0, x1, y1), float(conf)))
        texts = self._ocr.read(image)
        elements = merge_boxes(icons, texts)

        if self._caption is not None:
            todo = [e for e in elements if e.kind == "icon" and not e.label]
            if todo:
                for e, cap in zip(todo, self._caption.caption(image, [e.bbox for e in todo])):
                    e.label = cap
                    e.source = "yolo+caption"
        for e in elements:
            if not e.label:
                e.label = "unlabeled icon"
        return elements


class _FlorenceCaptioner:
    """OmniParser's fine-tuned Florence-2 icon captioner."""

    def __init__(self, path: Path, device: str, batch_size: int = 64, cache_size: int = 4096):
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor

        self.device = device
        self.batch_size = batch_size
        self.dtype = torch.float16 if device in ("cuda", "mps") else torch.float32
        with _no_flash_attn():
            self.processor = AutoProcessor.from_pretrained(FLORENCE_PROCESSOR_REPO, trust_remote_code=True)
            self.model = AutoModelForCausalLM.from_pretrained(
                str(path), torch_dtype=self.dtype, trust_remote_code=True
            ).to(device)
        self.model.eval()
        # The same icons (taskbar, toolbars) appear on every screen; captioning is the slowest
        # stage (~0.2 s per icon on MPS), so captions are cached by crop content.
        self._cache: OrderedDict[bytes, str] = OrderedDict()
        self._cache_size = cache_size

    def caption(self, image: Image.Image, boxes: list[Box]) -> list[str]:
        crops = [image.crop(b).resize((64, 64)) for b in boxes]
        keys = [hashlib.blake2b(c.tobytes(), digest_size=16).digest() for c in crops]
        missing = [i for i, k in enumerate(keys) if k not in self._cache]
        if missing:
            for i, cap in zip(missing, self._generate([crops[i] for i in missing])):
                self._cache[keys[i]] = cap
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        out = []
        for k in keys:
            self._cache.move_to_end(k)
            out.append(self._cache[k])
        return out

    def _generate(self, crops: list[Image.Image]) -> list[str]:
        import torch

        out: list[str] = []
        for i in range(0, len(crops), self.batch_size):
            batch = crops[i : i + self.batch_size]
            inputs = self.processor(images=batch, text=["<CAPTION>"] * len(batch), return_tensors="pt")
            inputs = {k: (v.to(self.device, self.dtype) if v.is_floating_point() else v.to(self.device))
                      for k, v in inputs.items()}  # fmt: skip
            with torch.inference_mode():
                ids = self.model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=20,
                    num_beams=1,
                    do_sample=False,
                )
            out.extend(t.strip() for t in self.processor.batch_decode(ids, skip_special_tokens=True))
        return out


class _no_flash_attn:
    """Florence-2's remote code imports flash_attn, which does not exist on Mac/CPU; hide that import."""

    def __enter__(self):
        from unittest import mock

        from transformers.dynamic_module_utils import get_imports

        def fixed(filename):
            imports = get_imports(filename)
            return [i for i in imports if i != "flash_attn"]

        self._patch = mock.patch("transformers.dynamic_module_utils.get_imports", fixed)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def make_parser(
    kind: str, weights_dir: Path, device: str = "auto", caption_icons: bool = True, ocr: str = "auto"
) -> UIParser:
    if kind == "omniparser":
        return OmniParserV2(weights_dir=weights_dir, device=device, caption_icons=caption_icons, ocr=ocr)
    if kind == "ocr":
        return OcrOnlyParser(ocr)
    if kind == "null":
        return NullParser()
    raise ValueError(f"unknown parser {kind!r}")

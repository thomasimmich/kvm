"""A fast, rough description of the whole screen from an image-to-text model.

It runs next to OmniParser, not before it, so it never delays a decision. The
text is for people following a run (read out by the remote control), not for
Jev. Any OpenAI-compatible chat-completions endpoint works.
"""

from __future__ import annotations

import base64
import io
import logging

import httpx
from PIL import Image

log = logging.getLogger(__name__)

PROMPT = (
    "This is a screenshot of a remote computer that an automated agent is operating. "
    "Describe for a blind person in 2–3 short sentences what is on the screen: which application "
    "or page is open, the main areas, and any open menu, dialog or message. "
    "Do not list every element and do not guess what the agent will do."
)


def jpeg_data_url(image: Image.Image, max_width: int = 1024, quality: int = 75) -> str:
    img = image.convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


class VisionDescriber:
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4.1-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 20.0,
        prompt: str = PROMPT,
        client: httpx.Client | None = None,
    ):
        self.model = model
        self.prompt = prompt
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.client = client or httpx.Client(timeout=timeout_s)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def describe(self, image: Image.Image) -> str:
        body = {
            "model": self.model,
            "max_tokens": 200,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
                        {"type": "image_url", "image_url": {"url": jpeg_data_url(image), "detail": "low"}},
                    ],
                }
            ],
        }
        r = self.client.post(self.url, json=body, headers=self.headers)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()

    __call__ = describe


def make_describer(provider: str, api_key: str, model: str, base_url: str) -> VisionDescriber | None:
    if provider == "none":
        return None
    if not api_key:
        log.info("no OPENAI_API_KEY: screens are not described (set CUA_VISION_PROVIDER=none to silence)")
        return None
    return VisionDescriber(api_key, model, base_url)

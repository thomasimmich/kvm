"""TypeSafe Jev client — a Python port of leanscope-agent-server/src/decision/typesafe-jev-client.ts.

Jev is a decision-only model: it answers typed questions about a state with
probabilities and never generates text.

    POST {base}/v1/systemone   Authorization: Bearer <TYPESAFE_API_KEY>
    { model, state, questions }  →  { model, answers, usage: { input_tokens, output_tokens } }

Question types: noul (yes/no), choice (≤255 options, each description ≤255
chars), score (2–10 ordered levels).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

CRITERIA_MAX_CHARS = 255
CHOICE_MAX_OPTIONS = 255
DEFAULT_BASE_URL = "https://api.typesafe.ai"
_RETRYABLE = {429, 529}


class JevError(RuntimeError):
    pass


class JevCredentialError(JevError):
    def __init__(self) -> None:
        super().__init__(
            "TypeSafe rejected the API key (HTTP 401) — check TYPESAFE_API_KEY in the environment"
        )


class JevRequestError(JevError):
    """HTTP 422: Jev's validation message is the most useful text about a malformed question."""

    def __init__(self, body: str) -> None:
        super().__init__(f"TypeSafe rejected the request (HTTP 422): {body}")
        self.body = body


class JevTransportError(JevError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"TypeSafe request failed (HTTP {status}): {body[:500]}")
        self.status = status
        self.body = body


@dataclass
class JevResult:
    model: str
    answers: dict[str, dict[str, Any]]
    input_tokens: int = 0
    output_tokens: int = 0


def noul(instructions: str, true: str | None = None, false: str | None = None) -> dict:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true is not None and false is not None:
        q["criteria"] = {"true": true, "false": false}
    return q


def choice(instructions: str, options: dict[str, str | None]) -> dict:
    if not 2 <= len(options) <= CHOICE_MAX_OPTIONS:
        raise ValueError(f"a choice needs 2..{CHOICE_MAX_OPTIONS} options, got {len(options)}")
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def truncate_choice_criteria(questions: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for key, q in questions.items():
        if q.get("type") != "choice":
            out[key] = q
            continue
        crit = {
            k: (v[:CRITERIA_MAX_CHARS] if isinstance(v, str) and len(v) > CRITERIA_MAX_CHARS else v)
            for k, v in q["criteria"].items()
        }
        out[key] = {**q, "criteria": crit}
    return out


class JevClient:
    def __init__(
        self,
        api_key: str,
        model: str = "jev-latest",
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 3,
        base_delay_s: float = 0.5,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise JevCredentialError()
        self.model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout_s,
            transport=transport,
        )
        self._max_retries = max_retries
        self._base_delay_s = base_delay_s
        self._sleep = sleep

    def decide(self, state: str | dict | list, questions: dict[str, dict]) -> JevResult:
        body = {"model": self.model, "state": state, "questions": truncate_choice_criteria(questions)}
        r = self._client.post("/v1/systemone", json=body)
        attempt = 0
        while r.status_code in _RETRYABLE and attempt < self._max_retries:
            self._sleep(self._base_delay_s * 2**attempt)
            attempt += 1
            r = self._client.post("/v1/systemone", json=body)
        if r.status_code == 401:
            raise JevCredentialError()
        if r.status_code == 422:
            raise JevRequestError(r.text)
        if r.status_code // 100 != 2:
            raise JevTransportError(r.status_code, r.text)
        try:
            data = r.json()
        except ValueError as e:
            raise JevTransportError(r.status_code, r.text) from e
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict) or not all(
            isinstance(a, dict) and a.get("type") in ("noul", "choice", "score") for a in answers.values()
        ):
            raise JevTransportError(r.status_code, r.text)
        usage = data.get("usage") or {}
        return JevResult(
            model=data.get("model", self.model),
            answers=answers,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )

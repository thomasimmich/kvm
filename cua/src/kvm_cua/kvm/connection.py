"""Where the JetKVM is and its CUA API token.

KVM_URL and KVM_TOKEN in .env are the defaults. What is entered in the dashboard wins and is kept
in a small git-ignored file, so a new device or a new token needs no edit of .env and no restart.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .device import NO_SIGNAL, KvmError

log = logging.getLogger(__name__)

# One line for the JetKVM web UI's browser console: turns the CUA API on (keeps an existing token)
# and shows the token preselected in a dialog, ready for Cmd+C. Not the console's `copy` helper: it
# is gone by the time the answer arrives, and the page's clipboard API refuses while DevTools has focus.
TOKEN_SNIPPET = (
    'window.__kvmTestHooks.sendJsonRpc("setCuaApiConfig", { enabled: true, regenerateToken: false }, '
    '(r) => r.result ? prompt("CUA token: copy it (Cmd+C / Ctrl+C) and paste it into the dashboard", r.result.token) '
    ": console.error(r))"
)


def normalize_kvm_url(url: str) -> str:
    """'192.168.1.50', 'http://192.168.1.50/', 'http://192.168.1.50/api/cua' → 'http://192.168.1.50'."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


class KvmConnection:
    def __init__(self, state_file: Path | None, default_url: str = "", default_token: str = ""):
        self.state_file = state_file
        self.url = normalize_kvm_url(default_url)
        self.token = default_token.strip()
        self.source = ".env"
        self._load()

    def _load(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError) as e:
            log.warning("could not read %s: %s", self.state_file, e)
            return
        url, token = normalize_kvm_url(str(data.get("url") or "")), str(data.get("token") or "").strip()
        if url or token:
            self.url, self.token, self.source = url or self.url, token or self.token, "dashboard"

    def save(self, url: str, token: str) -> None:
        self.url, self.token, self.source = url, token, "dashboard"
        if self.state_file is None:
            return
        tmp = self.state_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds the token
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"url": url, "token": token}, f, indent=2)
        os.replace(tmp, self.state_file)

    def snapshot(self) -> dict:
        return {
            "url": self.url,
            "tokenSet": bool(self.token),
            "tokenEnd": self.token[-4:] if len(self.token) >= 12 else "",
            "source": self.source,
            "stateFile": str(self.state_file or ""),
        }


def explain_kvm_error(e: Exception, url: str) -> tuple[str, str]:
    """(kind, text) for the dashboard. kind: network, api_off, token, firmware, signal or other.
    'signal' means the device answered fine and only the target computer shows no picture."""
    if isinstance(e, httpx.TransportError):
        return "network", (
            f"no answer from {url or 'the LeanScope dongle'}: check the IP address on the dongle's display "
            "and that this computer is on the same network"
        )
    text = str(e)
    if isinstance(e, KvmError):
        if text.startswith(NO_SIGNAL):
            return "signal", text
        if "CUA API is disabled" in text:
            return (
                "api_off",
                "the CUA API is turned off on the LeanScope dongle: run the token line in its web UI's console",
            )
        if "HTTP 401" in text:
            return (
                "token",
                "the LeanScope dongle rejected the token: copy it again with the token line and paste it here",
            )
        if "KVM_TOKEN is empty" in text:
            return "token", "no token yet: get it with the token line in the dongle's web UI"
        if "web page instead of the CUA API" in text:
            return "firmware", text
    return "other", text

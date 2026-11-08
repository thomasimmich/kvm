"""The LeanScope agent server's REST API, as far as the CUA uses it.

- start a session of the narration choreo (run-choreo plugin route, then /start)
- upload screenshots into that session (/sessions/:token/files)
- report into the activity the engine bound to the CUA's worker step (/activities/:guid)

Everything is REST with the session token or the activity URL; no GraphQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)


@dataclass
class SessionHandle:
    session_token: str
    session_scenario_guid: str = ""
    session_id: str = ""
    ui_url: str = ""
    controller_url: str = ""


class AgentServerError(RuntimeError):
    pass


class AgentServerClient:
    def __init__(self, base_url: str, api_token: str = "", timeout_s: float = 15.0,
                 client: httpx.Client | None = None):  # fmt: skip
        self.base_url = base_url.rstrip("/") + "/"
        self.api_token = api_token
        self.client = client or httpx.Client(timeout=timeout_s)

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def absolute(self, url: str) -> str:
        """Uploaded files come back controller-relative (/api/v1/files/…)."""
        return url if "://" in url else self._url(url)

    def _check(self, r: httpx.Response, what: str) -> httpx.Response:
        if r.status_code >= 400:
            raise AgentServerError(f"{what}: HTTP {r.status_code} {r.text[:300]}")
        return r

    # --- session ---------------------------------------------------------------

    def run_choreo(self, repository_id: int | str, scenario_guid: str, initial_input: dict | None = None,
                   session_repository_id: int | str | None = None) -> SessionHandle:  # fmt: skip
        body: dict = {**(initial_input or {}), "repositoryId": repository_id, "scenarioGuid": scenario_guid}
        if session_repository_id:
            body["sessionRepositoryId"] = session_repository_id
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
        r = self._check(
            self.client.post(self._url("/api/v1/plugins/run-choreo"), json=body, headers=headers),
            "run-choreo",
        )
        s = r.json().get("session") or {}
        if not s.get("sessionToken"):
            raise AgentServerError(f"run-choreo returned no session token: {r.text[:300]}")
        return SessionHandle(
            session_token=s["sessionToken"],
            session_scenario_guid=s.get("sessionScenarioGuid", ""),
            session_id=s.get("sessionId", ""),
            ui_url=s.get("uiUrl") or r.json().get("url", ""),
            controller_url=s.get("controllerUrl", ""),
        )

    def start(self, session_token: str) -> None:
        self._check(
            self.client.post(self._url(f"/api/v1/sessions/{session_token}/start"), json={}), "start session"
        )

    # --- files and activity ----------------------------------------------------

    def upload_file(self, session_token: str, data: bytes, filename: str, mime: str) -> str:
        """Stores a file in the session; returns its absolute URL."""
        r = self._check(
            self.client.post(
                self._url(f"/api/v1/sessions/{session_token}/files"),
                files={"file": (filename, data, mime)},
            ),
            "upload file",
        )
        return self.absolute(r.json()["url"])

    def patch_activity(self, activity_url: str, body: dict) -> None:
        self._check(self.client.patch(activity_url, json=body), "patch activity")

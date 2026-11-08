"""LeanScope login, and the repositories and choreos the logged-in user can pick.

- Login is the Supabase password grant that LeanScope uses. Its access token is the login JWT
  the agent server needs to create a session (run-choreo).
- The JWT lives about an hour. The refresh token renews it, and it is stored in the state file
  (CUA_STATE_FILE) together with the dashboard's choice of choreo, so a restart keeps both.
  The password and the access token are never written to disk.
- Repositories and choreo scenarios (with the goal and hints they prefill in the dashboard) come
  from the LeanScope GraphQL API, with the same JWT.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

REFRESH_MARGIN_S = 60.0


class LeanScopeError(RuntimeError):
    pass


@dataclass
class ChoreoSelection:
    """Which choreo narrates a run, and where its sessions are recorded."""

    repository_id: str = ""
    scenario_guid: str = ""
    scenario_title: str = ""
    session_repository_id: str = ""  # empty: the user's account repository

    @property
    def complete(self) -> bool:
        return bool(self.repository_id and self.scenario_guid)

    def to_json(self) -> dict:
        return {
            "repositoryId": self.repository_id,
            "scenarioGuid": self.scenario_guid,
            "scenarioTitle": self.scenario_title,
            "sessionRepositoryId": self.session_repository_id,
        }


class LeanScopeAuth:
    def __init__(self, supabase_url: str, anon_key: str, state_file: Path | None = None,
                 client: httpx.Client | None = None, timeout_s: float = 15.0):  # fmt: skip
        self.supabase_url = supabase_url.rstrip("/")
        self.anon_key = anon_key
        self.state_file = state_file
        self.client = client or httpx.Client(timeout=timeout_s)
        self._lock = threading.Lock()
        self.email = ""
        self._refresh_token = ""
        self._access_token = ""
        self._expires_at = 0.0
        self.selection = ChoreoSelection()
        self._load()

    @property
    def logged_in(self) -> bool:
        return bool(self._refresh_token)

    # --- login --------------------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        email = email.strip()
        if not email or not password:
            raise LeanScopeError("email and password are required")
        data = self._token_request("password", {"email": email, "password": password})
        with self._lock:
            if email.lower() != self.email.lower():
                self.selection = ChoreoSelection()  # another user may not reach the old choice
            self.email = email
            self._take(data)
            self._save()

    def logout(self) -> None:
        with self._lock:
            self._refresh_token = self._access_token = ""
            self._expires_at = 0.0
            self._save()

    def access_token(self) -> str:
        """A JWT with at least a minute left; refreshes it when needed."""
        with self._lock:
            if not self._refresh_token:
                raise LeanScopeError("not logged in to LeanScope: log in in the CUA dashboard")
            if self._access_token and time.time() < self._expires_at - REFRESH_MARGIN_S:
                return self._access_token
            try:
                data = self._token_request("refresh_token", {"refresh_token": self._refresh_token})
            except LeanScopeError as e:
                if getattr(e, "status", 0) in (400, 401, 403):  # revoked or expired: log in again
                    self._refresh_token = self._access_token = ""
                    self._save()
                    raise LeanScopeError(
                        "the LeanScope login expired: log in again in the CUA dashboard"
                    ) from e
                raise
            self._take(data)  # the refresh token rotates: keep the new one
            self._save()
            return self._access_token

    def set_selection(self, selection: ChoreoSelection) -> None:
        with self._lock:
            self.selection = selection
            self._save()

    def _token_request(self, grant: str, body: dict) -> dict:
        try:
            r = self.client.post(
                f"{self.supabase_url}/auth/v1/token",
                params={"grant_type": grant},
                json=body,
                headers={"apikey": self.anon_key, "Authorization": f"Bearer {self.anon_key}"},
            )
        except httpx.HTTPError as e:
            raise LeanScopeError(f"LeanScope login service unreachable: {e}") from e
        if r.status_code >= 400:
            try:
                j = r.json()
                msg = (
                    j.get("msg") or j.get("error_description") or j.get("message") or j.get("error") or r.text
                )
            except ValueError:
                msg = r.text
            err = LeanScopeError(f"LeanScope login failed: {str(msg)[:200]}")
            err.status = r.status_code  # type: ignore[attr-defined]
            raise err
        data = r.json()
        if not data.get("access_token") or not data.get("refresh_token"):
            raise LeanScopeError("LeanScope login returned no token")
        return data

    def _take(self, data: dict) -> None:
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._expires_at = time.time() + float(data.get("expires_in") or 3600)

    # --- state file ---------------------------------------------------------------

    def _load(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            j = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable %s: %s", self.state_file, e)
            return
        self.email = j.get("email") or ""
        self._refresh_token = j.get("refreshToken") or ""
        sel = j.get("selection") or {}
        self.selection = ChoreoSelection(
            **{f.name: str(sel.get(f.name) or "") for f in fields(ChoreoSelection)}
        )

    def _save(self) -> None:
        if self.state_file is None:
            return
        data = {"email": self.email, "refreshToken": self._refresh_token, "selection": asdict(self.selection)}
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_file)


class LeanScopeCatalog:
    """What the logged-in user can pick: repositories and the choreo scenarios in them."""

    def __init__(self, api_url: str, auth: LeanScopeAuth, client: httpx.Client | None = None,
                 timeout_s: float = 20.0):  # fmt: skip
        self.graphql_url = api_url.rstrip("/") + "/graphql"
        self.auth = auth
        self.client = client or httpx.Client(timeout=timeout_s)

    def _query(self, query: str, variables: dict | None = None) -> dict:
        try:
            r = self.client.post(
                self.graphql_url,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {self.auth.access_token()}"},
            )
        except httpx.HTTPError as e:
            raise LeanScopeError(f"LeanScope API unreachable: {e}") from e
        try:
            j = r.json()
        except ValueError:
            j = {}
        if j.get("errors"):
            raise LeanScopeError(f"LeanScope API: {j['errors'][0].get('message', j['errors'][0])}")
        if r.status_code >= 400 or not isinstance(j.get("data"), dict):
            raise LeanScopeError(f"LeanScope API: HTTP {r.status_code} {r.text[:200]}")
        return j["data"]

    def repositories(self) -> list[dict]:
        repos: dict[int, dict] = {}
        for r in self._query("query { repositories { id name groupName } }").get("repositories") or []:
            if r:
                repos[r["id"]] = r
        # separate request: this resolver creates the account repository on first read and can fail alone
        try:
            acc = self._query("query { accountRepository { id name groupName } }").get("accountRepository")
            if acc:
                repos[acc["id"]] = {**acc, "account": True}
        except LeanScopeError as e:
            log.info("account repository not available: %s", e)
        out = [{"id": str(r["id"]), "name": r.get("name") or "", "groupName": r.get("groupName") or "",
                "account": bool(r.get("account"))} for r in repos.values()]  # fmt: skip
        out.sort(key=lambda r: (not r["account"], r["groupName"].lower(), r["name"].lower()))
        return out

    def choreos(self, repository_id: str) -> list[dict]:
        try:
            rid = int(repository_id)
        except ValueError as e:
            raise LeanScopeError(f"not a repository id: {repository_id!r}") from e
        data = self._query(
            "query($repositoryId: Int!) { uxmData(repositoryId: $repositoryId) "
            "{ scenarios { _guid title type personaGoal preconditions } } }",
            {"repositoryId": rid},
        )
        scenarios = ((data.get("uxmData") or {}).get("scenarios")) or []
        # personaGoal prefills the dashboard's goal, preconditions its hints (one per line)
        out = [{"guid": s["_guid"], "title": s.get("title") or "(untitled)",
                "goal": (s.get("personaGoal") or "").strip(),
                "hints": [h.strip() for h in (s.get("preconditions") or "").splitlines() if h.strip()]}
               for s in scenarios if s and s.get("type") == "Choreo"]  # fmt: skip
        out.sort(key=lambda s: s["title"].lower())
        return out

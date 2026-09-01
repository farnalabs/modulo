"""GiteaConnector — async Gitea API connector for self-hosted Gitea instances."""

import base64
from typing import Any

import httpx

from modulo.connectors._safe_page import safe_records_list as _safe_records_list
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)
from modulo.core.ssrf import pinned_async_client_sync

REQUIRED_SCOPES = frozenset({"read:user", "read:repository", "write:repository"})


class GiteaConnector(ConnectorBase):
    """Read/write Gitea via the REST API v1.

    Gitea is a self-hosted Git service. The ``base_url`` must point to the
    Gitea instance root (e.g. ``https://gitea.example.com``). The API is
    at ``/api/v1`` relative to the base URL.

    Supported query resources:
      "repos"   — list repositories accessible to the token
      "file"    — read a file; filters: {"repo": "owner/repo", "path": "...", "ref": "main"}
      "pulls"   — list pull requests; filters: {"repo": "owner/repo", "state": "open"}
      "issues"  — list issues; filters: {"repo": "owner/repo", "state": "open"}

    Supported write resources:
      "file"    — create/update a file; data: {"repo": ..., "path": ..., "content": ...,
                 "message": ..., "sha": <required for update>}
      "pull"    — create a pull request; data: {"repo": ..., "title": ...,
                 "head": ..., "base": ..., "body": ...}
      "issue"   — create an issue; data: {"repo": ..., "title": ..., "body": ...,
                 "assignees": ..., "labels": ...}
    """

    def __init__(self, token: str, base_url: str = "https://codeberg.org") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITEA

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-520): validate + resolve the base_url's host
        # synchronously and pin the validated IP onto the transport, so the
        # connection never re-resolves the host at connect time (closes the
        # DNS-rebinding window). ``trust_env=False`` stops a proxy from
        # re-resolving the destination server-side and defeating the pin. The
        # validated host is ``self._base_url``; the API client base_url is the
        # same host with an ``/api/v1`` path suffix.
        return pinned_async_client_sync(
            self._base_url,
            base_url=f"{self._base_url}/api/v1",
            headers=self._headers(),
            timeout=30,
        )

    async def _get_missing_scopes(self) -> frozenset[str]:
        """Check the token's scopes against required scopes.

        Gitea's /api/v1/user endpoint doesn't return scope info in headers,
        so we infer missing scopes by testing specific endpoints.
        """
        try:
            async with self._client() as client:
                r = await client.get("/repos", params={"limit": 1})

            if r.status_code == 401:
                return REQUIRED_SCOPES

            if r.status_code == 403:
                return REQUIRED_SCOPES - {"read:repository"}

            return frozenset()
        except httpx.HTTPStatusError:
            return REQUIRED_SCOPES
        except (httpx.TimeoutException, httpx.ConnectError):
            return REQUIRED_SCOPES

    async def health_check(self) -> HealthResult:
        """Check API access by fetching the authenticated user."""
        try:
            async with self._client() as client:
                r = await client.get("/user")

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            user_info = r.json()
            username = user_info.get("login", "")
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Gitea API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Gitea API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Gitea API connection error")
        except ValueError as exc:
            return health_check_failure(exc)

        missing = await self._get_missing_scopes()
        if missing:
            return HealthResult(
                ok=False,
                detail=f"Missing scopes: {', '.join(sorted(missing))}. Required: {', '.join(sorted(REQUIRED_SCOPES))}",
            )

        return HealthResult(ok=True, detail=username)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "repos":
                    r = await client.get("/user/repos", params={"limit": q.limit})
                    r.raise_for_status()
                    data = _safe_records_list(r.json())
                    return ConnectorResult(records=data, total=len(data))
                case "file":
                    owner_repo = q.filters["repo"]
                    path = q.filters["path"]
                    ref = q.filters.get("ref", "main")
                    r = await client.get(
                        f"/repos/{owner_repo}/contents/{path}",
                        params={"ref": ref},
                    )
                    r.raise_for_status()
                    info: dict[str, Any] = r.json()
                    # Gitea returns content as base64-encoded string
                    if "content" in info and info.get("encoding") == "base64":
                        info["content"] = base64.b64decode(info["content"]).decode("utf-8")
                    return ConnectorResult(records=[info])
                case "pulls":
                    owner_repo = q.filters["repo"]
                    state = q.filters.get("state", "open")
                    r = await client.get(
                        f"/repos/{owner_repo}/pulls",
                        params={"state": state, "limit": q.limit},
                    )
                    r.raise_for_status()
                    prs = _safe_records_list(r.json())
                    return ConnectorResult(records=prs, total=len(prs))
                case "issues":
                    owner_repo = q.filters["repo"]
                    state = q.filters.get("state", "open")
                    r = await client.get(
                        f"/repos/{owner_repo}/issues",
                        params={"state": state, "limit": q.limit},
                    )
                    r.raise_for_status()
                    issues = _safe_records_list(r.json())
                    return ConnectorResult(records=issues, total=len(issues))
                case _:
                    raise ValueError(f"Unsupported Gitea resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "file":
                    owner_repo = payload.data["repo"]
                    path = payload.data["path"]
                    body: dict[str, Any] = {
                        "content": payload.data["content"],
                        "message": payload.data.get("message", "Update via Modulo"),
                    }
                    if payload.data.get("sha"):
                        body["sha"] = payload.data["sha"]
                    # Gitea uses PUT for create/update of file contents
                    r = await client.put(
                        f"/repos/{owner_repo}/contents/{path}",
                        json=body,
                    )
                    r.raise_for_status()
                    result: dict[str, Any] = r.json()
                    return result
                case "pull":
                    owner_repo = payload.data["repo"]
                    body = {
                        "title": payload.data["title"],
                        "head": payload.data["head"],
                        "base": payload.data.get("base", "main"),
                    }
                    if "body" in payload.data:
                        body["body"] = payload.data["body"]
                    r = await client.post(
                        f"/repos/{owner_repo}/pulls",
                        json=body,
                    )
                    r.raise_for_status()
                    pr: dict[str, Any] = r.json()
                    return pr
                case "issue":
                    owner_repo = payload.data["repo"]
                    body = {
                        "title": payload.data["title"],
                    }
                    if "body" in payload.data:
                        body["body"] = payload.data["body"]
                    if "assignees" in payload.data:
                        body["assignees"] = payload.data["assignees"]
                    if "labels" in payload.data:
                        body["labels"] = payload.data["labels"]
                    r = await client.post(
                        f"/repos/{owner_repo}/issues",
                        json=body,
                    )
                    r.raise_for_status()
                    issue: dict[str, Any] = r.json()
                    return issue
                case _:
                    raise ValueError(f"Unsupported Gitea write resource: {payload.resource!r}")

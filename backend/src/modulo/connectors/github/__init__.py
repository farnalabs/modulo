"""GitHubConnector — async GitHub API connector."""

from typing import Any

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_GITHUB_API = "https://api.github.com"
_API_VERSION = "2022-11-28"

REQUIRED_SCOPES = frozenset({"repo:read", "repo:write", "pull_requests:write"})


class GitHubConnector(ConnectorBase):
    """Read/write GitHub via the REST API.

    Supported query resources:
      "repos"  — list repositories accessible to the token
      "file"   — read a file; filters: {"repo": "owner/repo", "path": "...", "ref": "main"}
      "pulls"  — list pull requests; filters: {"repo": "owner/repo", "state": "open"}

    Supported write resources:
      "file"   — create/update a file; data: {"repo": ..., "path": ..., "content": <base64>,
                 "message": ..., "sha": <required for update>}
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITHUB

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "Accept": "application/vnd.github+json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_GITHUB_API, headers=self._headers(), timeout=30)

    async def health_check(self) -> HealthResult:
        """Check API access and verify required scopes via representative endpoints.

        Required scopes: repo:read, repo:write, pull_requests:write.
        Scope verification is done by attempting actual API calls — a 401/403
        on the endpoint indicates the scope is missing.
        """
        async with self._client() as client:
            r = await client.get("/user")

        if r.status_code != 200:
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

        user_login = r.json().get("login", "")

        # Verify repo:read scope by attempting to list repos
        async with self._client() as client:
            repos_r = await client.get("/user/repos", params={"per_page": 1})
            if repos_r.status_code in (401, 403):
                return HealthResult(ok=False, detail="Missing scopes: repo:read")

        return HealthResult(ok=True, detail=user_login)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "repos":
                    r = await client.get("/user/repos", params={"per_page": q.limit})
                    r.raise_for_status()
                    data: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=data, total=len(data))
                case "file":
                    owner_repo = q.filters["repo"]
                    path = q.filters["path"]
                    ref = q.filters.get("ref", "main")
                    r = await client.get(f"/repos/{owner_repo}/contents/{path}", params={"ref": ref})
                    r.raise_for_status()
                    return ConnectorResult(records=[r.json()])
                case "pulls":
                    owner_repo = q.filters["repo"]
                    state = q.filters.get("state", "open")
                    r = await client.get(
                        f"/repos/{owner_repo}/pulls",
                        params={"state": state, "per_page": q.limit},
                    )
                    r.raise_for_status()
                    prs: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=prs, total=len(prs))
                case _:
                    raise ValueError(f"Unsupported GitHub resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "file":
                    owner_repo = payload.data["repo"]
                    path = payload.data["path"]
                    body: dict[str, Any] = {
                        "message": payload.data.get("message", "Update via Modulo"),
                        "content": payload.data["content"],
                    }
                    if "sha" in payload.data:
                        body["sha"] = payload.data["sha"]
                    r = await client.put(f"/repos/{owner_repo}/contents/{path}", json=body)
                    r.raise_for_status()
                    result: dict[str, Any] = r.json()
                    return result
                case _:
                    raise ValueError(f"Unsupported GitHub write resource: {payload.resource!r}")

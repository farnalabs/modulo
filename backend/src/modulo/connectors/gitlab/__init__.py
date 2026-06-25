"""GitLabConnector — async GitLab API connector via REST API v4."""

import base64
from typing import Any
from urllib.parse import quote

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_GITLAB_API = "https://gitlab.com/api/v4"

REQUIRED_SCOPES = frozenset({"read_api", "write_repository", "api"})


def _project_path(project_id: str) -> str:
    """URL-encode a project path like 'group/subgroup/project'."""
    return quote(project_id, safe="")


class GitLabConnector(ConnectorBase):
    """Read/write GitLab via the REST API v4.

    Supported query resources:
      "projects"  — list projects accessible to the token
      "file"     — read a file; filters: {"project": "group/project", "path": "...", "ref": "main"}
      "mrs"      — list merge requests; filters: {"project": "group/project", "state": "opened"}

    Supported write resources:
      "file"     — create/update a file; data: {"project": ..., "path": ..., "content": ...,
                   "message": ..., "sha": <required for update>}
      "mr"       — create a merge request; data: {"project": ..., "title": ...,
                   "source_branch": ..., "target_branch": ..., "description": ...}
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITLAB

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_GITLAB_API, headers=self._headers(), timeout=30)

    async def health_check(self) -> HealthResult:
        """Check API access and verify required scopes.

        Required scopes: read_api, write_repository, api.
        Scope verification relies on the 401/403 response from endpoints.
        """
        async with self._client() as client:
            r = await client.get("/user")

        if r.status_code != 200:
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

        user_info = r.json()
        username = user_info.get("username", "")

        async with self._client() as client:
            projects_r = await client.get("/projects", params={"per_page": 1})
            if projects_r.status_code in (401, 403):
                return HealthResult(ok=False, detail="Missing scopes: API access not granted")

        return HealthResult(ok=True, detail=username)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "projects":
                    r = await client.get("/projects", params={"per_page": q.limit})
                    r.raise_for_status()
                    data: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=data, total=len(data))
                case "file":
                    project = q.filters["project"]
                    path = q.filters["path"]
                    ref = q.filters.get("ref", "main")
                    encoded = _project_path(project)
                    r = await client.get(
                        f"/projects/{encoded}/repository/files/{quote(path, safe='')}",
                        params={"ref": ref},
                    )
                    r.raise_for_status()
                    info: dict[str, Any] = r.json()
                    # GitLab returns content as base64-encoded string
                    if "content" in info:
                        info["content"] = base64.b64decode(info["content"]).decode("utf-8")
                    return ConnectorResult(records=[info])
                case "mrs":
                    project = q.filters["project"]
                    state = q.filters.get("state", "opened")
                    encoded = _project_path(project)
                    r = await client.get(
                        f"/projects/{encoded}/merge_requests",
                        params={"state": state, "per_page": q.limit},
                    )
                    r.raise_for_status()
                    mrs: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=mrs, total=len(mrs))
                case _:
                    raise ValueError(f"Unsupported GitLab resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "file":
                    project = payload.data["project"]
                    path = payload.data["path"]
                    encoded = _project_path(project)
                    body: dict[str, Any] = {
                        "content": payload.data["content"],
                        "commit_message": payload.data.get("message", "Update via Modulo"),
                    }
                    if payload.data.get("sha"):
                        body["sha"] = payload.data["sha"]
                    r = await client.put(
                        f"/projects/{encoded}/repository/files/{quote(path, safe='')}",
                        json=body,
                    )
                    r.raise_for_status()
                    result: dict[str, Any] = r.json()
                    return result
                case "mr":
                    project = payload.data["project"]
                    encoded = _project_path(project)
                    body = {
                        "source_branch": payload.data["source_branch"],
                        "target_branch": payload.data.get("target_branch", "main"),
                        "title": payload.data["title"],
                    }
                    if "description" in payload.data:
                        body["description"] = payload.data["description"]
                    r = await client.post(
                        f"/projects/{encoded}/merge_requests",
                        json=body,
                    )
                    r.raise_for_status()
                    mr: dict[str, Any] = r.json()
                    return mr
                case _:
                    raise ValueError(f"Unsupported GitLab write resource: {payload.resource!r}")

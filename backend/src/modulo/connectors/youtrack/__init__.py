"""YouTrackConnector — async YouTrack REST API connector."""

import asyncio
from typing import Any, cast

import httpx

from modulo._types import _DICT_STR_ANY
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)
from modulo.core.ssrf import validate_outbound_url


def _safe_top_level_records(body: Any) -> list[dict[str, Any]]:
    """Return *body* as a page list, or an empty page for a corrupt body.

    YouTrack's list endpoints (`/issues`, `/admin/projects`, `/users`) return a
    top-level JSON array. A corrupt or hostile response may place anything
    there — an object, a string, a number, ``null``. Such a body would crash
    downstream list iteration, so only an actual list is treated as records and
    everything else degrades to an empty page (mirrors the ``safe_records``
    hardening lens applied to the other connectors).
    """
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    return []


class YouTrackConnector(ConnectorBase):
    """Read/write YouTrack issues, projects, users via the REST API.

    Credentials (from credentials_ciphertext):
      "token"  — YouTrack Permanent Token

    Supported query resources:
      "issues"      — list issues; supports query, fields, skip, top filters
      "issue"       — get single issue; requires "issue_id" filter
      "projects"    — list projects (GET /admin/projects)
      "project"     — get single project; requires "project_id" filter
      "users"       — list users; supports query filter

    Supported write resources:
      "issue"           — create issue; data: {"summary": "...", ...}
      "issue_update"    — update issue; data: {"id": "...", ...}
      "comment"         — add comment; data: {"issue_id": "...", "text": "..."}
    """

    def __init__(self, token: str, base_url: str = "") -> None:
        self._token = token
        self._base_url = base_url

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.YOUTRACK

    def _client(self) -> httpx.AsyncClient:
        validate_outbound_url(self._base_url)
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify API connectivity by fetching current user."""
        try:
            async with self._client() as client:
                r = await client.get("/users/me")
                r.raise_for_status()
                body: dict[str, Any] = r.json()
                name = body.get("name") or body.get("login", "")
                return HealthResult(ok=True, detail=name)
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "issues":
                return await self._query_issues(q)
            case "issue":
                return await self._query_issue(q)
            case "projects":
                return await self._query_projects()
            case "project":
                return await self._query_project(q)
            case "users":
                return await self._query_users(q)
            case _:
                raise ValueError(f"Unsupported YouTrack query resource: {q.resource!r}")

    async def _query_issues(self, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if "query" in q.filters:
            params["query"] = q.filters["query"]
        if "fields" in q.filters:
            params["fields"] = q.filters["fields"]
        if "skip" in q.filters:
            params["$skip"] = q.filters["skip"]
        if "top" in q.filters:
            params["$top"] = q.filters["top"]
        if q.limit:
            params.setdefault("$top", q.limit)
        async with self._client() as client:
            r = await client.get("/issues", params=params)
            r.raise_for_status()
            records = _safe_top_level_records(r.json())
        return ConnectorResult(records=records, total=len(records))

    async def _query_issue(self, q: ConnectorQuery) -> ConnectorResult:
        issue_id = q.filters.get("issue_id")
        if not issue_id:
            raise ValueError("YouTrack issue query requires 'issue_id' filter")
        params: dict[str, Any] = {}
        if "fields" in q.filters:
            params["fields"] = q.filters["fields"]
        async with self._client() as client:
            r = await client.get(f"/issues/{issue_id}", params=params)
            r.raise_for_status()
            record: dict[str, Any] = r.json()
        return ConnectorResult(records=[record])

    async def _query_projects(self) -> ConnectorResult:
        async with self._client() as client:
            r = await client.get("/admin/projects")
            r.raise_for_status()
            records = _safe_top_level_records(r.json())
        return ConnectorResult(records=records, total=len(records))

    async def _query_project(self, q: ConnectorQuery) -> ConnectorResult:
        project_id = q.filters.get("project_id")
        if not project_id:
            raise ValueError("YouTrack project query requires 'project_id' filter")
        async with self._client() as client:
            r = await client.get(f"/admin/projects/{project_id}")
            r.raise_for_status()
            record = r.json()
        return ConnectorResult(records=[record])

    async def _query_users(self, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if "query" in q.filters:
            params["query"] = q.filters["query"]
        async with self._client() as client:
            r = await client.get("/users", params=params)
            r.raise_for_status()
            records = _safe_top_level_records(r.json())
        return ConnectorResult(records=records, total=len(records))

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "issue":
                async with self._client() as client:
                    r = await client.post("/issues", json=payload.data)
                    r.raise_for_status()
                    return cast(_DICT_STR_ANY, r.json())

            case "issue_update":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in issue_update payload")
                update_data = {k: v for k, v in payload.data.items() if k != "id"}
                async with self._client() as client:
                    r = await client.post(f"/issues/{issue_id}", json=update_data)
                    r.raise_for_status()
                    return cast(_DICT_STR_ANY, r.json())

            case "comment":
                issue_id = payload.data.get("issue_id")
                text = payload.data.get("text")
                if not issue_id or not text:
                    raise ValueError("comment requires 'issue_id' and 'text' in data")
                comment_data = {"text": text}
                async with self._client() as client:
                    r = await client.post(f"/issues/{issue_id}/comments", json=comment_data)
                    r.raise_for_status()
                    return cast(_DICT_STR_ANY, r.json())

            case _:
                raise ValueError(f"Unsupported YouTrack write resource: {payload.resource!r}")

"""AsanaConnector — async Asana REST API v1 connector."""

from typing import Any

import httpx

from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_ASANA_API = "https://app.asana.com/api/1.0"


class AsanaConnector(ConnectorBase):
    """Read/write Asana workspaces, projects, tasks, and sections via the REST API v1.

    Credentials (from credentials_ciphertext):
      "personal_access_token" — Asana Personal Access Token

    Supported query resources:
      "projects"   — list projects; filters: {"workspace": "..."}
      "project"    — get single project; filters: {"project_id": "..."}
      "tasks"      — list tasks in a project; filters: {"project_id": "..."}
      "task"       — get single task; filters: {"task_id": "..."}
      "sections"   — list sections in a project; filters: {"project_id": "..."}
      "workspaces" — list workspaces
      "users"      — list users; filters: {"workspace": "..."}

    Supported write resources:
      "task"         — create a task; data: {"name": "...", "projects": [...], ...}
      "task_update"  — update a task; data: {"id": "...", "name": "...", ...}
      "project"      — create a project; data: {"name": "...", ...}
      "section"      — create a section; data: {"project": "...", "name": "..."}
      "comment"      — add a comment to a task; data: {"task_id": "...", "text": "..."}
    """

    def __init__(self, personal_access_token: str) -> None:
        self._token = personal_access_token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.ASANA

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_ASANA_API,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the authenticated user's profile."""
        async with self._client() as client:
            r = await client.get("/users/me")

        if r.status_code != 200:
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

        body: dict[str, Any] = r.json()
        data = body.get("data", {})
        display_name = data.get("name", "")
        return HealthResult(ok=True, detail=display_name)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "projects":
                    return await self._query_projects(client, q)
                case "project":
                    return await self._query_project(client, q)
                case "tasks":
                    return await self._query_tasks(client, q)
                case "task":
                    return await self._query_task(client, q)
                case "sections":
                    return await self._query_sections(client, q)
                case "workspaces":
                    return await self._query_workspaces(client)
                case "users":
                    return await self._query_users(client, q)
                case _:
                    raise ValueError(f"Unsupported Asana resource: {q.resource!r}")

    async def _query_projects(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, str] = {}
        if "workspace" in q.filters:
            params["workspace"] = q.filters["workspace"]
        if "archived" in q.filters:
            params["archived"] = str(q.filters["archived"]).lower()
        r = await client.get("/projects", params=params)
        r.raise_for_status()
        return self._list_result(r.json())

    async def _query_project(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        project_id = q.filters.get("project_id")
        if not project_id:
            raise ValueError("Asana project query requires 'project_id' filter")
        r = await client.get(f"/projects/{project_id}")
        r.raise_for_status()
        return self._single_result(r.json())

    async def _query_tasks(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, str] = {}
        if "workspace" in q.filters:
            params["workspace"] = q.filters["workspace"]
        project_id = q.filters.get("project_id")
        if project_id:
            r = await client.get(f"/projects/{project_id}/tasks", params=params)
        elif "workspace" in q.filters:
            r = await client.get("/tasks", params=params)
        else:
            raise ValueError("Asana tasks query requires 'project_id' or 'workspace' filter")
        r.raise_for_status()
        return self._list_result(r.json())

    async def _query_task(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        task_id = q.filters.get("task_id")
        if not task_id:
            raise ValueError("Asana task query requires 'task_id' filter")
        r = await client.get(f"/tasks/{task_id}")
        r.raise_for_status()
        return self._single_result(r.json())

    async def _query_sections(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        project_id = q.filters.get("project_id")
        if not project_id:
            raise ValueError("Asana sections query requires 'project_id' filter")
        r = await client.get(f"/projects/{project_id}/sections")
        r.raise_for_status()
        return self._list_result(r.json())

    async def _query_workspaces(self, client: httpx.AsyncClient) -> ConnectorResult:
        r = await client.get("/workspaces")
        r.raise_for_status()
        return self._list_result(r.json())

    async def _query_users(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, str] = {}
        if "workspace" in q.filters:
            params["workspace"] = q.filters["workspace"]
        r = await client.get("/users", params=params)
        r.raise_for_status()
        return self._list_result(r.json())

    @staticmethod
    def _list_result(body: Any) -> ConnectorResult:
        records = _safe_records(body, "data")
        return ConnectorResult(records=records, total=len(records))

    @staticmethod
    def _single_result(body: Any) -> ConnectorResult:
        record = body.get("data", {}) if isinstance(body, dict) else {}
        return ConnectorResult(records=[record] if record else [])

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "task":
                    r = await client.post("/tasks", json={"data": payload.data})
                    r.raise_for_status()
                    body = r.json()
                    created: dict[str, Any] = body.get("data", {})
                    return created

                case "task_update":
                    task_id = payload.data.get("id")
                    if not task_id:
                        raise ValueError("Asana task_update requires 'id' in data")
                    r = await client.put(f"/tasks/{task_id}", json={"data": payload.data})
                    r.raise_for_status()
                    body = r.json()
                    updated: dict[str, Any] = body.get("data", {})
                    return updated

                case "project":
                    r = await client.post("/projects", json={"data": payload.data})
                    r.raise_for_status()
                    body = r.json()
                    created_project: dict[str, Any] = body.get("data", {})
                    return created_project

                case "section":
                    project_gid = payload.data.pop("project", None)
                    if not project_gid:
                        raise ValueError("Asana section requires 'project' in data")
                    params = {"project": project_gid}
                    r = await client.post(
                        "/sections",
                        params=params,
                        json={"data": payload.data},
                    )
                    r.raise_for_status()
                    body = r.json()
                    created_section: dict[str, Any] = body.get("data", {})
                    return created_section

                case "comment":
                    task_id = payload.data.get("task_id")
                    if not task_id:
                        raise ValueError("Asana comment requires 'task_id' in data")
                    text = payload.data.get("text", "")
                    r = await client.post(
                        f"/tasks/{task_id}/stories",
                        json={"data": {"text": text}},
                    )
                    r.raise_for_status()
                    body = r.json()
                    comment: dict[str, Any] = body.get("data", {})
                    return comment

                case _:
                    raise ValueError(f"Unsupported Asana write resource: {payload.resource!r}")

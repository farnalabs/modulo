"""ShortcutConnector — async Shortcut REST API v3 connector."""

import asyncio
from typing import Any

import httpx

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

_SHORTCUT_API = "https://api.app.shortcut.com/api/v3"

# singular query resource -> (plural resource, filter key, missing-id error message)
_SINGULAR_RESOURCES: dict[str, tuple[str, str, str]] = {
    "story": ("stories", "story_id", "Shortcut story query requires 'story_id' filter"),
    "project": ("projects", "project_id", "Shortcut project query requires 'project_id' filter"),
    "epic": ("epics", "epic_id", "Shortcut epic query requires 'epic_id' filter"),
}


class ShortcutConnector(ConnectorBase):
    """Read/write Shortcut stories, epics, projects via the REST API v3.

    Credentials (from credentials_ciphertext):
      "token"  — Shortcut API token

    Supported query resources:
      "stories"     — list stories; optional filters: project_id, workflow_state_id, owner_id
      "story"       — get single story; requires "story_id" filter
      "projects"    — list projects; optional filter: "suspended"
      "project"     — get single project; requires "project_id" filter
      "epics"       — list epics; optional filter: "suspended"
      "epic"        — get single epic; requires "epic_id" filter
      "workflows"   — list all workflows
      "members"     — list all members
      "teams"       — list all teams

    Supported write resources:
      "story"           — create story; data: {"name": "...", ...}
      "story_update"    — update story; data: {"id": "...", ...}
      "story_comment"   — add comment; data: {"story_id": "...", "text": "..."}
      "epic"            — create epic; data: {"name": "...", ...}
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SHORTCUT

    def _client(self) -> httpx.AsyncClient:
        return pinned_async_client_sync(
            _SHORTCUT_API,
            base_url=_SHORTCUT_API,
            headers={"Shortcut-Token": self._token, "Content-Type": "application/json"},
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify API connectivity by fetching the authenticated member."""
        try:
            async with self._client() as client:
                r = await client.get("/member")
                r.raise_for_status()
                body: dict[str, Any] = r.json()
                name = body.get("mention_name") or body.get("profile", {}).get("name", "") or body.get("id", "")
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

    async def _get_by_resource(
        self,
        resource: str,
        item_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = f"/{resource}" if item_id is None else f"/{resource}/{item_id}"
        async with self._client() as client:
            r = await client.get(path, params=params)
            r.raise_for_status()
            body: dict[str, Any] = r.json()
            return body

    async def _get_list(self, resource: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        path = f"/{resource}"
        async with self._client() as client:
            r = await client.get(path, params=params)
            r.raise_for_status()
            body: list[dict[str, Any]] = r.json()
            return body

    async def _post(self, resource: str, data: dict[str, Any]) -> dict[str, Any]:
        path = f"/{resource}"
        async with self._client() as client:
            r = await client.post(path, json=data)
            r.raise_for_status()
            body: dict[str, Any] = r.json()
            return body

    async def _put(self, resource: str, item_id: str, data: dict[str, Any]) -> dict[str, Any]:
        path = f"/{resource}/{item_id}"
        async with self._client() as client:
            r = await client.put(path, json=data)
            r.raise_for_status()
            body: dict[str, Any] = r.json()
            return body

    async def _query_list(self, resource: str, params: dict[str, Any] | None = None) -> ConnectorResult:
        records = await self._get_list(resource, params=params)
        return ConnectorResult(records=records, total=len(records))

    async def _query_list_collection(self, q: ConnectorQuery, resource: str) -> ConnectorResult:
        """List a filterable collection (stories/projects/epics)."""
        params: dict[str, Any] = {}
        if resource == "stories":
            for key in ("project_id", "workflow_state_id", "owner_id"):
                if key in q.filters:
                    params[key] = q.filters[key]
            if q.limit:
                params["limit"] = q.limit
        elif "suspended" in q.filters:
            params["suspended"] = str(q.filters["suspended"]).lower()
        return await self._query_list(resource, params=params)

    async def _query_single_by_resource(self, q: ConnectorQuery, resource: str) -> ConnectorResult:
        """Fetch one item of a singular resource (story/project/epic)."""
        plural, id_key, error_message = _SINGULAR_RESOURCES[resource]
        item_id = q.filters.get(id_key)
        if not item_id:
            raise ValueError(error_message)
        record = await self._get_by_resource(plural, item_id=str(item_id))
        return ConnectorResult(records=[record])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        if q.resource in ("workflows", "members", "teams"):
            return await self._query_list(q.resource)
        match q.resource:
            case "stories" | "projects" | "epics":
                return await self._query_list_collection(q, q.resource)
            case "story" | "project" | "epic":
                return await self._query_single_by_resource(q, q.resource)
            case _:
                raise ValueError(f"Unsupported Shortcut query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "story":
                return await self._post("stories", payload.data)

            case "story_update":
                story_id = payload.data.get("id")
                if not story_id:
                    raise ValueError("Missing 'id' in story_update payload")
                update_data = {k: v for k, v in payload.data.items() if k != "id"}
                return await self._put("stories", str(story_id), update_data)

            case "story_comment":
                story_id = payload.data.get("story_id")
                text = payload.data.get("text")
                if not story_id or not text:
                    raise ValueError("story_comment requires 'story_id' and 'text' in data")
                comment_data = {"text": text}
                for key in ("author_id", "created_at", "external_id"):
                    if key in payload.data:
                        comment_data[key] = payload.data[key]
                return await self._post(f"stories/{story_id}/comments", comment_data)

            case "epic":
                return await self._post("epics", payload.data)

            case _:
                raise ValueError(f"Unsupported Shortcut write resource: {payload.resource!r}")

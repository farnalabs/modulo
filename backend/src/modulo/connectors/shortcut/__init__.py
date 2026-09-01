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

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "stories":
                params: dict[str, Any] = {}
                if "project_id" in q.filters:
                    params["project_id"] = q.filters["project_id"]
                if "workflow_state_id" in q.filters:
                    params["workflow_state_id"] = q.filters["workflow_state_id"]
                if "owner_id" in q.filters:
                    params["owner_id"] = q.filters["owner_id"]
                if q.limit:
                    params["limit"] = q.limit
                records = await self._get_list("stories", params=params)
                return ConnectorResult(records=records, total=len(records))

            case "story":
                story_id = q.filters.get("story_id")
                if not story_id:
                    raise ValueError("Shortcut story query requires 'story_id' filter")
                record = await self._get_by_resource("stories", item_id=str(story_id))
                return ConnectorResult(records=[record])

            case "projects":
                params = {}
                if "suspended" in q.filters:
                    params["suspended"] = str(q.filters["suspended"]).lower()
                records = await self._get_list("projects", params=params)
                return ConnectorResult(records=records, total=len(records))

            case "project":
                project_id = q.filters.get("project_id")
                if not project_id:
                    raise ValueError("Shortcut project query requires 'project_id' filter")
                record = await self._get_by_resource("projects", item_id=str(project_id))
                return ConnectorResult(records=[record])

            case "epics":
                params = {}
                if "suspended" in q.filters:
                    params["suspended"] = str(q.filters["suspended"]).lower()
                records = await self._get_list("epics", params=params)
                return ConnectorResult(records=records, total=len(records))

            case "epic":
                epic_id = q.filters.get("epic_id")
                if not epic_id:
                    raise ValueError("Shortcut epic query requires 'epic_id' filter")
                record = await self._get_by_resource("epics", item_id=str(epic_id))
                return ConnectorResult(records=[record])

            case "workflows":
                records = await self._get_list("workflows")
                return ConnectorResult(records=records, total=len(records))

            case "members":
                records = await self._get_list("members")
                return ConnectorResult(records=records, total=len(records))

            case "teams":
                records = await self._get_list("teams")
                return ConnectorResult(records=records, total=len(records))

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

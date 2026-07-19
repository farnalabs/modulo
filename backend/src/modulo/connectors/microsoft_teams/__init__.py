"""MicrosoftTeamsConnector — async Microsoft Graph API connector for Teams."""

import asyncio
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftTeamsConnector(ConnectorBase):
    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.MICROSOFT_TEAMS

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=GRAPH_API_BASE,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/users", params={"$top": 1, "$select": "id"})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Microsoft Graph API token validated")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid Microsoft Graph API token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "teams":
                    return await self._list_teams(c, q)
                case "team":
                    return await self._get_team(c, q)
                case "channels":
                    return await self._list_channels(c, q)
                case "channel":
                    return await self._get_channel(c, q)
                case "messages":
                    return await self._list_messages(c, q)
                case "members":
                    return await self._list_members(c, q)
                case "users":
                    return await self._list_users(c, q)
                case "groups":
                    return await self._list_groups(c, q)
                case "channel_messages":
                    return await self._list_channel_messages(c, q)
                case _:
                    raise ValueError(f"Unsupported Microsoft Teams resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "message":
                    return await self._send_message(c, payload.data)
                case "channel":
                    return await self._create_channel(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Microsoft Teams write resource: {payload.resource!r}")

    async def _list_teams(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"$select": "id,displayName,description"}
        if q.filters.get("$filter"):
            params["$filter"] = q.filters["$filter"]
        if q.limit:
            params["$top"] = q.limit
        if q.cursor:
            params["$skiptoken"] = q.cursor
        resp = await c.get("/teams", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = cast("list[dict[str, Any]]", body.get("value", []))
        next_link = body.get("@odata.nextLink", "")
        skiptoken = ""
        if next_link:
            parsed = urlparse(next_link)
            params_qs = parse_qs(parsed.query)
            skiptoken = params_qs.get("$skiptoken", [""])[0]
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            next_cursor=skiptoken or None,
            total=len(records),
        )

    async def _get_team(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        team_id = q.filters.get("team_id", "")
        if not team_id:
            raise ValueError("Microsoft Teams team query requires 'team_id' in filters")
        resp = await c.get(f"/teams/{team_id}")
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[cast("dict[str, Any]", body)])

    async def _list_channels(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        team_id = q.filters.get("team_id", "")
        if not team_id:
            raise ValueError("Microsoft Teams channels query requires 'team_id' in filters")
        params: dict[str, Any] = {}
        if q.limit:
            params["$top"] = q.limit
        resp = await c.get(f"/teams/{team_id}/channels", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = cast("list[dict[str, Any]]", body.get("value", []))
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _get_channel(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        team_id = q.filters.get("team_id", "")
        channel_id = q.filters.get("channel_id", "")
        if not team_id or not channel_id:
            raise ValueError("Microsoft Teams channel query requires 'team_id' and 'channel_id' in filters")
        resp = await c.get(f"/teams/{team_id}/channels/{channel_id}")
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[cast("dict[str, Any]", body)])

    async def _list_messages(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        team_id = q.filters.get("team_id", "")
        channel_id = q.filters.get("channel_id", "")
        if not team_id or not channel_id:
            raise ValueError("Microsoft Teams messages query requires 'team_id' and 'channel_id' in filters")
        params: dict[str, Any] = {}
        if q.limit:
            params["$top"] = q.limit
        if q.filters.get("$orderby"):
            params["$orderby"] = q.filters["$orderby"]
        resp = await c.get(f"/teams/{team_id}/channels/{channel_id}/messages", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = cast("list[dict[str, Any]]", body.get("value", []))
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    _list_channel_messages = _list_messages

    async def _list_members(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        team_id = q.filters.get("team_id", "")
        if not team_id:
            raise ValueError("Microsoft Teams members query requires 'team_id' in filters")
        resp = await c.get(f"/teams/{team_id}/members")
        resp.raise_for_status()
        body = resp.json()
        records = cast("list[dict[str, Any]]", body.get("value", []))
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_users(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"$select": "id,displayName,mail,userPrincipalName"}
        if q.filters.get("$filter"):
            params["$filter"] = q.filters["$filter"]
        if q.limit:
            params["$top"] = q.limit
        resp = await c.get("/users", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = cast("list[dict[str, Any]]", body.get("value", []))
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_groups(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"$select": "id,displayName,description"}
        if q.filters.get("$filter"):
            params["$filter"] = q.filters["$filter"]
        if q.limit:
            params["$top"] = q.limit
        resp = await c.get("/groups", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = cast("list[dict[str, Any]]", body.get("value", []))
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _send_message(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        team_id = data.get("team_id", "")
        channel_id = data.get("channel_id", "")
        body_content = data.get("body", "")
        if not team_id or not channel_id or not body_content:
            raise ValueError("Microsoft Teams message write requires 'team_id', 'channel_id', and 'body' in data")
        body: dict[str, Any] = {
            "body": {
                "contentType": "html" if data.get("content_type") == "html" else "text",
                "content": body_content,
            },
        }
        resp = await c.post(f"/teams/{team_id}/channels/{channel_id}/messages", json=body)
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    async def _create_channel(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        team_id = data.get("team_id", "")
        display_name = data.get("displayName", "")
        if not team_id or not display_name:
            raise ValueError("Microsoft Teams channel write requires 'team_id' and 'displayName' in data")
        body: dict[str, Any] = {
            "displayName": display_name,
        }
        if data.get("description"):
            body["description"] = data["description"]
        if data.get("membershipType"):
            body["membershipType"] = data["membershipType"]
        resp = await c.post(f"/teams/{team_id}/channels", json=body)
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

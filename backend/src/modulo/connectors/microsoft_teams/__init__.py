"""MicrosoftTeamsConnector — async Microsoft Graph API connector for Teams."""

import asyncio
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx

from modulo._types import _DICT_STR_ANY
from modulo.connectors._safe_cursor import safe_cursor as _safe_cursor
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Repeated OData query parameter names (S1192).
_ODATA_SELECT = "$select"
_ODATA_FILTER = "$filter"
_ODATA_ORDERBY = "$orderby"


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
                resp = await c.get("/users", params={"$top": 1, _ODATA_SELECT: "id"})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Microsoft Graph API token validated")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid Microsoft Graph API token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

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
        params: dict[str, Any] = {_ODATA_SELECT: "id,displayName,description"}
        if q.filters.get(_ODATA_FILTER):
            params[_ODATA_FILTER] = q.filters[_ODATA_FILTER]
        if q.limit:
            params["$top"] = q.limit
        if q.cursor:
            params["$skiptoken"] = q.cursor
        resp = await c.get("/teams", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "value")
        next_cursor: str | None = None
        next_link = body.get("@odata.nextLink", "") if isinstance(body, dict) else ""
        if isinstance(next_link, str) and next_link:
            params_qs = parse_qs(urlparse(next_link).query)
            skiptoken = params_qs.get("$skiptoken", [""])[0]
            next_cursor = _safe_cursor(skiptoken)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            next_cursor=next_cursor,
            total=len(records),
        )

    async def _get_team(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        team_id = q.filters.get("team_id", "")
        if not team_id:
            raise ValueError("Microsoft Teams team query requires 'team_id' in filters")
        resp = await c.get(f"/teams/{team_id}")
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[cast(_DICT_STR_ANY, body)])

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
        records = _safe_records(body, "value")
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
        return ConnectorResult(records=[cast(_DICT_STR_ANY, body)])

    async def _list_messages(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        team_id = q.filters.get("team_id", "")
        channel_id = q.filters.get("channel_id", "")
        if not team_id or not channel_id:
            raise ValueError("Microsoft Teams messages query requires 'team_id' and 'channel_id' in filters")
        params: dict[str, Any] = {}
        if q.limit:
            params["$top"] = q.limit
        if q.filters.get(_ODATA_ORDERBY):
            params[_ODATA_ORDERBY] = q.filters[_ODATA_ORDERBY]
        resp = await c.get(f"/teams/{team_id}/channels/{channel_id}/messages", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "value")
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
        records = _safe_records(body, "value")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_users(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {_ODATA_SELECT: "id,displayName,mail,userPrincipalName"}
        if q.filters.get(_ODATA_FILTER):
            params[_ODATA_FILTER] = q.filters[_ODATA_FILTER]
        if q.limit:
            params["$top"] = q.limit
        resp = await c.get("/users", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "value")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_groups(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {_ODATA_SELECT: "id,displayName,description"}
        if q.filters.get(_ODATA_FILTER):
            params[_ODATA_FILTER] = q.filters[_ODATA_FILTER]
        if q.limit:
            params["$top"] = q.limit
        resp = await c.get("/groups", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "value")
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
        return cast(_DICT_STR_ANY, resp.json())

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
        return cast(_DICT_STR_ANY, resp.json())

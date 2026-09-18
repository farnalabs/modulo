"""DiscordConnector — async Discord REST API v10 connector."""

import asyncio
from typing import Any, cast

import httpx

from modulo._types import _DICT_STR_ANY
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

_DISCORD_API = "https://discord.com/api/v10"


class DiscordConnector(ConnectorBase):
    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.DISCORD

    def _client(self) -> httpx.AsyncClient:
        return pinned_async_client_sync(
            _DISCORD_API,
            base_url=_DISCORD_API,
            headers={
                "Authorization": f"Bot {self._token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/users/@me")
                if resp.status_code == 200:
                    user = resp.json()
                    return HealthResult(ok=True, detail=user.get("username", "Discord bot validated"))
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid Discord bot token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    def _require_filter(self, q: ConnectorQuery, key: str, message: str) -> str:
        value = q.filters.get(key, "")
        if not value:
            raise ValueError(message)
        return str(value)

    @staticmethod
    def _records_result(data: Any) -> ConnectorResult:
        records = _safe_records_list(data)
        return ConnectorResult(records=records, total=len(records))

    async def _query_guilds(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        resp = await c.get("/users/@me/guilds", params={"limit": min(q.limit, 200)})
        resp.raise_for_status()
        return self._records_result(resp.json())

    async def _query_channels(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        guild_id = self._require_filter(q, "guild_id", "Discord channels query requires 'guild_id' in filters")
        resp = await c.get(f"/guilds/{guild_id}/channels")
        resp.raise_for_status()
        data = _safe_records_list(resp.json())
        limited = data[: q.limit] if q.limit else data
        return ConnectorResult(records=limited, total=len(data))

    async def _query_messages(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        channel_id = self._require_filter(q, "channel_id", "Discord messages query requires 'channel_id' in filters")
        params: dict[str, Any] = {"limit": min(q.limit, 100)}
        for key in ("around", "before", "after"):
            if key in q.filters:
                params[key] = q.filters[key]
        resp = await c.get(f"/channels/{channel_id}/messages", params=params)
        resp.raise_for_status()
        return self._records_result(resp.json())

    async def _query_guild_members(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        guild_id = self._require_filter(q, "guild_id", "Discord guild_members query requires 'guild_id' in filters")
        resp = await c.get(f"/guilds/{guild_id}/members", params={"limit": min(q.limit, 100)})
        resp.raise_for_status()
        return self._records_result(resp.json())

    async def _query_roles(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        guild_id = self._require_filter(q, "guild_id", "Discord roles query requires 'guild_id' in filters")
        resp = await c.get(f"/guilds/{guild_id}/roles")
        resp.raise_for_status()
        return self._records_result(resp.json())

    async def _query_guild(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        guild_id = self._require_filter(q, "guild_id", "Discord guild query requires 'guild_id' in filters")
        resp = await c.get(f"/guilds/{guild_id}")
        resp.raise_for_status()
        return ConnectorResult(records=[cast(_DICT_STR_ANY, resp.json())], total=1)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "guilds":
                    return await self._query_guilds(c, q)
                case "channels":
                    return await self._query_channels(c, q)
                case "messages":
                    return await self._query_messages(c, q)
                case "guild_members":
                    return await self._query_guild_members(c, q)
                case "roles":
                    return await self._query_roles(c, q)
                case "guild":
                    return await self._query_guild(c, q)
                case _:
                    raise ValueError(f"Unsupported Discord resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "message":
                    channel_id = payload.data.get("channel_id", "")
                    content = payload.data.get("content", "")
                    if not channel_id or not content:
                        raise ValueError("Discord message write requires 'channel_id' and 'content' in data")
                    body: dict[str, Any] = {"content": content}
                    if "embed" in payload.data:
                        body["embed"] = payload.data["embed"]
                    resp = await c.post(f"/channels/{channel_id}/messages", json=body)
                    resp.raise_for_status()
                    return cast(_DICT_STR_ANY, resp.json())
                case "reaction":
                    channel_id = payload.data.get("channel_id", "")
                    message_id = payload.data.get("message_id", "")
                    emoji = payload.data.get("emoji", "")
                    if not channel_id or not message_id or not emoji:
                        raise ValueError(
                            "Discord reaction write requires 'channel_id', 'message_id', and 'emoji' in data",
                        )
                    resp = await c.put(f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me")
                    resp.raise_for_status()
                    return {"ok": True}
                case "channel":
                    guild_id = payload.data.get("guild_id", "")
                    name = payload.data.get("name", "")
                    if not guild_id or not name:
                        raise ValueError("Discord channel write requires 'guild_id' and 'name' in data")
                    channel_body: dict[str, Any] = {"name": name, "type": payload.data.get("type", 0)}
                    if "topic" in payload.data:
                        channel_body["topic"] = payload.data["topic"]
                    resp = await c.post(f"/guilds/{guild_id}/channels", json=channel_body)
                    resp.raise_for_status()
                    return cast(_DICT_STR_ANY, resp.json())
                case _:
                    raise ValueError(f"Unsupported Discord write resource: {payload.resource!r}")

"""DiscordConnector — async Discord REST API v10 connector."""

import asyncio
from typing import Any, cast

import httpx

from modulo.connectors._safe_page import safe_records_list as _safe_records_list
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.types import _DICT_STR_ANY

_DISCORD_API = "https://discord.com/api/v10"


class DiscordConnector(ConnectorBase):
    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.DISCORD

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
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
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "guilds":
                    resp = await c.get("/users/@me/guilds", params={"limit": min(q.limit, 200)})
                    resp.raise_for_status()
                    data = _safe_records_list(resp.json())
                    return ConnectorResult(records=data, total=len(data))
                case "channels":
                    guild_id = q.filters.get("guild_id", "")
                    if not guild_id:
                        raise ValueError("Discord channels query requires 'guild_id' in filters")
                    resp = await c.get(f"/guilds/{guild_id}/channels")
                    resp.raise_for_status()
                    data = _safe_records_list(resp.json())
                    return ConnectorResult(records=data[: q.limit] if q.limit else data, total=len(data))
                case "messages":
                    channel_id = q.filters.get("channel_id", "")
                    if not channel_id:
                        raise ValueError("Discord messages query requires 'channel_id' in filters")
                    params: dict[str, Any] = {"limit": min(q.limit, 100)}
                    for key in ("around", "before", "after"):
                        if key in q.filters:
                            params[key] = q.filters[key]
                    resp = await c.get(f"/channels/{channel_id}/messages", params=params)
                    resp.raise_for_status()
                    data = _safe_records_list(resp.json())
                    return ConnectorResult(records=data, total=len(data))
                case "guild_members":
                    guild_id = q.filters.get("guild_id", "")
                    if not guild_id:
                        raise ValueError("Discord guild_members query requires 'guild_id' in filters")
                    resp = await c.get(f"/guilds/{guild_id}/members", params={"limit": min(q.limit, 100)})
                    resp.raise_for_status()
                    data = _safe_records_list(resp.json())
                    return ConnectorResult(records=data, total=len(data))
                case "roles":
                    guild_id = q.filters.get("guild_id", "")
                    if not guild_id:
                        raise ValueError("Discord roles query requires 'guild_id' in filters")
                    resp = await c.get(f"/guilds/{guild_id}/roles")
                    resp.raise_for_status()
                    data = _safe_records_list(resp.json())
                    return ConnectorResult(records=data, total=len(data))
                case "guild":
                    guild_id = q.filters.get("guild_id", "")
                    if not guild_id:
                        raise ValueError("Discord guild query requires 'guild_id' in filters")
                    resp = await c.get(f"/guilds/{guild_id}")
                    resp.raise_for_status()
                    return ConnectorResult(records=[cast(_DICT_STR_ANY, resp.json())])
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

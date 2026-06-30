"""SlackConnector — async Slack Web API connector."""

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

_SLACK_API = "https://slack.com/api"

_RATE_LIMITED_STATUS = 429


class SlackConnector(ConnectorBase):
    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SLACK

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bot_token}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_SLACK_API, headers=self._headers(), timeout=30)

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/api.test", timeout=10)
                if resp.status_code == _RATE_LIMITED_STATUS:
                    retry_after = resp.headers.get("Retry-After", "unknown")
                    return HealthResult(ok=False, detail=f"Rate limited; retry after {retry_after}s")
                resp.raise_for_status()
                body = resp.json()
                if body.get("ok"):
                    return HealthResult(ok=True)
                return HealthResult(ok=False, detail=body.get("error", "unknown"))
        except httpx.HTTPStatusError as e:
            return HealthResult(ok=False, detail=f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            return HealthResult(ok=False, detail=str(e))

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "channels":
                    return await self._list_channels(c, q)
                case "messages":
                    return await self._get_messages(c, q)
                case "users":
                    return await self._list_users(c, q)
                case _:
                    raise ValueError(f"Unsupported Slack resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "message":
                    return await self._post_message(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Slack write resource: {payload.resource!r}")

    async def _list_channels(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"limit": q.limit, "types": "public_channel,private_channel"}
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/conversations.list", params=params)
        if resp.status_code == _RATE_LIMITED_STATUS:
            retry_after = resp.headers.get("Retry-After", "unknown")
            raise ValueError(f"Rate limited by Slack API; retry after {retry_after}s")
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise ValueError(f"Slack API error in conversations.list: {body.get('error', 'unknown')}")
        return ConnectorResult(
            records=body.get("channels", []),
            next_cursor=body.get("response_metadata", {}).get("next_cursor"),
        )

    async def _get_messages(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        channel = q.filters.get("channel")
        if not channel:
            raise ValueError("Slack messages query requires 'channel' filter")
        params: dict[str, Any] = {"channel": channel, "limit": q.limit}
        if q.filters.get("oldest"):
            params["oldest"] = q.filters["oldest"]
        if q.filters.get("latest"):
            params["latest"] = q.filters["latest"]
        resp = await c.get("/conversations.history", params=params)
        if resp.status_code == _RATE_LIMITED_STATUS:
            retry_after = resp.headers.get("Retry-After", "unknown")
            raise ValueError(f"Rate limited by Slack API; retry after {retry_after}s")
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise ValueError(f"Slack API error in conversations.history: {body.get('error', 'unknown')}")
        return ConnectorResult(
            records=body.get("messages", []),
            next_cursor=body.get("response_metadata", {}).get("next_cursor"),
        )

    async def _list_users(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"limit": q.limit}
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/users.list", params=params)
        if resp.status_code == _RATE_LIMITED_STATUS:
            retry_after = resp.headers.get("Retry-After", "unknown")
            raise ValueError(f"Rate limited by Slack API; retry after {retry_after}s")
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise ValueError(f"Slack API error in users.list: {body.get('error', 'unknown')}")
        return ConnectorResult(
            records=body.get("members", []),
            next_cursor=body.get("response_metadata", {}).get("next_cursor"),
        )

    async def _post_message(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        channel = data.get("channel")
        if not channel:
            raise ValueError("Missing 'channel' in message payload")
        body_data = {k: v for k, v in data.items() if k != "channel"}
        resp = await c.post("/chat.postMessage", json={"channel": channel, **body_data})
        if resp.status_code == _RATE_LIMITED_STATUS:
            retry_after = resp.headers.get("Retry-After", "unknown")
            raise ValueError(f"Rate limited by Slack API; retry after {retry_after}s")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if not body.get("ok"):
            raise ValueError(f"Slack API error: {body.get('error', 'unknown')}")
        return body

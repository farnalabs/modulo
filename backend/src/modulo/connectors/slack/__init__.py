"""SlackConnector — async Slack Web API connector."""

import asyncio
import json
import random
from typing import Any, cast

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

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0


def _parse_retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return None


def _compute_retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    retry_after = _parse_retry_after(response) if response else None
    if retry_after is not None:
        return float(min(retry_after, _MAX_DELAY))
    jitter = random.uniform(0, 1)  # noqa: S311 — non-cryptographic jitter for retry delays
    return float(min(_BASE_DELAY * (2**attempt) + jitter, _MAX_DELAY))


def _check_slack_ok(body: Any, context: str) -> None:
    if not isinstance(body, dict):
        raise ValueError(f"Slack API returned non-JSON-object response in {context}: {type(body).__name__}")
    if not body.get("ok"):
        raise ValueError(f"Slack API error in {context}: {body.get('error', 'unknown')}")


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

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    r = await client.request(method, path, **kwargs)
                    if r.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                        delay = _compute_retry_delay(attempt, r)
                        await asyncio.sleep(delay)
                        continue
                    r.raise_for_status()
                    return r
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    delay = _compute_retry_delay(attempt, exc.response)
                    await asyncio.sleep(delay)
                    continue
                raise ValueError(f"Slack API HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = _compute_retry_delay(attempt)
                    await asyncio.sleep(delay)
                    continue
                raise ValueError("Slack API timeout") from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = _compute_retry_delay(attempt)
                    await asyncio.sleep(delay)
                    continue
                raise ValueError("Slack API connection error") from exc
        raise ValueError("Slack API request failed after retries") from last_exc

    async def _parse_json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Slack API returned invalid JSON: {response.text[:200]}") from exc

    async def verify_scopes(self) -> dict[str, Any]:
        r = await self._call_api("GET", "/auth.test")
        body = await self._parse_json(r)
        if not body.get("ok"):
            raise ValueError(f"Token validation failed: {body.get('error', 'unknown')}")
        return cast(dict[str, Any], body)

    async def health_check(self) -> HealthResult:
        try:
            r = await self._call_api("GET", "/api.test", timeout=10)
            body = await self._parse_json(r)
            if not body.get("ok"):
                return HealthResult(ok=False, detail=body.get("error", "unknown"))
            try:
                await self.verify_scopes()
            except ValueError as exc:
                msg = str(exc)
                if "connection error" in msg or "timeout" in msg or "HTTP" in msg:
                    return HealthResult(ok=False, detail=f"Token validation failed due to network error: {exc}")
                return HealthResult(ok=False, detail=f"Token is invalid or revoked: {exc}")
            return HealthResult(ok=True)
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "channels":
                return await self._list_channels(q)
            case "messages":
                return await self._get_messages(q)
            case "users":
                return await self._list_users(q)
            case "channel_info":
                return await self._get_channel_info(q)
            case "channel_members":
                return await self._get_channel_members(q)
            case "thread_replies":
                return await self._get_thread_replies(q)
            case _:
                raise ValueError(f"Unsupported Slack resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "message":
                return await self._post_message(payload.data)
            case "thread_reply":
                return await self._post_thread_reply(payload.data)
            case _:
                raise ValueError(f"Unsupported Slack write resource: {payload.resource!r}")

    async def _list_channels(self, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"limit": q.limit, "types": "public_channel,private_channel"}
        if q.cursor:
            params["cursor"] = q.cursor
        r = await self._call_api("GET", "/conversations.list", params=params)
        body = await self._parse_json(r)
        _check_slack_ok(body, "conversations.list")
        meta = body.get("response_metadata") or {}
        return ConnectorResult(
            records=body.get("channels", []),
            next_cursor=meta.get("next_cursor") if isinstance(meta, dict) else None,
        )

    async def _get_messages(self, q: ConnectorQuery) -> ConnectorResult:
        if "channel" not in q.filters:
            raise ValueError("Slack messages query requires 'channel' filter")
        channel = q.filters["channel"]
        params: dict[str, Any] = {"channel": channel, "limit": q.limit}
        if q.filters.get("oldest"):
            params["oldest"] = q.filters["oldest"]
        if q.filters.get("latest"):
            params["latest"] = q.filters["latest"]
        r = await self._call_api("GET", "/conversations.history", params=params)
        body = await self._parse_json(r)
        _check_slack_ok(body, "conversations.history")
        meta = body.get("response_metadata") or {}
        return ConnectorResult(
            records=body.get("messages", []),
            next_cursor=meta.get("next_cursor") if isinstance(meta, dict) else None,
        )

    async def _list_users(self, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"limit": q.limit}
        if q.cursor:
            params["cursor"] = q.cursor
        r = await self._call_api("GET", "/users.list", params=params)
        body = await self._parse_json(r)
        _check_slack_ok(body, "users.list")
        meta = body.get("response_metadata") or {}
        return ConnectorResult(
            records=body.get("members", []),
            next_cursor=meta.get("next_cursor") if isinstance(meta, dict) else None,
        )

    async def _post_message(self, data: dict[str, Any]) -> dict[str, Any]:
        if "channel" not in data:
            raise ValueError("Missing 'channel' in message payload")
        channel = data["channel"]
        body_data = {k: v for k, v in data.items() if k != "channel"}
        r = await self._call_api("POST", "/chat.postMessage", json={"channel": channel, **body_data})
        body: dict[str, Any] = await self._parse_json(r)
        _check_slack_ok(body, "chat.postMessage")
        return body

    async def _get_channel_info(self, q: ConnectorQuery) -> ConnectorResult:
        if "channel" not in q.filters:
            raise ValueError("Slack channel_info query requires 'channel' filter")
        channel = q.filters["channel"]
        r = await self._call_api("GET", "/conversations.info", params={"channel": channel})
        body = await self._parse_json(r)
        _check_slack_ok(body, "conversations.info")
        return ConnectorResult(records=[body.get("channel", {})])

    async def _get_channel_members(self, q: ConnectorQuery) -> ConnectorResult:
        if "channel" not in q.filters:
            raise ValueError("Slack channel_members query requires 'channel' filter")
        channel = q.filters["channel"]
        params: dict[str, Any] = {"channel": channel, "limit": q.limit}
        if q.cursor:
            params["cursor"] = q.cursor
        r = await self._call_api("GET", "/conversations.members", params=params)
        body = await self._parse_json(r)
        _check_slack_ok(body, "conversations.members")
        return ConnectorResult(
            records=[{"user_id": uid} for uid in body.get("members", [])],
            next_cursor=body.get("response_metadata", {}).get("next_cursor"),
        )

    async def _get_thread_replies(self, q: ConnectorQuery) -> ConnectorResult:
        if "channel" not in q.filters:
            raise ValueError("Slack thread_replies query requires 'channel' filter")
        if "thread_ts" not in q.filters:
            raise ValueError("Slack thread_replies query requires 'thread_ts' filter")
        channel = q.filters["channel"]
        thread_ts = q.filters["thread_ts"]
        params: dict[str, Any] = {"channel": channel, "ts": thread_ts, "limit": q.limit}
        if q.filters.get("oldest"):
            params["oldest"] = q.filters["oldest"]
        if q.filters.get("latest"):
            params["latest"] = q.filters["latest"]
        r = await self._call_api("GET", "/conversations.replies", params=params)
        body = await self._parse_json(r)
        _check_slack_ok(body, "conversations.replies")
        return ConnectorResult(
            records=body.get("messages", []),
            next_cursor=body.get("response_metadata", {}).get("next_cursor"),
        )

    async def _post_thread_reply(self, data: dict[str, Any]) -> dict[str, Any]:
        if "channel" not in data:
            raise ValueError("Missing 'channel' in thread_reply payload")
        if "thread_ts" not in data:
            raise ValueError("Missing 'thread_ts' in thread_reply payload")
        channel = data["channel"]
        thread_ts = data["thread_ts"]
        body_data = {k: v for k, v in data.items() if k not in ("channel", "thread_ts")}
        r = await self._call_api(
            "POST",
            "/chat.postMessage",
            json={"channel": channel, "thread_ts": thread_ts, **body_data},
        )
        body: dict[str, Any] = await self._parse_json(r)
        _check_slack_ok(body, "chat.postMessage (thread)")
        return body

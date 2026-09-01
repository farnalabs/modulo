"""DatadogConnector — async Datadog REST API connector (v1 + v2)."""

import asyncio
from typing import Any, cast

import httpx

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
from modulo.connectors.security import CredentialRedactor, redacting
from modulo.core.ssrf import pinned_async_client_sync

_SITES: dict[str, str] = {
    "us": "https://api.datadoghq.com",
    "eu": "https://api.datadoghq.eu",
    "us3": "https://api.us3.datadoghq.com",
    "us5": "https://api.us5.datadoghq.com",
    "ap1": "https://api.ap1.datadoghq.com",
}


class DatadogConnector(ConnectorBase):
    def __init__(self, api_key: str, app_key: str, site: str = "us") -> None:
        self._api_key = api_key
        self._app_key = app_key
        base = _SITES.get(site)
        if base is None:
            raise ValueError(f"Unknown Datadog site: {site!r}. Choose from: {', '.join(_SITES)}")
        self._base = base
        self._redactor = CredentialRedactor([api_key, app_key])

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.DATADOG

    def _client(self) -> httpx.AsyncClient:
        base = self._base
        return pinned_async_client_sync(
            base,
            base_url=base,
            headers={
                "DD-API-KEY": self._api_key,
                "DD-APPLICATION-KEY": self._app_key,
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/api/v1/validate")
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Datadog API key validated")
                if resp.status_code == 403:
                    return HealthResult(ok=False, detail="Invalid Datadog API key")
                return HealthResult(
                    ok=False, detail=self._redactor.redact(f"HTTP {resp.status_code}: {resp.text[:200]}")
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                return HealthResult(ok=False, detail="Invalid Datadog API key")
            return HealthResult(
                ok=False, detail=self._redactor.redact(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(self._redactor.redact_exc(exc))

    @redacting
    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "monitors":
                    return await self._list_monitors(c, q)
                case "events":
                    return await self._list_events(c, q)
                case "metrics":
                    return await self._query_metrics(c, q)
                case "dashboards":
                    return await self._list_dashboards(c, q)
                case "logs":
                    return await self._search_logs(c, q)
                case _:
                    raise ValueError(f"Unsupported Datadog resource: {q.resource!r}")

    @redacting
    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "event":
                    return await self._create_event(c, payload.data)
                case "monitor":
                    return await self._create_monitor(c, payload.data)
                case "monitor_status":
                    return await self._update_monitor_status(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Datadog write resource: {payload.resource!r}")

    async def _list_monitors(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        group_states = q.filters.get("group_states")
        if group_states:
            params["group_states"] = group_states
        name = q.filters.get("name")
        if name:
            params["name"] = name
        tags = q.filters.get("tags")
        if tags:
            params["tags"] = tags
        resp = await c.get("/api/v1/monitor", params=params)
        resp.raise_for_status()
        data: list[dict[str, Any]] = resp.json()
        return ConnectorResult(records=data[: q.limit], total=len(data))

    async def _list_events(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        start = q.filters.get("start")
        end = q.filters.get("end")
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        priority = q.filters.get("priority")
        if priority:
            params["priority"] = priority
        sources = q.filters.get("sources")
        if sources:
            params["sources"] = sources
        tags = q.filters.get("tags")
        if tags:
            params["tags"] = tags
        resp = await c.get("/api/v1/events", params=params)
        resp.raise_for_status()
        body = resp.json()
        events: list[dict[str, Any]] = _safe_records(body, "events")
        return ConnectorResult(records=events[: q.limit], total=len(events))

    async def _query_metrics(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        data: dict[str, Any] = {
            "data": {
                "attributes": {
                    "formulas": q.filters.get("formulas", []),
                    "from": q.filters.get("from", 0),
                    "to": q.filters.get("to", 0),
                    "queries": q.filters.get("queries", []),
                },
            },
        }
        resp = await c.post("/api/v2/query/timeseries", json=data)
        resp.raise_for_status()
        body = resp.json()
        series: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(records=series, total=len(series))

    async def _list_dashboards(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("filter"):
            params["filter"] = q.filters["filter"]
        if q.filters.get("count"):
            params["count"] = q.filters["count"]
        resp = await c.get("/api/v2/dashboards", params=params)
        resp.raise_for_status()
        body = resp.json()
        dashboards: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(records=dashboards[: q.limit], total=len(dashboards))

    async def _search_logs(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        data: dict[str, Any] = {
            "filter": q.filters.get("filter", {}),
            "sort": q.filters.get("sort", "timestamp"),
        }
        if q.limit:
            data["page"] = {"limit": q.limit}
        if q.cursor:
            data.setdefault("page", {})["cursor"] = q.cursor
        resp = await c.post("/api/v2/logs/events/search", json=data)
        resp.raise_for_status()
        body = resp.json()
        logs: list[dict[str, Any]] = _safe_records(body, "data")
        meta = body.get("meta") if isinstance(body, dict) else None
        page = meta.get("page") if isinstance(meta, dict) else None
        after = page.get("after") if isinstance(page, dict) else None
        next_cursor: str | None = after if isinstance(after, str) else None
        return ConnectorResult(
            records=logs,
            total=None,
            next_cursor=next_cursor,
        )

    async def _create_event(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        title = data.get("title")
        text = data.get("text")
        if not title or not text:
            raise ValueError("Datadog event write requires 'title' and 'text' in data")
        body: dict[str, Any] = {"title": title, "text": text}
        for key in ("priority", "tags", "alert_type", "source_type_name", "host", "date_happened"):
            if key in data:
                body[key] = data[key]
        resp = await c.post("/api/v1/events", json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return cast("dict[str, Any]", result.get("event", {}))

    async def _create_monitor(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        query = data.get("query")
        name = data.get("name", query)
        _type = data.get("type")
        if not query or not _type:
            raise ValueError("Datadog monitor write requires 'query' and 'type' in data")
        body: dict[str, Any] = {"query": query, "name": name, "type": _type}
        for key in ("message", "tags", "options", "priority", "restricted_roles"):
            if key in data:
                body[key] = data[key]
        resp = await c.post("/api/v1/monitor", json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _update_monitor_status(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        monitor_id = data.get("monitor_id")
        if monitor_id is None:
            raise ValueError("Datadog monitor_status write requires 'monitor_id' in data")
        body: dict[str, Any] = {}
        for key in ("status", "message"):
            if key in data:
                body[key] = data[key]
        resp = await c.put(f"/api/v1/monitor/{monitor_id}", json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

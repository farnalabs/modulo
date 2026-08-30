"""GrafanaConnector — async Grafana HTTP API connector."""

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
from modulo.core.ssrf import validate_outbound_url


class GrafanaConnector(ConnectorBase):
    def __init__(self, token: str, base_url: str = "http://localhost:3000") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GRAFANA

    def _client(self) -> httpx.AsyncClient:
        validate_outbound_url(self._base_url)
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/api/health")
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Grafana API healthy")
                if resp.status_code in (401, 403):
                    return HealthResult(ok=False, detail="Invalid Grafana API token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "dashboards":
                    return await self._list_dashboards(c, q)
                case "dashboard":
                    return await self._get_dashboard(c, q)
                case "alerts":
                    return await self._list_alerts(c, q)
                case "alert_rules":
                    return await self._list_alert_rules(c, q)
                case "datasources":
                    return await self._list_datasources(c, q)
                case "folders":
                    return await self._list_folders(c, q)
                case "organizations":
                    return await self._list_organizations(c, q)
                case "users":
                    return await self._list_users(c, q)
                case "annotations":
                    return await self._list_annotations(c, q)
                case _:
                    raise ValueError(f"Unsupported Grafana resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "annotation":
                    return await self._create_annotation(c, payload.data)
                case "dashboard":
                    return await self._create_dashboard(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Grafana write resource: {payload.resource!r}")

    async def _list_dashboards(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"type": "dash-db"}
        if q.filters.get("folderIds"):
            params["folderIds"] = q.filters["folderIds"]
        if q.filters.get("query"):
            params["query"] = q.filters["query"]
        if q.filters.get("tag"):
            params["tag"] = q.filters["tag"]
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get("/api/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records_list(body)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _get_dashboard(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        uid = q.filters.get("uid", "")
        if not uid:
            raise ValueError("Grafana dashboard query requires 'uid' in filters")
        resp = await c.get(f"/api/dashboards/uid/{uid}")
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[cast(_DICT_STR_ANY, body)])

    async def _list_alerts(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        for key in ("state", "folderIds", "dashboardId", "panelId", "query"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get("/api/alerts", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records_list(body)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_alert_rules(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("limit"):
            params["limit"] = q.filters["limit"]
        resp = await c.get("/api/v1/provisioning/alert-rules", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records_list(body)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_datasources(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        resp = await c.get("/api/datasources")
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records_list(body)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_folders(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get("/api/folders", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records_list(body)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_organizations(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get("/api/orgs", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records_list(body)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_users(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("permission"):
            params["permission"] = q.filters["permission"]
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get("/api/users", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records_list(body)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _list_annotations(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"type": "alert"}
        for key in ("from", "to", "alertId", "dashboardId", "panelId", "limit"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.filters.get("limit"):
            params["limit"] = q.filters["limit"]
        resp = await c.get("/api/annotations", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records_list(body)
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
        )

    async def _create_annotation(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        text = data.get("text")
        if not text:
            raise ValueError("Grafana annotation write requires 'text' in data")
        body: dict[str, Any] = {"text": text}
        if data.get("dashboardId"):
            body["dashboardId"] = data["dashboardId"]
        if data.get("panelId"):
            body["panelId"] = data["panelId"]
        if data.get("tags"):
            body["tags"] = data["tags"]
        if data.get("time"):
            body["time"] = data["time"]
        if data.get("timeEnd"):
            body["timeEnd"] = data["timeEnd"]
        resp = await c.post("/api/annotations", json=body)
        resp.raise_for_status()
        return cast(_DICT_STR_ANY, resp.json())

    async def _create_dashboard(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        dashboard = data.get("dashboard")
        if not dashboard:
            raise ValueError("Grafana dashboard write requires 'dashboard' in data")
        overwrite = data.get("overwrite", False)
        body: dict[str, Any] = {
            "dashboard": dashboard,
            "overwrite": overwrite,
        }
        if data.get("folderId"):
            body["folderId"] = data["folderId"]
        if data.get("folderUid"):
            body["folderUid"] = data["folderUid"]
        if data.get("message"):
            body["message"] = data["message"]
        resp = await c.post("/api/dashboards/db", json=body)
        resp.raise_for_status()
        return cast(_DICT_STR_ANY, resp.json())

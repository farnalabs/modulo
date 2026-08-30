"""SentryConnector — async Sentry API connector (v0)."""

import asyncio
from typing import Any

import httpx

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


class SentryConnector(ConnectorBase):
    def __init__(self, token: str, organization: str, base_url: str = "https://sentry.io") -> None:
        self._token = token
        self._organization = organization
        self._base = f"{base_url.rstrip('/')}/api/0"

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SENTRY

    def _client(self) -> httpx.AsyncClient:
        validate_outbound_url(self._base)
        return httpx.AsyncClient(
            base_url=self._base,
            headers={
                "Authorization": f"Bearer {self._token}",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/")
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Sentry API token validated")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid Sentry auth token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "issues":
                    return await self._list_issues(c, q)
                case "events":
                    return await self._list_events(c, q)
                case "projects":
                    return await self._list_projects(c, q)
                case "releases":
                    return await self._list_releases(c, q)
                case "teams":
                    return await self._list_teams(c, q)
                case "issue_events":
                    return await self._list_issue_events(c, q)
                case _:
                    raise ValueError(f"Unsupported Sentry resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "issue_status":
                    return await self._update_issue_status(c, payload.data)
                case "event_comment":
                    return await self._create_event_comment(c, payload.data)
                case "release":
                    return await self._create_release(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Sentry write resource: {payload.resource!r}")

    async def _list_issues(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org = self._organization
        project = q.filters.get("project", "")
        if not project:
            raise ValueError("Sentry issues query requires 'project' in filters")
        params: dict[str, Any] = {}
        for key in ("query", "status", "statsPeriod", "sort", "environment"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get(f"/projects/{org}/{project}/issues/", params=params)
        resp.raise_for_status()
        data = _safe_records_list(resp.json())
        return ConnectorResult(records=data[: q.limit], total=len(data))

    async def _list_events(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org = self._organization
        project = q.filters.get("project", "")
        if not project:
            raise ValueError("Sentry events query requires 'project' in filters")
        params: dict[str, Any] = {}
        for key in ("query", "statsPeriod", "environment", "sort"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get(f"/projects/{org}/{project}/events/", params=params)
        resp.raise_for_status()
        data = _safe_records_list(resp.json())
        return ConnectorResult(records=data[: q.limit], total=len(data))

    async def _list_projects(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/projects/", params=params)
        resp.raise_for_status()
        data = _safe_records_list(resp.json())
        return ConnectorResult(records=data[: q.limit], total=len(data))

    async def _list_releases(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org = self._organization
        params: dict[str, Any] = {}
        for key in ("query", "status", "sort"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get(f"/organizations/{org}/releases/", params=params)
        resp.raise_for_status()
        data = _safe_records_list(resp.json())
        return ConnectorResult(records=data[: q.limit], total=len(data))

    async def _list_teams(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org = self._organization
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get(f"/organizations/{org}/teams/", params=params)
        resp.raise_for_status()
        data = _safe_records_list(resp.json())
        return ConnectorResult(records=data[: q.limit], total=len(data))

    async def _list_issue_events(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        issue_id = q.filters.get("issue_id", "")
        if not issue_id:
            raise ValueError("Sentry issue_events query requires 'issue_id' in filters")
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get(f"/issues/{issue_id}/events/", params=params)
        resp.raise_for_status()
        data = _safe_records_list(resp.json())
        return ConnectorResult(records=data[: q.limit], total=len(data))

    async def _update_issue_status(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        issue_id = data.get("issue_id")
        if not issue_id:
            raise ValueError("Sentry issue_status write requires 'issue_id' in data")
        status = data.get("status")
        if not status:
            raise ValueError("Sentry issue_status write requires 'status' in data (resolve, unresolved, ignore)")
        body: dict[str, Any] = {"status": status}
        resp = await c.put(f"/issues/{issue_id}/", json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _create_event_comment(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        issue_id = data.get("issue_id")
        if not issue_id:
            raise ValueError("Sentry event_comment write requires 'issue_id' in data")
        text = data.get("text")
        if not text:
            raise ValueError("Sentry event_comment write requires 'text' in data")
        body: dict[str, Any] = {"text": text}
        resp = await c.post(f"/issues/{issue_id}/comments/", json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _create_release(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        org = self._organization
        version = data.get("version")
        if not version:
            raise ValueError("Sentry release write requires 'version' in data")
        body: dict[str, Any] = {"version": version}
        for key in ("ref", "url", "projects", "dateReleased", "commits"):
            if key in data:
                body[key] = data[key]
        resp = await c.post(f"/organizations/{org}/releases/", json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

"""SnykConnector — async Snyk REST API connector for vulnerability scanning."""

import asyncio
from typing import Any

import httpx

from modulo.connectors._safe_cursor import safe_cursor as _safe_cursor
from modulo.connectors._safe_int import safe_int as _safe_int
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

_API_BASE = "https://api.snyk.io/rest"


def _next_cursor(body: dict[str, Any]) -> str | None:
    if not isinstance(body, dict):
        return None
    links = body.get("links")
    if not isinstance(links, dict):
        return None
    return _safe_cursor(links.get("next"))


def _meta_total(body: dict[str, Any], fallback: int) -> int:
    """Extract Snyk's ``meta.count`` as a safe int.

    Guards against two corrupt-response failure modes: a non-dict ``meta``
    value (which would raise ``AttributeError`` through a bare ``.get()``
    chain) and a non-finite ``count`` (``inf``/``nan`` from an overflowing
    JSON literal), which would otherwise poison the reported total.
    """
    if not isinstance(body, dict):
        return fallback
    meta = body.get("meta")
    if not isinstance(meta, dict):
        return fallback
    raw = meta.get("count")
    if raw is None:
        return fallback
    return _safe_int(raw)


class SnykConnector(ConnectorBase):
    def __init__(self, token: str) -> None:
        self._token = token
        self._version = "2024-10-15"

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SNYK

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_API_BASE,
            headers={
                "Authorization": f"token {self._token}",
                "Content-Type": "application/vnd.api+json",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/orgs", params={"limit": 1, "version": self._version})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Snyk API token validated")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid Snyk auth token")
                if resp.status_code == 403:
                    return HealthResult(ok=False, detail="Snyk token lacks required permissions")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Cannot connect to Snyk API")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "projects":
                    return await self._list_projects(c, q)
                case "project":
                    return await self._get_project(c, q)
                case "issues":
                    return await self._list_issues(c, q)
                case "aggregated_issues":
                    return await self._aggregated_issues(c, q)
                case "orgs":
                    return await self._list_orgs(c, q)
                case "tests":
                    return await self._list_tests(c, q)
                case _:
                    raise ValueError(f"Unsupported Snyk resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "test":
                    return await self._trigger_test(c, payload.data)
                case "ignore":
                    return await self._ignore_issue(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Snyk write resource: {payload.resource!r}")

    async def _list_projects(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org_id = q.filters.get("org_id")
        if not org_id:
            raise ValueError("Snyk projects query requires 'org_id' in filters")
        params: dict[str, Any] = {
            "version": self._version,
            "limit": str(q.limit),
        }
        if q.filters.get("names"):
            params["names"] = q.filters["names"]
        if q.filters.get("name"):
            params["name"] = q.filters["name"]
        if q.cursor:
            params["starting_after"] = q.cursor
        resp = await c.get(f"/orgs/{org_id}/projects", params=params)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(
            records=data,
            total=_meta_total(body, len(data)),
            next_cursor=_next_cursor(body),
        )

    async def _get_project(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org_id = q.filters.get("org_id")
        project_id = q.filters.get("project_id")
        if not org_id:
            raise ValueError("Snyk project query requires 'org_id' in filters")
        if not project_id:
            raise ValueError("Snyk project query requires 'project_id' in filters")
        params = {"version": self._version}
        resp = await c.get(f"/orgs/{org_id}/projects/{project_id}", params=params)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", {})
        return ConnectorResult(records=[data] if data else [])

    async def _list_issues(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org_id = q.filters.get("org_id")
        project_id = q.filters.get("project_id")
        if not org_id:
            raise ValueError("Snyk issues query requires 'org_id' in filters")
        if not project_id:
            raise ValueError("Snyk issues query requires 'project_id' in filters")
        params: dict[str, Any] = {
            "version": self._version,
            "limit": str(q.limit),
        }
        if q.filters.get("types"):
            params["type"] = q.filters["types"]
        if q.filters.get("status"):
            params["status"] = q.filters["status"]
        if q.filters.get("severity"):
            params["severity"] = q.filters["severity"]
        if q.cursor:
            params["starting_after"] = q.cursor
        resp = await c.get(f"/orgs/{org_id}/projects/{project_id}/issues", params=params)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(
            records=data,
            total=_meta_total(body, len(data)),
            next_cursor=_next_cursor(body),
        )

    async def _aggregated_issues(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org_id = q.filters.get("org_id")
        if not org_id:
            raise ValueError("Snyk aggregated_issues query requires 'org_id' in filters")
        params = {"version": self._version}
        packages = q.filters.get("packages", [])
        if not packages:
            raise ValueError(
                "Snyk aggregated_issues query requires 'packages' in filters (list of {name, version, ecosystem})",
            )
        body_payload: dict[str, Any] = {"data": {"attributes": {"packages": packages}}}
        resp = await c.post(f"/orgs/{org_id}/packages/issues", params=params, json=body_payload)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(records=data, total=len(data))

    async def _list_orgs(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {
            "version": self._version,
            "limit": str(q.limit),
        }
        if q.cursor:
            params["starting_after"] = q.cursor
        resp = await c.get("/orgs", params=params)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(
            records=data,
            total=_meta_total(body, len(data)),
            next_cursor=_next_cursor(body),
        )

    async def _list_tests(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        org_id = q.filters.get("org_id")
        if not org_id:
            raise ValueError("Snyk tests query requires 'org_id' in filters")
        params: dict[str, Any] = {
            "version": self._version,
            "limit": str(q.limit),
        }
        if q.cursor:
            params["starting_after"] = q.cursor
        resp = await c.get(f"/orgs/{org_id}/tests", params=params)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(
            records=data,
            total=_meta_total(body, len(data)),
            next_cursor=_next_cursor(body),
        )

    async def _trigger_test(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        org_id = data.get("org_id")
        if not org_id:
            raise ValueError("Snyk test write requires 'org_id' in data")
        name = data.get("name")
        version = data.get("version")
        ecosystem = data.get("ecosystem")
        if not all([name, version, ecosystem]):
            raise ValueError("Snyk test write requires 'name', 'version', and 'ecosystem' in data")
        params = {"version": self._version}
        body_payload: dict[str, Any] = {
            "data": {
                "attributes": {
                    "name": name,
                    "version": version,
                    "ecosystem": ecosystem,
                },
            },
        }
        resp = await c.post(f"/orgs/{org_id}/tests", params=params, json=body_payload)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _ignore_issue(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        org_id = data.get("org_id")
        project_id = data.get("project_id")
        issue_id = data.get("issue_id")
        if not org_id:
            raise ValueError("Snyk ignore write requires 'org_id' in data")
        if not project_id:
            raise ValueError("Snyk ignore write requires 'project_id' in data")
        if not issue_id:
            raise ValueError("Snyk ignore write requires 'issue_id' in data")
        reason = data.get("reason", "")
        reason_type = data.get("reason_type", "temporary-ignore")
        params = {"version": self._version}
        body_payload: dict[str, Any] = {
            "data": {
                "attributes": {
                    "reason": reason,
                    "reason_type": reason_type,
                    "ignore_type": "ignore",
                },
            },
        }
        resp = await c.post(
            f"/orgs/{org_id}/projects/{project_id}/issues/{issue_id}/ignore",
            params=params,
            json=body_payload,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

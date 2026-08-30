"""TrivyConnector — async Trivy REST API connector for vulnerability scanning.

Trivy server mode endpoints:
  - POST /trivy/v1/artifact   — scan an image, filesystem, or repository
  - GET  /trivy/v1/reports    — list scan reports
  - GET  /trivy/v1/reports/{digest} — get a single report
  - GET  /trivy/v1/plugins    — list installed plugins
  - GET  /trivy/v1/health     — server health check
"""

import asyncio
from typing import Any

import httpx

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


class TrivyConnector(ConnectorBase):
    """Read-only vulnerability scanner via the Trivy REST API.

    Supported query resources:
      "artifact"  — scan an image/filesystem/repo  (POST /trivy/v1/artifact)
      "reports"   — list scan reports               (GET /trivy/v1/reports)
      "report"    — get a single report by digest    (GET /trivy/v1/reports/{digest})
      "status"    — server health                    (GET /trivy/v1/health)
      "plugins"   — list installed plugins           (GET /trivy/v1/plugins)

    Supported write resources:
      "scan"     — trigger an artifact scan  (POST /trivy/v1/artifact)

    NOTE — the default ``base_url`` is loopback, which the outbound SSRF guard
    blocks unless the operator opts in with
    ``SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128`` (both entries: ``localhost``
    resolves to IPv4 and IPv6 on dual-stack hosts). Without the opt-in, building
    the client raises ``ValueError`` naming the blocked address, and
    ``health_check`` reports it as unhealthy. See
    ``docs/configuration-reference.md`` → "Outbound Egress Guard (SSRF)".
    """

    def __init__(self, token: str = "", base_url: str = "http://localhost:8080") -> None:  # nosec B107
        self._token = token
        self._base_url = base_url.rstrip("/")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.TRIVY

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        validate_outbound_url(self._base_url)
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers(), timeout=60)

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/trivy/v1/health", timeout=10)
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Trivy server is healthy")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid Trivy auth token")
                if resp.status_code == 403:
                    return HealthResult(ok=False, detail="Trivy token lacks required permissions")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Cannot connect to Trivy server")
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Trivy server timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "artifact":
                    return await self._scan_artifact(c, q)
                case "reports":
                    return await self._list_reports(c, q)
                case "report":
                    return await self._get_report(c, q)
                case "status":
                    return await self._get_status(c, q)
                case "plugins":
                    return await self._list_plugins(c, q)
                case _:
                    raise ValueError(f"Unsupported Trivy resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "scan":
                    return await self._trigger_scan(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Trivy write resource: {payload.resource!r}")

    async def _scan_artifact(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        body: dict[str, Any] = {}
        if "image" in q.filters:
            body = {"image": q.filters["image"]}
        elif "filesystem" in q.filters:
            body = {"filesystem": q.filters["filesystem"]}
        elif "repository" in q.filters:
            body = {"repository": q.filters["repository"]}
        if not body:
            raise ValueError("Trivy artifact query requires one of 'image', 'filesystem', or 'repository' in filters")
        params: dict[str, Any] = {}
        if q.filters.get("scan_options"):
            body["scan_options"] = q.filters["scan_options"]
        if q.filters.get("timeout"):
            params["timeout"] = q.filters["timeout"]
        resp = await c.post("/trivy/v1/artifact", params=params, json=body)
        resp.raise_for_status()
        data: list[dict[str, Any]] | dict[str, Any] = resp.json()
        records = data if isinstance(data, list) else [data]
        return ConnectorResult(records=records, total=len(records))

    async def _list_reports(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit and q.limit < 1000:
            params["limit"] = str(q.limit)
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/trivy/v1/reports", params=params)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = body if isinstance(body, list) else body.get("reports", [])
        return ConnectorResult(records=data, total=len(data))

    async def _get_report(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        digest = q.filters.get("digest")
        if not digest:
            raise ValueError("Trivy report query requires 'digest' in filters")
        resp = await c.get(f"/trivy/v1/reports/{digest}")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return ConnectorResult(records=[data])

    async def _get_status(self, c: httpx.AsyncClient, _q: ConnectorQuery) -> ConnectorResult:
        resp = await c.get("/trivy/v1/health")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return ConnectorResult(records=[data])

    async def _list_plugins(self, c: httpx.AsyncClient, _q: ConnectorQuery) -> ConnectorResult:
        resp = await c.get("/trivy/v1/plugins")
        resp.raise_for_status()
        data: list[dict[str, Any]] = resp.json()
        return ConnectorResult(records=data, total=len(data))

    async def _trigger_scan(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if "image" in data:
            body = {"image": data["image"]}
        elif "filesystem" in data:
            body = {"filesystem": data["filesystem"]}
        elif "repository" in data:
            body = {"repository": data["repository"]}
        if not body:
            raise ValueError("Trivy scan write requires one of 'image', 'filesystem', or 'repository' in data")
        if data.get("scan_options"):
            body["scan_options"] = data["scan_options"]
        params: dict[str, Any] = {}
        if data.get("timeout"):
            params["timeout"] = data["timeout"]
        resp = await c.post("/trivy/v1/artifact", params=params, json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

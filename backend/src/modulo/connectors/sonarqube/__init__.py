"""SonarQubeConnector — async SonarQube REST API connector."""

import asyncio
from typing import Any

import httpx

from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.connectors._safe_page import safe_paging_total as _safe_paging_total
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.security import CredentialRedactor, redacting
from modulo.core.ssrf import validate_outbound_url

_RATE_LIMITED_STATUS = 429


def _next_page_cursor(body: dict[str, Any], limit: int) -> str | None:
    paging = body.get("paging", {}) if isinstance(body, dict) else {}
    if not isinstance(paging, dict):
        paging = {}
    page_index = _safe_int(paging.get("pageIndex"), 1)
    total = _safe_int(paging.get("total"), 0)
    if total > limit * page_index:
        return str(page_index + 1)
    return None


class SonarQubeConnector(ConnectorBase):
    """SonarQube code-quality connector.

    NOTE — the default ``base_url`` is loopback, which the outbound SSRF guard
    blocks unless the operator opts in with
    ``SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128`` (both entries: ``localhost``
    resolves to IPv4 and IPv6 on dual-stack hosts). Without the opt-in, building
    the client raises ``ValueError`` naming the blocked address, and
    ``health_check`` reports it as unhealthy. See
    ``docs/configuration-reference.md`` → "Outbound Egress Guard (SSRF)".
    """

    def __init__(self, token: str, base_url: str = "http://localhost:9000") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._api_base = f"{self._base_url}/api"
        self._redactor = CredentialRedactor([token])

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SONARQUBE

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _client(self) -> httpx.AsyncClient:
        validate_outbound_url(self._base_url)
        return httpx.AsyncClient(base_url=self._api_base, headers=self._headers(), timeout=30)

    @redacting
    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/system/health", timeout=10)
                if resp.status_code == _RATE_LIMITED_STATUS:
                    retry_after = resp.headers.get("Retry-After", "unknown")
                    return HealthResult(ok=False, detail=f"Rate limited; retry after {retry_after}s")
                resp.raise_for_status()
                body = resp.json()
                status_text = body.get("health", "")
                if status_text in ("GREEN", "YELLOW"):
                    return HealthResult(ok=True, detail=f"SonarQube health: {status_text}")
                return HealthResult(ok=False, detail=f"SonarQube health: {status_text}")
        except httpx.HTTPStatusError as e:
            return HealthResult(
                ok=False, detail=self._redactor.redact(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return HealthResult(ok=False, detail=self._redactor.redact(str(e)))

    @redacting
    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "projects":
                    return await self._list_projects(c, q)
                case "project_analyses":
                    return await self._list_project_analyses(c, q)
                case "measures":
                    return await self._get_measures(c, q)
                case "issues":
                    return await self._search_issues(c, q)
                case "quality_gates":
                    return await self._list_quality_gates(c, q)
                case "quality_gate":
                    return await self._get_quality_gate(c, q)
                case "metrics":
                    return await self._list_metrics(c, q)
                case "plugins":
                    return await self._list_plugins(c, q)
                case "hotspots":
                    return await self._search_hotspots(c, q)
                case _:
                    raise ValueError(f"Unsupported SonarQube resource: {q.resource!r}")

    @redacting
    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "issue_comment":
                    return await self._add_issue_comment(c, payload.data)
                case "issue_status":
                    return await self._transition_issue(c, payload.data)
                case "gate":
                    return await self._create_quality_gate(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported SonarQube write resource: {payload.resource!r}")

    async def _list_projects(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("search"):
            params["search"] = q.filters["search"]
        if q.filters.get("project"):
            params["project"] = q.filters["project"]
        if q.filters.get("qualifier"):
            params["qualifier"] = q.filters["qualifier"]
        if q.filters.get("analyzedBefore"):
            params["analyzedBefore"] = q.filters["analyzedBefore"]
        if q.filters.get("onProvisionedOnly"):
            params["onProvisionedOnly"] = q.filters["onProvisionedOnly"]
        params["ps"] = str(q.limit)
        if q.cursor:
            params["p"] = q.cursor
        resp = await c.get("/projects/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "components")
        return ConnectorResult(
            records=records,
            total=_safe_paging_total(body, "paging", "total"),
            next_cursor=_next_page_cursor(body, q.limit),
        )

    async def _list_project_analyses(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        project = q.filters.get("project")
        if not project:
            raise ValueError("SonarQube project_analyses query requires 'project' filter")
        params: dict[str, Any] = {"project": project}
        if q.filters.get("from"):
            params["from"] = q.filters["from"]
        if q.filters.get("to"):
            params["to"] = q.filters["to"]
        if q.filters.get("branch"):
            params["branch"] = q.filters["branch"]
        if q.filters.get("category"):
            params["category"] = q.filters["category"]
        params["ps"] = str(q.limit)
        if q.cursor:
            params["p"] = q.cursor
        resp = await c.get("/project_analyses/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "analyses")
        return ConnectorResult(
            records=records,
            total=_safe_paging_total(body, "paging", "total"),
            next_cursor=_next_page_cursor(body, q.limit),
        )

    async def _get_measures(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        component = q.filters.get("component")
        if not component:
            raise ValueError("SonarQube measures query requires 'component' filter")
        metric_keys = q.filters.get("metricKeys")
        if not metric_keys:
            raise ValueError("SonarQube measures query requires 'metricKeys' filter")
        params: dict[str, Any] = {
            "component": component,
            "metricKeys": metric_keys,
        }
        if q.filters.get("branch"):
            params["branch"] = q.filters["branch"]
        if q.filters.get("pullRequest"):
            params["pullRequest"] = q.filters["pullRequest"]
        resp = await c.get("/measures/component", params=params)
        resp.raise_for_status()
        body = resp.json()
        component = body.get("component", {}) if isinstance(body, dict) else {}
        component = component if isinstance(component, dict) else {}
        measures = component.get("measures", [])
        measures = measures if isinstance(measures, list) else []
        return ConnectorResult(records=measures, total=len(measures))

    async def _search_issues(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("component"):
            params["component"] = q.filters["component"]
        if q.filters.get("status"):
            params["status"] = q.filters["status"]
        if q.filters.get("types"):
            params["types"] = q.filters["types"]
        if q.filters.get("severities"):
            params["severities"] = q.filters["severities"]
        if q.filters.get("resolved"):
            params["resolved"] = q.filters["resolved"]
        if q.filters.get("branch"):
            params["branch"] = q.filters["branch"]
        if q.filters.get("sansTop25"):
            params["sansTop25"] = q.filters["sansTop25"]
        if q.filters.get("tags"):
            params["tags"] = q.filters["tags"]
        if q.filters.get("assignee"):
            params["assignee"] = q.filters["assignee"]
        if q.filters.get("createdAfter"):
            params["createdAfter"] = q.filters["createdAfter"]
        if q.filters.get("createdBefore"):
            params["createdBefore"] = q.filters["createdBefore"]
        params["ps"] = str(q.limit)
        if q.cursor:
            params["p"] = q.cursor
        resp = await c.get("/issues/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "issues")
        return ConnectorResult(
            records=records,
            total=_safe_paging_total(body, "paging", "total"),
            next_cursor=_next_page_cursor(body, q.limit),
        )

    async def _list_quality_gates(self, c: httpx.AsyncClient, _q: ConnectorQuery) -> ConnectorResult:
        resp = await c.get("/qualitygates/list")
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "qualitygates")
        return ConnectorResult(
            records=records,
            total=len(records),
        )

    async def _get_quality_gate(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        gate_id = q.filters.get("id")
        if not gate_id:
            raise ValueError("SonarQube quality_gate query requires 'id' filter")
        params: dict[str, Any] = {"id": str(gate_id)}
        resp = await c.get("/qualitygates/show", params=params)
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[body])

    async def _list_metrics(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("search"):
            params["search"] = q.filters["search"]
        params["ps"] = str(q.limit)
        if q.cursor:
            params["p"] = q.cursor
        resp = await c.get("/metrics/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "metrics")
        return ConnectorResult(
            records=records,
            total=_safe_paging_total(body, "paging", "total"),
            next_cursor=_next_page_cursor(body, q.limit),
        )

    async def _list_plugins(self, c: httpx.AsyncClient, _q: ConnectorQuery) -> ConnectorResult:
        resp = await c.get("/plugins/installed")
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "plugins")
        return ConnectorResult(
            records=records,
            total=len(records),
        )

    async def _search_hotspots(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        project = q.filters.get("project")
        if not project:
            raise ValueError("SonarQube hotspots query requires 'project' filter")
        params: dict[str, Any] = {"project": project}
        if q.filters.get("branch"):
            params["branch"] = q.filters["branch"]
        if q.filters.get("status"):
            params["status"] = q.filters["status"]
        if q.filters.get("resolution"):
            params["resolution"] = q.filters["resolution"]
        if q.filters.get("p"):
            params["p"] = q.filters["p"]
        params["ps"] = str(q.limit)
        if q.cursor:
            params["p"] = q.cursor
        resp = await c.get("/hotspots/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "hotspots")
        return ConnectorResult(
            records=records,
            total=_safe_paging_total(body, "paging", "total"),
            next_cursor=_next_page_cursor(body, q.limit),
        )

    async def _add_issue_comment(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        issue = data.get("issue")
        text = data.get("text")
        if not issue or not text:
            raise ValueError("SonarQube issue_comment write requires 'issue' and 'text' in data")
        params: dict[str, Any] = {"issue": issue, "text": text}
        resp = await c.post("/issues/add_comment", params=params)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

    async def _transition_issue(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        issue = data.get("issue")
        transition = data.get("transition")
        if not issue or not transition:
            raise ValueError("SonarQube issue_status write requires 'issue' and 'transition' in data")
        valid_transitions = {"confirm", "resolve", "reopen", "falsepositive", "wontfix"}
        if transition not in valid_transitions:
            transitions_str = ", ".join(sorted(valid_transitions))
            raise ValueError(
                f"Invalid SonarQube transition {transition!r}. Must be one of: {transitions_str}",
            )
        params: dict[str, Any] = {"issue": issue, "transition": transition}
        resp = await c.post("/issues/do_transition", params=params)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

    async def _create_quality_gate(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("name")
        if not name:
            raise ValueError("SonarQube gate write requires 'name' in data")
        params: dict[str, Any] = {"name": name}
        resp = await c.post("/qualitygates/create", params=params)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

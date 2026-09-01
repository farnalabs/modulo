"""TeamCity CI/CD connector — triggers and observes builds via the TeamCity REST API."""

from typing import Any, cast

import httpx

from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    CIRun,
    CIRunLog,
    CIRunStatus,
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.core.ssrf import pinned_async_client_sync


def _parse_teamcity_status(state: str, status: str | None = None) -> CIRunStatus:
    match state:
        case "queued":
            return CIRunStatus.QUEUED
        case "running":
            return CIRunStatus.IN_PROGRESS
        case "finished" if status == "SUCCESS":
            return CIRunStatus.SUCCESS
        case "finished" if status in ("FAILURE", "ERROR"):
            return CIRunStatus.FAILURE
        case _:
            return CIRunStatus.UNKNOWN


class TeamCityConnector(ConnectorBase):
    """TeamCity CI/CD connector using the TeamCity REST API.

    Authenticates via Bearer token.
    Connects to a configurable base_url (default http://localhost:8111).

    NOTE — that default is loopback, which the outbound SSRF guard blocks unless
    the operator opts in with ``SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128``
    (both entries: ``localhost`` resolves to IPv4 and IPv6 on dual-stack hosts).
    Without the opt-in, building the client raises ``ValueError`` naming the
    blocked address, and ``health_check`` reports it as unhealthy. See
    ``docs/configuration-reference.md`` → "Outbound Egress Guard (SSRF)".
    """

    def __init__(self, token: str, base_url: str = "http://localhost:8111") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.TEAMCITY

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-520): validate + resolve the base_url's host
        # synchronously and pin the validated IP onto the transport, so the
        # connection never re-resolves the host at connect time (closes the
        # DNS-rebinding window). ``trust_env=False`` stops a proxy from
        # re-resolving the destination server-side and defeating the pin.
        return pinned_async_client_sync(
            self._base_url,
            base_url=self._base_url,
            headers=self._auth_header(),
            timeout=30,
        )

    def _run_from_build(self, data: dict[str, Any], *, fallback_run_id: str = "") -> CIRun:
        """Build a :class:`CIRun` from a TeamCity build resource."""
        state = data.get("state", "")
        raw_status = data.get("status")
        status = _parse_teamcity_status(state, raw_status)
        href = data.get("href", "")
        bt = data.get("buildType")
        build_type_id = cast("str", bt.get("buildTypeId", bt.get("id", ""))) if isinstance(bt, dict) else ""
        response_id = data.get("id")
        duration = data.get("duration")
        return CIRun(
            id=str(response_id if response_id is not None else fallback_run_id),
            pipeline_id=build_type_id,
            status=status,
            url=f"{self._base_url}{href}" if href else "",
            branch=data.get("branchName", ""),
            commit_sha=data.get("revision", ""),
            created_at=str(data.get("startDate", "")),
            updated_at=str(data.get("finishDate", "")),
            duration_seconds=_safe_int(duration) if duration is not None else None,
            triggered_by="",
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/app/rest/server")
            if r.status_code == 200:
                return HealthResult(ok=True)
            if r.status_code in (401, 403):
                return HealthResult(ok=False, detail="Authentication failed: invalid token")
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"TeamCity API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="TeamCity API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="TeamCity API connection error")
        except ValueError as exc:
            # The outbound SSRF guard in ``_client`` rejects a private/internal
            # base_url by raising — and the default base_url is loopback. A health
            # check must REPORT that as unhealthy (surfacing the guard's
            # remediation text), never propagate it.
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        body: dict[str, Any] = {"buildTypeId": pipeline_id}
        if branch:
            body["branchName"] = branch
        if variables:
            body["properties"] = {"property": [{"name": k, "value": v} for k, v in variables.items()]}
        async with self._client() as client:
            r = await client.post("/app/rest/buildQueue", json=body)
            r.raise_for_status()
            data = r.json()
            build_id = str(data.get("id", ""))
            href = data.get("href", "")
            return CIRun(
                id=build_id,
                pipeline_id=pipeline_id,
                status=CIRunStatus.QUEUED,
                url=f"{self._base_url}{href}" if href else "",
                branch=branch,
            )

    async def get_run_status(self, run_id: str) -> CIRun:
        async with self._client() as client:
            r = await client.get(f"/app/rest/builds/id:{run_id}")
            r.raise_for_status()
            data = r.json()
        return self._run_from_build(data, fallback_run_id=run_id)

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        async with self._client() as client:
            r = await client.get(f"/app/rest/builds/id:{run_id}/text")
            r.raise_for_status()
            text = r.text
            lines = text.splitlines()
            start = int(cursor) if cursor and cursor.isdigit() else 0
            return CIRunLog(
                run_id=run_id,
                lines=lines[start:],
                next_cursor=str(len(lines)) if start < len(lines) else None,
            )

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        locator_parts: list[str] = []
        if pipeline_id:
            locator_parts.append(f"buildType:{pipeline_id}")
        locator_parts.append(f"count:{limit}")
        locator = ",".join(locator_parts)
        async with self._client() as client:
            r = await client.get("/app/rest/builds", params={"locator": locator})
            r.raise_for_status()
            data = r.json()
            builds: list[dict[str, Any]] = _safe_records(data, "build")
            runs = [self._run_from_build(b) for b in builds]
            if status:
                runs = [r for r in runs if r.status == status]
            return runs

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "projects":
                async with self._client() as client:
                    r = await client.get("/app/rest/projects")
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "project")
                    return ConnectorResult(records=records, total=len(records))
            case "buildTypes":
                project_id = q.filters.get("project_id", "")
                locator = f"project:{project_id}" if project_id else ""
                async with self._client() as client:
                    params = {"locator": locator} if locator else {}
                    r = await client.get("/app/rest/buildTypes", params=params)
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "buildType")
                    return ConnectorResult(records=records, total=len(records))
            case "builds":
                build_type_id = q.filters.get("buildTypeId", "")
                locator_parts: list[str] = []
                if build_type_id:
                    locator_parts.append(f"buildType:{build_type_id}")
                locator_parts.append(f"count:{q.limit}")
                locator = ",".join(locator_parts)
                async with self._client() as client:
                    r = await client.get("/app/rest/builds", params={"locator": locator})
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "build")
                    return ConnectorResult(records=records, total=len(records))
            case "agents":
                async with self._client() as client:
                    r = await client.get("/app/rest/agents")
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "agent")
                    return ConnectorResult(records=records, total=len(records))
            case _:
                raise ValueError(f"Unsupported query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "build":
                build_type_id = payload.data.get("buildTypeId", "")
                branch = payload.data.get("branch", "")
                variables = payload.data.get("parameters")
                body: dict[str, Any] = {"buildTypeId": build_type_id}
                if branch:
                    body["branchName"] = branch
                if variables:
                    body["properties"] = {"property": [{"name": k, "value": v} for k, v in variables.items()]}
                async with self._client() as client:
                    r = await client.post("/app/rest/buildQueue", json=body)
                    r.raise_for_status()
                    data = r.json()
                    return {"id": str(data.get("id", "")), "buildTypeId": build_type_id}
            case "buildType":
                build_type_id = payload.data.get("buildTypeId", "")
                project_id = payload.data.get("projectId", "")
                name = payload.data.get("name", "")
                if not build_type_id or not project_id or not name:
                    raise ValueError("buildType write requires buildTypeId, projectId, and name")
                body = {
                    "id": build_type_id,
                    "name": name,
                    "project": {"id": project_id},
                }
                async with self._client() as client:
                    r = await client.post("/app/rest/buildTypes", json=body)
                    r.raise_for_status()
                    data = r.json()
                    return {"id": data.get("id", ""), "name": data.get("name", "")}
            case _:
                raise ValueError(f"Unsupported write resource: {payload.resource!r}")


class _TeamCityTestDouble(TeamCityConnector):
    """Minimal test double that does not make HTTP calls."""

    def __init__(self) -> None:
        import uuid as _uuid

        self._uuid = _uuid
        self._token = "test"  # nosec - test double, not a real credential
        self._base_url = "http://teamcity.example.com"
        self._builds: list[dict[str, Any]] = []
        self._projects: list[dict[str, Any]] = []
        self._build_types: list[dict[str, Any]] = []

    def _client(self) -> httpx.AsyncClient:
        raise RuntimeError("Test double has no HTTP client")

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        run = CIRun(
            id=f"{self._uuid.uuid4()}",
            pipeline_id=pipeline_id,
            status=CIRunStatus.QUEUED,
            branch=branch,
        )
        self._builds.append({"run": run, "variables": variables or {}})
        return run

    async def get_run_status(self, run_id: str) -> CIRun:
        return CIRun(
            id=run_id,
            pipeline_id="my-build-type",
            status=CIRunStatus.SUCCESS,
        )

    async def get_run_logs(self, run_id: str, _cursor: str | None = None) -> CIRunLog:
        return CIRunLog(run_id=run_id, lines=["line1", "line2"])

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        _limit: int = 20,
    ) -> list[CIRun]:
        return [
            CIRun(
                id=f"{self._uuid.uuid4()}",
                pipeline_id=pipeline_id or "my-build-type",
                status=status or CIRunStatus.SUCCESS,
            ),
        ]

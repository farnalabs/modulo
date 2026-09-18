"""CircleCI CI runner — triggers and observes pipeline runs via the CircleCI API v2."""

from typing import Any, cast

import httpx

from modulo.connectors._safe_cursor import safe_cursor as _safe_cursor
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    CIRun,
    CIRunLog,
    CIRunStatus,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    HealthResult,
)
from modulo.connectors.ci_runner.base import CIRunnerBase
from modulo.core.ssrf import pinned_async_client_sync

_CIRCLECI_API = "https://circleci.com/api/v2"

_STATUS_MAP: dict[str, CIRunStatus] = {
    "created": CIRunStatus.PENDING,
    "pending": CIRunStatus.PENDING,
    "queued": CIRunStatus.QUEUED,
    "running": CIRunStatus.IN_PROGRESS,
    "success": CIRunStatus.SUCCESS,
    "failed": CIRunStatus.FAILURE,
    "error": CIRunStatus.FAILURE,
    "canceled": CIRunStatus.CANCELLED,
    "not_running": CIRunStatus.CANCELLED,
    "infrastructure_fail": CIRunStatus.FAILURE,
    "timedout": CIRunStatus.TIMED_OUT,
    "on_hold": CIRunStatus.PENDING,
    "blocked": CIRunStatus.PENDING,
    "no_tests": CIRunStatus.SUCCESS,
}


class CircleCIConnector(CIRunnerBase):
    """CircleCI CI runner using the CircleCI REST API v2.

    Requires a CircleCI personal API token.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Circle-Token": self._token,
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return pinned_async_client_sync(_CIRCLECI_API, base_url=_CIRCLECI_API, headers=self._headers(), timeout=30)

    def _parse_run(self, raw: dict[str, Any]) -> CIRun:
        raw_id = raw.get("id")
        raw_state = raw.get("state", "")
        status = _STATUS_MAP.get(raw_state, CIRunStatus.UNKNOWN)
        vcs = raw.get("vcs")
        trigger = raw.get("trigger")
        actor = trigger.get("actor") if isinstance(trigger, dict) else None
        pipeline_number = raw.get("number", "")
        project_slug = raw.get("project_slug", "")
        pipeline_url = (
            f"https://app.circleci.com/pipelines/{project_slug}/{pipeline_number}"
            if project_slug and pipeline_number
            else ""
        )
        return CIRun(
            id=str(raw_id) if raw_id is not None else "",
            pipeline_id=project_slug,
            status=status,
            url=pipeline_url,
            branch=vcs.get("branch", "") if isinstance(vcs, dict) else "",
            commit_sha=vcs.get("revision", "") if isinstance(vcs, dict) else "",
            created_at=raw.get("created_at", ""),
            updated_at="",
            duration_seconds=None,
            triggered_by=actor.get("login", "") if isinstance(actor, dict) else "",
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/me")
            if r.status_code == 200:
                return HealthResult(ok=True)
            if r.status_code in (401, 403):
                return HealthResult(ok=False, detail="Authentication failed: invalid or expired token")
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"CircleCI API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="CircleCI API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="CircleCI API connection error")

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        project_slug = pipeline_id
        body: dict[str, Any] = {"branch": branch or "main"}
        if variables:
            body["parameters"] = variables

        async with self._client() as client:
            r = await client.post(f"/project/{project_slug}/pipeline", json=body)
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            return self._parse_run(data)

    async def get_run_status(self, run_id: str) -> CIRun:
        async with self._client() as client:
            r = await client.get(f"/pipeline/{run_id}")
            r.raise_for_status()
            return self._parse_run(r.json())

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        async with self._client() as client:
            all_lines = await self._collect_log_lines(client, run_id)
            return CIRunLog(
                run_id=run_id,
                lines=all_lines,
                next_cursor=str(len(all_lines)) if cursor else None,
            )

    async def _collect_log_lines(
        self,
        client: httpx.AsyncClient,
        run_id: str,
    ) -> list[str]:
        all_lines: list[str] = []
        for wf in await self._fetch_workflows(client, run_id):
            wf_name = wf.get("name", wf.get("id", ""))
            all_lines.append(f"--- Workflow: {wf_name} ---")
            for job in await self._fetch_workflow_jobs(client, wf.get("id", "")):
                all_lines.extend(await self._job_log_lines(client, job))
        return all_lines

    async def _fetch_workflows(
        self,
        client: httpx.AsyncClient,
        run_id: str,
    ) -> list[dict[str, Any]]:
        wf_r = await client.get(f"/pipeline/{run_id}/workflow")
        wf_r.raise_for_status()
        return _safe_records(wf_r.json(), "items")

    async def _fetch_workflow_jobs(
        self,
        client: httpx.AsyncClient,
        wf_id: str,
    ) -> list[dict[str, Any]]:
        job_r = await client.get(f"/workflow/{wf_id}/job")
        job_r.raise_for_status()
        return _safe_records(job_r.json(), "items")

    async def _job_log_lines(
        self,
        client: httpx.AsyncClient,
        job: dict[str, Any],
    ) -> list[str]:
        job_number = job.get("job_number")
        project_slug = job.get("project_slug", "")
        if not job_number or not project_slug:
            return []
        out_r = await client.get(
            f"/project/{project_slug}/{job_number}/outputs",
        )
        if out_r.status_code != 200:
            return []
        outputs = _safe_records(out_r.json(), "items")
        job_name = job.get("name", "")
        lines = [f"  Job: {job_name} (#{job_number})"]
        for out in outputs:
            msg = out.get("message", "") or ""
            lines.extend(f"    {line}" for line in msg.splitlines())
        return lines

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        params: dict[str, Any] = {}
        project_slug = pipeline_id or ""

        if status:
            status_map: dict[CIRunStatus, str] = {
                CIRunStatus.PENDING: "created",
                CIRunStatus.QUEUED: "queued",
                CIRunStatus.IN_PROGRESS: "running",
                CIRunStatus.SUCCESS: "success",
                CIRunStatus.FAILURE: "failed",
                CIRunStatus.CANCELLED: "canceled",
                CIRunStatus.TIMED_OUT: "timedout",
            }
            cc_status = status_map.get(status)
            if cc_status:
                params["status"] = cc_status

        async with self._client() as client:
            r = await client.get(f"/project/{project_slug}/pipeline", params=params)
            r.raise_for_status()
            data: list[dict[str, Any]] = _safe_records(r.json(), "items")
            return [self._parse_run(item) for item in data[:limit]]

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "pipelines":
                slug = q.filters.get("slug", "")
                params = {"page-token": q.cursor} if q.cursor else {}
                async with self._client() as client:
                    r = await client.get(f"/project/{slug}/pipeline", params=params)
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "items")
                    return ConnectorResult(
                        records=records,
                        next_cursor=_safe_cursor(data.get("next_page_token") if isinstance(data, dict) else None),
                        total=len(records),
                    )
            case "workflows":
                pipeline_uuid = q.filters.get("pipeline_id", "")
                async with self._client() as client:
                    r = await client.get(f"/pipeline/{pipeline_uuid}/workflow")
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "items")
                    return ConnectorResult(
                        records=records,
                        next_cursor=_safe_cursor(data.get("next_page_token") if isinstance(data, dict) else None),
                        total=len(records),
                    )
            case "jobs":
                workflow_id = q.filters.get("workflow_id", "")
                async with self._client() as client:
                    r = await client.get(f"/workflow/{workflow_id}/job")
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "items")
                    return ConnectorResult(
                        records=records,
                        next_cursor=_safe_cursor(data.get("next_page_token") if isinstance(data, dict) else None),
                        total=len(records),
                    )
            case "runs":
                slug = q.filters.get("slug", "")
                async with self._client() as client:
                    r = await client.get(f"/project/{slug}/pipeline")
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "items")
                    return ConnectorResult(
                        records=records,
                        next_cursor=_safe_cursor(data.get("next_page_token") if isinstance(data, dict) else None),
                        total=len(records),
                    )
            case _:
                raise ValueError(f"Unsupported query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        if payload.resource != "trigger_pipeline":
            raise ValueError(f"Unsupported write resource: {payload.resource!r}")
        project_slug = payload.data.get("project_slug", "")
        branch = payload.data.get("branch", "main")
        variables = payload.data.get("parameters")
        body: dict[str, Any] = {"branch": branch}
        if variables:
            body["parameters"] = variables
        async with self._client() as client:
            r = await client.post(f"/project/{project_slug}/pipeline", json=body)
            r.raise_for_status()
            return cast("dict[str, Any]", r.json())


class _CircleCITestDouble(CircleCIConnector):
    """Minimal test double that does not make HTTP calls."""

    def __init__(self) -> None:
        import uuid as _uuid

        self._token = "cct_test"  # nosec - test double, not a real credential
        self._uuid = _uuid
        self._status: CIRunStatus = CIRunStatus.QUEUED
        self._run_logs: list[str] = []
        self._triggered: list[dict[str, Any]] = []

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
        self._triggered.append({"run": run, "variables": variables or {}})
        self._status = CIRunStatus.QUEUED
        return run

    async def get_run_status(self, run_id: str) -> CIRun:
        return CIRun(
            id=run_id,
            pipeline_id="gh/owner/repo",
            status=self._status,
        )

    async def get_run_logs(self, run_id: str, _cursor: str | None = None) -> CIRunLog:
        return CIRunLog(run_id=run_id, lines=self._run_logs)

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        _limit: int = 20,
    ) -> list[CIRun]:
        return [
            CIRun(
                id="pipeline-uuid-1",
                pipeline_id=pipeline_id or "gh/owner/repo",
                status=status or CIRunStatus.SUCCESS,
            ),
        ]

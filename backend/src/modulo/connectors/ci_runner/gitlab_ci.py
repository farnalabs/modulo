"""GitLab CI runner — triggers and observes pipeline runs via the GitLab API."""

from typing import Any

import httpx

from modulo.connectors.base import CIRun, CIRunLog, CIRunStatus, HealthResult
from modulo.connectors.ci_runner.base import CIRunnerBase

_GITLAB_API_DEFAULT = "https://gitlab.com/api/v4"

_STATUS_MAP: dict[str, CIRunStatus] = {
    "created": CIRunStatus.PENDING,
    "waiting_for_resource": CIRunStatus.QUEUED,
    "preparing": CIRunStatus.QUEUED,
    "pending": CIRunStatus.PENDING,
    "running": CIRunStatus.IN_PROGRESS,
    "success": CIRunStatus.SUCCESS,
    "failed": CIRunStatus.FAILURE,
    "canceled": CIRunStatus.CANCELLED,
    "skipped": CIRunStatus.SUCCESS,
    "manual": CIRunStatus.PENDING,
    "scheduled": CIRunStatus.QUEUED,
}


class GitLabCIRunner(CIRunnerBase):
    """GitLab CI runner using the GitLab Pipeline, Job, and Trace APIs.

    Supports both gitlab.com and self-hosted GitLab instances.
    """

    def __init__(self, token: str, base_url: str = _GITLAB_API_DEFAULT) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "PRIVATE-TOKEN": self._token,
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers(), timeout=30)

    def _parse_run(self, raw: dict[str, Any]) -> CIRun:
        raw_status = raw.get("status", "")
        status = _STATUS_MAP.get(raw_status, CIRunStatus.UNKNOWN)
        return CIRun(
            id=str(raw.get("id", "")),
            pipeline_id=str(raw.get("project_id", "")),
            status=status,
            url=raw.get("web_url", ""),
            branch=raw.get("ref", ""),
            commit_sha=raw.get("sha", ""),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            duration_seconds=raw.get("duration"),
            triggered_by=raw.get("user", {}).get("username", ""),
        )

    async def health_check(self) -> HealthResult:
        async with self._client() as client:
            r = await client.get("/projects?per_page=1")
        if r.status_code == 200:
            return HealthResult(ok=True)
        if r.status_code in (401, 403):
            return HealthResult(ok=False, detail="Authentication failed: invalid or expired token")
        return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        project_id = pipeline_id
        body: dict[str, Any] = {"ref": branch or "main"}
        if variables:
            body["variables"] = [{"key": k, "value": v} for k, v in variables.items()]

        async with self._client() as client:
            r = await client.post(f"/projects/{project_id}/pipeline", json=body)
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            return self._parse_run(data)

    async def get_run_status(self, run_id: str) -> CIRun:
        project_id, _, pipeline_id = run_id.partition("/")
        if not pipeline_id:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'project_id/pipeline_id'.")
        async with self._client() as client:
            r = await client.get(f"/projects/{project_id}/pipelines/{pipeline_id}")
            r.raise_for_status()
            return self._parse_run(r.json())

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        project_id, _, pipeline_id = run_id.partition("/")
        if not pipeline_id:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'project_id/pipeline_id'.")
        async with self._client() as client:
            jobs_r = await client.get(
                f"/projects/{project_id}/pipelines/{pipeline_id}/jobs",
                params={"per_page": 100},
            )
            jobs_r.raise_for_status()
            jobs = jobs_r.json()

            all_lines: list[str] = []
            for job in jobs:
                job_id = job.get("id")
                trace_r = await client.get(
                    f"/projects/{project_id}/jobs/{job_id}/trace",
                    headers={"Accept": "text/plain"},
                )
                if trace_r.status_code == 200:
                    job_lines = trace_r.text.splitlines()
                    all_lines.append(f"--- Job {job.get('name', job_id)} ---")
                    all_lines.extend(job_lines)
                    all_lines.append("")

            return CIRunLog(
                run_id=run_id,
                lines=all_lines,
                next_cursor=str(len(all_lines)) if cursor else None,
            )

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        params: dict[str, Any] = {"per_page": limit, "order_by": "updated_at", "sort": "desc"}
        if status:
            status_map: dict[CIRunStatus, str] = {
                CIRunStatus.PENDING: "pending",
                CIRunStatus.QUEUED: "pending",
                CIRunStatus.IN_PROGRESS: "running",
                CIRunStatus.SUCCESS: "success",
                CIRunStatus.FAILURE: "failed",
                CIRunStatus.CANCELLED: "canceled",
            }
            gl_status = status_map.get(status)
            if gl_status:
                params["status"] = gl_status

        project_id = pipeline_id or ""

        async with self._client() as client:
            r = await client.get(f"/projects/{project_id}/pipelines", params=params)
            r.raise_for_status()
            raw_runs: list[dict[str, Any]] = r.json()
            return [self._parse_run(run) for run in raw_runs]


class _GitLabCITestDouble(GitLabCIRunner):
    """Minimal test double that does not make HTTP calls."""

    def __init__(self) -> None:
        import uuid as _uuid

        self._token = "glpat-test"  # nosec - test double, not a real credential
        self._uuid = _uuid
        self._base_url = _GITLAB_API_DEFAULT
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
            pipeline_id="12345",
            status=self._status,
        )

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        return CIRunLog(run_id=run_id, lines=self._run_logs)

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        return [
            CIRun(
                id="pipeline-1",
                pipeline_id=pipeline_id or "12345",
                status=status or CIRunStatus.SUCCESS,
            )
        ]

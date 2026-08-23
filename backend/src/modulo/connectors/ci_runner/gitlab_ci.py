"""GitLab CI runner — triggers and observes pipeline runs via the GitLab API."""

import logging
from typing import Any

import httpx

from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.connectors.base import CIRun, CIRunLog, CIRunStatus, HealthResult
from modulo.connectors.ci_runner.base import CIRunnerBase

logger = logging.getLogger(__name__)

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
        duration = raw.get("duration")
        user = raw.get("user")
        return CIRun(
            id=str(raw.get("id", "")),
            pipeline_id=str(raw.get("project_id", "")),
            status=status,
            url=raw.get("web_url", ""),
            branch=raw.get("ref", ""),
            commit_sha=raw.get("sha", ""),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            duration_seconds=_safe_int(duration) if duration is not None else None,
            triggered_by=user.get("username", "") if isinstance(user, dict) else "",
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/projects?per_page=1")
                if r.status_code == 200:
                    return HealthResult(ok=True)
                if r.status_code in (401, 403):
                    return HealthResult(ok=False, detail="Authentication failed: invalid or expired token")
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as exc:
            return HealthResult(ok=False, detail=f"HTTP error: {exc}")

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
        project_id = pipeline_id
        body: dict[str, Any] = {"ref": branch or "main"}
        if variables:
            body["variables"] = [{"key": k, "value": v} for k, v in variables.items()]

        try:
            async with self._client() as client:
                r = await client.post(f"/projects/{project_id}/pipeline", json=body)
                r.raise_for_status()
                data: dict[str, Any] = r.json()
                return self._parse_run(data)
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitLab API error ({exc.response.status_code}): {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"GitLab API connection error: {exc}") from exc

    async def get_run_status(self, run_id: str) -> CIRun:
        project_id, _, pipeline_id = run_id.partition("/")
        if not pipeline_id:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'project_id/pipeline_id'.")
        if not project_id:
            raise ValueError("project_id is required in run_id")
        try:
            async with self._client() as client:
                r = await client.get(f"/projects/{project_id}/pipelines/{pipeline_id}")
                r.raise_for_status()
                return self._parse_run(r.json())
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitLab API error ({exc.response.status_code}): {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"GitLab API connection error: {exc}") from exc

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        project_id, _, pipeline_id = run_id.partition("/")
        if not pipeline_id:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'project_id/pipeline_id'.")
        if not project_id:
            raise ValueError("project_id is required in run_id")
        try:
            async with self._client() as client:
                offset = int(cursor) if cursor and cursor.isdigit() else 0
                jobs_r = await client.get(
                    f"/projects/{project_id}/pipelines/{pipeline_id}/jobs",
                    params={"per_page": 100, "page": (offset // 100) + 1},
                )
                jobs_r.raise_for_status()
                jobs = jobs_r.json()

                all_lines: list[str] = []
                skipped_jobs = 0
                for job in jobs:
                    job_id = job.get("id")
                    if not job_id:
                        skipped_jobs += 1
                        logger.warning(
                            "Skipping job with missing id in pipeline %s (%d skipped so far)",
                            pipeline_id,
                            skipped_jobs,
                        )
                        continue
                    trace_r = await client.get(
                        f"/projects/{project_id}/jobs/{job_id}/trace",
                        headers={"Accept": "text/plain"},
                    )
                    if trace_r.status_code == 200:
                        job_lines = trace_r.text.splitlines()
                        all_lines.append(f"--- Job {job.get('name', job_id)} ---")
                        all_lines.extend(job_lines)
                        all_lines.append("")
                    else:
                        skipped_jobs += 1
                        logger.warning(
                            "Failed to fetch trace for job %s (HTTP %s) — skipping (%d skipped so far)",
                            job_id,
                            trace_r.status_code,
                            skipped_jobs,
                        )

                return CIRunLog(
                    run_id=run_id,
                    lines=all_lines,
                    next_cursor=str(offset + len(all_lines)) if cursor is not None else None,
                    truncated=skipped_jobs > 0,
                )
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitLab API error ({exc.response.status_code}): {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"GitLab API connection error: {exc}") from exc

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        if not pipeline_id:
            raise ValueError("pipeline_id is required for list_runs")
        params: dict[str, Any] = {"per_page": limit, "order_by": "updated_at", "sort": "desc"}
        if status:
            status_map: dict[CIRunStatus, str] = {
                CIRunStatus.PENDING: "pending",
                CIRunStatus.QUEUED: "pending",
                CIRunStatus.IN_PROGRESS: "running",
                CIRunStatus.SUCCESS: "success",
                CIRunStatus.FAILURE: "failed",
                CIRunStatus.CANCELLED: "canceled",
                CIRunStatus.TIMED_OUT: "canceled",
            }
            gl_status = status_map.get(status)
            if gl_status:
                params["status"] = gl_status
            elif status == CIRunStatus.UNKNOWN:
                logger.warning("Cannot filter by UNKNOWN status — returning all runs")
            else:
                logger.warning("No GitLab mapping for status %s — returning all runs", status)

        try:
            async with self._client() as client:
                r = await client.get(f"/projects/{pipeline_id}/pipelines", params=params)
                r.raise_for_status()
                raw_runs: list[dict[str, Any]] = r.json()
                return [self._parse_run(run) for run in raw_runs]
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitLab API error ({exc.response.status_code}): {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"GitLab API connection error: {exc}") from exc


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
                id="pipeline-1",
                pipeline_id=pipeline_id or "12345",
                status=status or CIRunStatus.SUCCESS,
            ),
        ]

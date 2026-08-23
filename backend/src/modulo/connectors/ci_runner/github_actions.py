"""GitHub Actions CI runner — triggers and observes workflow runs via the GitHub API."""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import CIRun, CIRunLog, CIRunStatus, HealthResult
from modulo.connectors.ci_runner.base import CIRunnerBase

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_API_VERSION = "2022-11-28"

_STATUS_MAP: dict[str, CIRunStatus] = {
    "queued": CIRunStatus.QUEUED,
    "in_progress": CIRunStatus.IN_PROGRESS,
    "completed": CIRunStatus.SUCCESS,
    "action_required": CIRunStatus.PENDING,
    "cancelled": CIRunStatus.CANCELLED,
    "failure": CIRunStatus.FAILURE,
    "neutral": CIRunStatus.SUCCESS,
    "skipped": CIRunStatus.SUCCESS,
    "stale": CIRunStatus.TIMED_OUT,
    "timed_out": CIRunStatus.TIMED_OUT,
}

_CONCLUSION_STATUS_MAP: dict[str, CIRunStatus] = {
    "success": CIRunStatus.SUCCESS,
    "failure": CIRunStatus.FAILURE,
    "cancelled": CIRunStatus.CANCELLED,
    "timed_out": CIRunStatus.TIMED_OUT,
    "action_required": CIRunStatus.PENDING,
    "neutral": CIRunStatus.SUCCESS,
    "skipped": CIRunStatus.SUCCESS,
    "stale": CIRunStatus.TIMED_OUT,
}


class GitHubActionsCIRunner(CIRunnerBase):
    """GitHub Actions CI runner using the Check Runs, Workflow Runs, and Actions APIs.

    Requires a GitHub API token with ``repo`` scope.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "Accept": "application/vnd.github+json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_GITHUB_API, headers=self._headers(), timeout=30)

    @staticmethod
    def _parse_duration_seconds(raw: dict[str, Any]) -> int | None:
        started = raw.get("run_started_at")
        updated = raw.get("updated_at")
        if started and updated:
            try:
                fmt = "%Y-%m-%dT%H:%M:%SZ"
                started_dt = datetime.strptime(started, fmt).replace(tzinfo=UTC)
                updated_dt = datetime.strptime(updated, fmt).replace(tzinfo=UTC)
                return int((updated_dt - started_dt).total_seconds())
            except (ValueError, TypeError):
                return None
        return None

    def _parse_run(self, raw: dict[str, Any]) -> CIRun:
        status: CIRunStatus
        raw_status = raw.get("status", "") or ""
        raw_conclusion = raw.get("conclusion")

        if raw_conclusion and raw_conclusion in _CONCLUSION_STATUS_MAP:
            status = _CONCLUSION_STATUS_MAP[raw_conclusion]
        elif raw_status in _STATUS_MAP:
            status = _STATUS_MAP[raw_status]
        else:
            status = CIRunStatus.UNKNOWN

        actor = raw.get("actor")
        return CIRun(
            id=str(raw.get("id", "")),
            pipeline_id=raw.get("workflow_id", ""),
            status=status,
            url=raw.get("html_url", ""),
            branch=raw.get("head_branch", ""),
            commit_sha=raw.get("head_sha", ""),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            duration_seconds=self._parse_duration_seconds(raw),
            triggered_by=actor.get("login", "") if isinstance(actor, dict) else "",
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/user")
                if r.status_code != 200:
                    return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")
                return HealthResult(ok=True)
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
        parts = pipeline_id.rsplit("/", 1)
        owner_repo = parts[0]
        if not owner_repo:
            raise ValueError(f"pipeline_id must be 'owner/repo' or 'owner/repo/workflow.yml', got {pipeline_id!r}")
        workflow_filename = parts[1] if len(parts) > 1 else ""

        try:
            async with self._client() as client:
                if workflow_filename:
                    r = await client.post(
                        f"/repos/{owner_repo}/actions/workflows/{workflow_filename}/dispatches",
                        json={
                            "ref": branch or "main",
                            "inputs": variables or {},
                        },
                    )
                else:
                    r = await client.post(
                        f"/repos/{owner_repo}/dispatches",
                        json={"event_type": "modulo-trigger", "client_payload": variables or {}},
                    )
                r.raise_for_status()

                if r.status_code == 204:
                    params: dict[str, Any] = {"per_page": 1, "branch": branch or "main"}
                    if workflow_filename:
                        params["workflow_id"] = workflow_filename
                    workflows_r = await client.get(
                        f"/repos/{owner_repo}/actions/runs",
                        params=params,
                    )
                    workflows_r.raise_for_status()
                    runs = _safe_records(workflows_r.json(), "workflow_runs")
                    if runs:
                        return self._parse_run(runs[0])

                return CIRun(
                    id="",
                    pipeline_id=pipeline_id,
                    status=CIRunStatus.PENDING,
                    url=f"https://github.com/{owner_repo}/actions",
                )
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitHub API error ({exc.response.status_code}): {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"GitHub API connection error: {exc}") from exc

    async def get_run_status(self, run_id: str) -> CIRun:
        parts = run_id.rsplit("/", 1)
        if len(parts) < 2:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'owner/repo/run_id'.")
        owner_repo, run_id_str = parts
        if not owner_repo or not run_id_str:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'owner/repo/run_id'.")
        try:
            async with self._client() as client:
                r = await client.get(f"/repos/{owner_repo}/actions/runs/{run_id_str}")
                r.raise_for_status()
                return self._parse_run(r.json())
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitHub API error ({exc.response.status_code}): {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"GitHub API connection error: {exc}") from exc

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        parts = run_id.rsplit("/", 1)
        if len(parts) < 2:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'owner/repo/run_id'.")
        owner_repo, run_id_str = parts
        if not owner_repo or not run_id_str:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'owner/repo/run_id'.")
        try:
            async with self._client() as client:
                url = f"/repos/{owner_repo}/actions/runs/{run_id_str}/logs"
                if cursor:
                    url = f"{url}?start_line={cursor}"
                r = await client.get(url)
                redirects = 0
                while r.status_code == 202 and redirects < 5:
                    location = r.headers.get("location", "")
                    if not location:
                        break
                    r = await client.get(location)
                    redirects += 1
                if r.status_code == 202:
                    raise ValueError(f"GitHub log archive still preparing after {redirects} redirects")
                r.raise_for_status()
                text = r.text
                lines = text.splitlines()
                start_line = int(cursor) if cursor and cursor.isdigit() else 0
                return CIRunLog(
                    run_id=run_id,
                    lines=lines,
                    next_cursor=str(start_line + len(lines)) if cursor is not None else None,
                )
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitHub API error ({exc.response.status_code}): {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"GitHub API connection error: {exc}") from exc

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
        params: dict[str, Any] = {"per_page": limit}
        owner_repo = pipeline_id
        if pipeline_id.count("/") >= 2:
            parts = pipeline_id.rsplit("/", 1)
            owner_repo = parts[0]
            params["workflow_id"] = parts[1]
        if not owner_repo:
            raise ValueError(f"pipeline_id must include owner/repo, got {pipeline_id!r}")
        if status:
            status_map: dict[CIRunStatus, str] = {
                CIRunStatus.QUEUED: "queued",
                CIRunStatus.IN_PROGRESS: "in_progress",
                CIRunStatus.SUCCESS: "success",
                CIRunStatus.FAILURE: "failure",
                CIRunStatus.CANCELLED: "cancelled",
                CIRunStatus.TIMED_OUT: "timed_out",
            }
            gh_status = status_map.get(status)
            if gh_status:
                params["status"] = gh_status
            elif status == CIRunStatus.UNKNOWN:
                logger.warning("Cannot filter by UNKNOWN status — returning all runs")
            else:
                logger.warning("No GitHub mapping for status %s — returning all runs", status)

        try:
            async with self._client() as client:
                r = await client.get(f"/repos/{owner_repo}/actions/runs", params=params)
                r.raise_for_status()
                raw_runs = _safe_records(r.json(), "workflow_runs")
                return [self._parse_run(run) for run in raw_runs]
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitHub API error ({exc.response.status_code}): {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"GitHub API connection error: {exc}") from exc


class _GitHubActionsTestDouble(GitHubActionsCIRunner):
    """Minimal test double that does not make HTTP calls."""

    def __init__(self) -> None:
        import uuid as _uuid

        self._token = "ghp_test"  # nosec - test double, not a real credential
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
            pipeline_id="test/workflow.yml",
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
                id="run-1",
                pipeline_id=pipeline_id or "test/workflow.yml",
                status=status or CIRunStatus.SUCCESS,
            ),
        ]

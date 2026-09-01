"""Buildkite CI/CD connector — triggers and observes pipeline runs via the Buildkite REST API v2."""

from datetime import datetime
from typing import Any, cast

import httpx

from modulo._types import _LIST_DICT_STR_ANY
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

_BUILDKITE_API = "https://api.buildkite.com/v2"

_STATUS_MAP: dict[str, CIRunStatus] = {
    "scheduled": CIRunStatus.QUEUED,
    "running": CIRunStatus.IN_PROGRESS,
    "passing": CIRunStatus.SUCCESS,
    "passed": CIRunStatus.SUCCESS,
    "failing": CIRunStatus.FAILURE,
    "failed": CIRunStatus.FAILURE,
    "canceling": CIRunStatus.CANCELLED,
    "canceled": CIRunStatus.CANCELLED,
    "blocked": CIRunStatus.PENDING,
    "skipped": CIRunStatus.CANCELLED,
    "not_run": CIRunStatus.CANCELLED,
    "waiting": CIRunStatus.PENDING,
    "waiting_failed": CIRunStatus.FAILURE,
}


def _duration_seconds(raw: dict[str, Any]) -> int | None:
    started_at = raw.get("started_at")
    finished_at = raw.get("finished_at")
    if started_at and finished_at:
        try:
            started = datetime.fromisoformat(started_at)
            finished = datetime.fromisoformat(finished_at)
            return int((finished - started).total_seconds())
        except (ValueError, TypeError):
            return None
    return None


class BuildkiteConnector(ConnectorBase):
    """Buildkite CI/CD connector using the Buildkite REST API v2.

    Requires a Buildkite API access token.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.BUILDKITE

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return pinned_async_client_sync(_BUILDKITE_API, base_url=_BUILDKITE_API, headers=self._headers(), timeout=30)

    def _parse_run(self, raw: dict[str, Any]) -> CIRun:
        raw_state = raw.get("state", "")
        status = _STATUS_MAP.get(raw_state, CIRunStatus.UNKNOWN)
        pipeline = raw.get("pipeline")
        creator = raw.get("creator")
        number = raw.get("number", "")
        web_url = raw.get("web_url", "")
        return CIRun(
            id=str(number),
            pipeline_id=pipeline.get("slug", "") if isinstance(pipeline, dict) else "",
            status=status,
            url=web_url,
            branch=raw.get("branch", ""),
            commit_sha=raw.get("commit", ""),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("finished_at", ""),
            duration_seconds=_duration_seconds(raw),
            triggered_by=creator.get("name", "") if isinstance(creator, dict) else "",
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/user")
            if r.status_code == 200:
                return HealthResult(ok=True)
            if r.status_code in (401, 403):
                return HealthResult(ok=False, detail="Authentication failed: invalid or expired token")
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Buildkite API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Buildkite API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Buildkite API connection error")

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "organizations":
                async with self._client() as client:
                    r = await client.get("/organizations")
                    r.raise_for_status()
                    records = cast(_LIST_DICT_STR_ANY, r.json())
                    return ConnectorResult(records=records, total=len(records))
            case "pipelines":
                org = q.filters.get("organization", "")
                async with self._client() as client:
                    r = await client.get(f"/organizations/{org}/pipelines")
                    r.raise_for_status()
                    records = cast(_LIST_DICT_STR_ANY, r.json())
                    return ConnectorResult(records=records, total=len(records))
            case "builds":
                org = q.filters.get("organization", "")
                pipeline = q.filters.get("pipeline", "")
                params: dict[str, Any] = {}
                if q.cursor:
                    params["page_token"] = q.cursor
                async with self._client() as client:
                    r = await client.get(
                        f"/organizations/{org}/pipelines/{pipeline}/builds",
                        params=params,
                    )
                    r.raise_for_status()
                    records = cast(_LIST_DICT_STR_ANY, r.json())
                    return ConnectorResult(records=records, total=len(records))
            case "jobs":
                org = q.filters.get("organization", "")
                pipeline = q.filters.get("pipeline", "")
                build_number = q.filters.get("build", "")
                async with self._client() as client:
                    r = await client.get(
                        f"/organizations/{org}/pipelines/{pipeline}/builds/{build_number}/jobs",
                    )
                    r.raise_for_status()
                    records = cast(_LIST_DICT_STR_ANY, r.json())
                    return ConnectorResult(records=records, total=len(records))
            case _:
                raise ValueError(f"Unsupported query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        if payload.resource != "build":
            raise ValueError(f"Unsupported write resource: {payload.resource!r}")
        org = payload.data.get("organization", "")
        pipeline = payload.data.get("pipeline", "")
        body: dict[str, Any] = {
            "branch": payload.data.get("branch", "main"),
        }
        commit = payload.data.get("commit")
        if commit:
            body["commit"] = commit
        message = payload.data.get("message")
        if message:
            body["message"] = message
        env = payload.data.get("env")
        if env:
            body["env"] = env
        meta_data = payload.data.get("meta_data")
        if meta_data:
            body["meta_data"] = meta_data
        async with self._client() as client:
            r = await client.post(
                f"/organizations/{org}/pipelines/{pipeline}/builds",
                json=body,
            )
            r.raise_for_status()
            return cast("dict[str, Any]", r.json())

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        org, _, pipeline_slug = pipeline_id.partition("/")
        if not pipeline_slug:
            raise ValueError(
                f"Invalid pipeline_id format: {pipeline_id!r}. Expected 'org/pipeline_slug'.",
            )
        body: dict[str, Any] = {"branch": branch or "main"}
        if variables:
            body["env"] = variables
        async with self._client() as client:
            r = await client.post(
                f"/organizations/{org}/pipelines/{pipeline_slug}/builds",
                json=body,
            )
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            return self._parse_run(data)

    async def get_run_status(self, run_id: str) -> CIRun:
        parts = run_id.split("/", 2)
        if len(parts) != 3:
            raise ValueError(
                f"Invalid run_id format: {run_id!r}. Expected 'org/pipeline_slug/build_number'.",
            )
        org, pipeline_slug, build_number = parts
        async with self._client() as client:
            r = await client.get(
                f"/organizations/{org}/pipelines/{pipeline_slug}/builds/{build_number}",
            )
            r.raise_for_status()
            return self._parse_run(r.json())

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        parts = run_id.split("/", 2)
        if len(parts) != 3:
            raise ValueError(
                f"Invalid run_id format: {run_id!r}. Expected 'org/pipeline_slug/build_number'.",
            )
        org, pipeline_slug, build_number = parts
        async with self._client() as client:
            jobs_r = await client.get(
                f"/organizations/{org}/pipelines/{pipeline_slug}/builds/{build_number}/jobs",
            )
            jobs_r.raise_for_status()
            jobs = jobs_r.json()
            all_lines: list[str] = []
            for job in jobs:
                job_id = job.get("id", "")
                job_name = job.get("name", "")
                all_lines.append(f"--- Job: {job_name} ({job_id}) ---")
                log_r = await client.get(
                    f"/organizations/{org}/pipelines/{pipeline_slug}/builds/{build_number}/jobs/{job_id}/log",
                )
                if log_r.status_code == 200:
                    content = log_r.text
                    if content:
                        all_lines.extend(content.splitlines())
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
        if not pipeline_id:
            return []
        org, _, pipeline_slug = pipeline_id.partition("/")
        if not pipeline_slug:
            raise ValueError(
                f"Invalid pipeline_id format: {pipeline_id!r}. Expected 'org/pipeline_slug'.",
            )
        params: dict[str, Any] = {"per_page": limit}
        if status:
            status_map: dict[CIRunStatus, str] = {
                CIRunStatus.PENDING: "blocked",
                CIRunStatus.QUEUED: "scheduled",
                CIRunStatus.IN_PROGRESS: "running",
                CIRunStatus.SUCCESS: "passed",
                CIRunStatus.FAILURE: "failed",
                CIRunStatus.CANCELLED: "canceled",
            }
            bk_state = status_map.get(status)
            if bk_state:
                params["state[]"] = bk_state
        async with self._client() as client:
            r = await client.get(
                f"/organizations/{org}/pipelines/{pipeline_slug}/builds",
                params=params,
            )
            r.raise_for_status()
            raw_runs: list[dict[str, Any]] = r.json()
            return [self._parse_run(run) for run in raw_runs[:limit]]


class _BuildkiteTestDouble(BuildkiteConnector):
    """Minimal test double that does not make HTTP calls."""

    def __init__(self) -> None:
        import uuid as _uuid

        self._token = "bkt_test"  # nosec - test double, not a real credential
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
            pipeline_id="my-org/my-pipeline",
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
                id="build-1",
                pipeline_id=pipeline_id or "my-org/my-pipeline",
                status=status or CIRunStatus.SUCCESS,
            ),
        ]

    async def query(self, _q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult(records=[])

    async def write(self, _payload: ConnectorPayload) -> dict[str, Any]:
        return {}

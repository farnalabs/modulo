"""GitHub Actions CI runner — triggers and observes workflow runs via the GitHub API."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import CIRun, CIRunLog, CIRunStatus, HealthResult
from modulo.connectors.ci_runner.base import CIRunnerBase
from modulo.core.ssrf import pinned_async_client_sync

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_API_VERSION = "2022-11-28"

#: Attempts to resolve a freshly-dispatched run through the latest-runs
#: lookup. GitHub's runs listing is eventually consistent right after a
#: dispatch accepts (204 No Content), so a single immediate miss must not be
#: read as "no run exists".
_LATEST_RUN_LOOKUP_ATTEMPTS = 3

#: Pause between the bounded latest-runs lookup attempts. Module-level (never
#: inlined) so tests patch it to zero instead of sleeping in wall-clock time.
_LATEST_RUN_LOOKUP_RETRY_SECONDS = 1.0

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
        # PINNED TRANSPORT (FAR-526B): build the client through the pinned
        # transport so the hardcoded api.github.com address is pinned onto the
        # connection (never re-resolved at connect time — closes DNS rebinding).
        # The token is in the Authorization header, so no creds reach the URL.
        return pinned_async_client_sync(_GITHUB_API, base_url=_GITHUB_API, headers=self._headers(), timeout=30)

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

    def _parse_run(self, raw: dict[str, Any], owner_repo: str = "") -> CIRun:
        """Parse a workflow-run payload into a :class:`CIRun`.

        ``owner_repo`` qualifies the id into the ``owner/repo/run_id`` form this
        connector's ``get_run_status``/``get_run_logs`` consume — every
        production call site passes the owner/repo it addressed the request
        against (see the run-id contract on ``CIRunnerBase``). Without it the
        raw payload id is returned for corrupt-payload hardening callers only.
        """
        status: CIRunStatus
        raw_status = raw.get("status", "") or ""
        raw_conclusion = raw.get("conclusion")

        if raw_conclusion and raw_conclusion in _CONCLUSION_STATUS_MAP:
            status = _CONCLUSION_STATUS_MAP[raw_conclusion]
        elif raw_status in _STATUS_MAP:
            status = _STATUS_MAP[raw_status]
        else:
            status = CIRunStatus.UNKNOWN

        raw_id = raw.get("id")
        actor = raw.get("actor")
        if raw_id is None:
            run_id = ""
        elif owner_repo:
            run_id = f"{owner_repo}/{raw_id}"
        else:
            run_id = str(raw_id)
        return CIRun(
            id=run_id,
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

    @staticmethod
    def _split_pipeline_id(pipeline_id: str) -> tuple[str, str]:
        """Split a pipeline_id into ``(owner_repo, workflow_filename)``.

        ``workflow_filename`` is empty for the repository-dispatch form.
        """
        parts = pipeline_id.rsplit("/", 1)
        owner_repo = parts[0]
        if not owner_repo:
            raise ValueError(f"pipeline_id must be 'owner/repo' or 'owner/repo/workflow.yml', got {pipeline_id!r}")
        workflow_filename = parts[1] if len(parts) > 1 else ""
        return owner_repo, workflow_filename

    async def _post_dispatch(
        self,
        client: httpx.AsyncClient,
        owner_repo: str,
        workflow_filename: str,
        branch: str,
        variables: dict[str, str] | None,
    ) -> httpx.Response:
        if workflow_filename:
            return await client.post(
                f"/repos/{owner_repo}/actions/workflows/{workflow_filename}/dispatches",
                json={
                    "ref": branch or "main",
                    "inputs": variables or {},
                },
            )
        return await client.post(
            f"/repos/{owner_repo}/dispatches",
            json={"event_type": "modulo-trigger", "client_payload": variables or {}},
        )

    async def _latest_dispatched_run(
        self,
        client: httpx.AsyncClient,
        owner_repo: str,
        workflow_filename: str,
        branch: str,
        dispatched_after: datetime,
    ) -> CIRun | None:
        """Fetch the run created for a just-dispatched workflow, or None.

        GitHub answers a dispatch with 204 No Content and materialises the run
        asynchronously, so the id can only come from this listing — and the
        listing is NEWEST-FIRST with no time bound of its own. On a repository
        with history an unbounded ``per_page=1`` lookup therefore resolves to
        the PREVIOUS run while the new one is not visible yet, and that stale id
        was handed to ``await_completion``, which then watched an unrelated job.

        The lookup is bounded to runs created at/after *dispatched_after* —
        captured BEFORE the dispatch POST, floored to the second by the ISO-8601
        format — via GitHub's documented ``created`` search qualifier
        (``>=YYYY-MM-DDTHH:MM:SSZ``), so a pre-dispatch run can never be
        returned. An empty page means the new run is not visible YET: the
        caller's bounded retry re-asks, then fails loud if it never appears.
        (A local clock more than ~1 s AHEAD of GitHub's would exclude even the
        new run — the failure is loud "no run id could be resolved", never a
        wrong id.)
        """
        params: dict[str, Any] = {
            "per_page": 1,
            "branch": branch or "main",
            "created": f">={dispatched_after:%Y-%m-%dT%H:%M:%SZ}",
        }
        if workflow_filename:
            params["workflow_id"] = workflow_filename
        workflows_r = await client.get(
            f"/repos/{owner_repo}/actions/runs",
            params=params,
        )
        workflows_r.raise_for_status()
        runs = _safe_records(workflows_r.json(), "workflow_runs")
        if runs:
            return self._parse_run(runs[0], owner_repo=owner_repo)
        return None

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
        owner_repo, workflow_filename = self._split_pipeline_id(pipeline_id)

        # Captured BEFORE the dispatch: the lower bound that keeps the
        # post-dispatch listing from answering with a PREVIOUS run (see
        # _latest_dispatched_run). One timestamp, floored to the second by the
        # format, so it can only ever include the run we are about to fire.
        dispatched_after = datetime.now(UTC)
        try:
            async with self._client() as client:
                r = await self._post_dispatch(client, owner_repo, workflow_filename, branch, variables)
                r.raise_for_status()

                # GitHub's dispatch endpoints answer 204 No Content — the run
                # is created asynchronously, so its id can only come from the
                # latest-runs lookup. Retry that (eventually consistent) lookup
                # a bounded number of times, then FAIL LOUD: an id the next
                # get_run_status call would reject is never handed back.
                attempts = _LATEST_RUN_LOOKUP_ATTEMPTS
                for attempt in range(attempts):
                    latest = await self._latest_dispatched_run(
                        client,
                        owner_repo,
                        workflow_filename,
                        branch,
                        dispatched_after,
                    )
                    if latest is not None and latest.id:
                        return latest
                    if attempt + 1 < attempts:
                        await asyncio.sleep(_LATEST_RUN_LOOKUP_RETRY_SECONDS)
                raise ValueError(
                    f"GitHub accepted the dispatch for {owner_repo} but no run id could be resolved "
                    f"(looked up the latest run {attempts} times, bounded to created>="
                    f"{dispatched_after:%Y-%m-%dT%H:%M:%SZ}) — refusing to return an unusable run id; "
                    f"check https://github.com/{owner_repo}/actions",
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
                return self._parse_run(r.json(), owner_repo=owner_repo)
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
                    # GitHub's log archive redirects to a signed CDN URL on a
                    # different host; follow it through a fresh pinned client so
                    # the redirect target is validated+pinned too (never a bare
                    # unpinned egress), matching the primary client's SSRF gate.
                    async with pinned_async_client_sync(location, timeout=30) as follow_client:
                        r = await follow_client.get(location)
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
                return [self._parse_run(run, owner_repo=owner_repo) for run in raw_runs]
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
        # Run-id contract (FAR-1141): emit `owner/repo/run_id`, the exact form
        # get_run_status/get_run_logs parse - a bare id is rejected there, so
        # the double must never hand one back. The owner/repo is derived the
        # same way the real producer derives it.
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
        owner_repo = self._split_pipeline_id(pipeline_id)[0]
        run = CIRun(
            id=f"{owner_repo}/{self._uuid.uuid4()}",
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
        resolved = pipeline_id or "test/workflow.yml"
        owner_repo = self._split_pipeline_id(resolved)[0]
        return [
            CIRun(
                id=f"{owner_repo}/run-1",
                pipeline_id=resolved,
                status=status or CIRunStatus.SUCCESS,
            ),
        ]

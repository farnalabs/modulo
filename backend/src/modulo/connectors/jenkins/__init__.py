"""Jenkins CI/CD connector — triggers and observes pipeline/Job runs via the Jenkins REST API."""

import asyncio
import base64
import logging
import re
from typing import Any

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

_logger = logging.getLogger(__name__)

_STATUS_MAP: dict[str, CIRunStatus] = {
    "SUCCESS": CIRunStatus.SUCCESS,
    "FAILURE": CIRunStatus.FAILURE,
    "UNSTABLE": CIRunStatus.FAILURE,
    "ABORTED": CIRunStatus.CANCELLED,
    "NOT_BUILT": CIRunStatus.CANCELLED,
    "BUILDING": CIRunStatus.IN_PROGRESS,
    "QUEUED": CIRunStatus.QUEUED,
    "PENDING": CIRunStatus.PENDING,
}

#: A build POST's ``Location`` pointing at a queue item.
_QUEUE_ITEM_PATH_RE = re.compile(r"/queue/item/(\d+)")

#: A build POST's ``Location`` pointing straight at a build
#: (``.../job/<job path>/<number>/``).
_BUILD_PATH_RE = re.compile(r"/job/(.+)/(\d+)/?$")

#: The queue form of a run id: ``job_name/queue/queue_id`` — produced by
#: ``trigger_run`` when Jenkins answers with a queue item, consumed by
#: ``get_run_status``/``get_run_logs`` until the queue resolves it to a build.
_QUEUE_RUN_ID_RE = re.compile(r"^(?P<job>.+)/queue/(?P<qid>\d+)$")


def _split_build_run_id(run_id: str) -> tuple[str, str]:
    """Split a ``job_name/build_number`` run id into its two parts.

    Raises ``ValueError`` on anything else — a bare number is never
    reinterpreted as ``job=<number>``, which would address a nonexistent (or
    worse, unrelated) job.
    """
    job_name, separator, build_number = run_id.rpartition("/")
    if not separator or not job_name or not build_number.isdigit():
        raise ValueError(
            f"Invalid run_id format: {run_id!r}. Expected 'job_name/build_number' or 'job_name/queue/queue_id'.",
        )
    return job_name, build_number


class JenkinsConnector(ConnectorBase):
    """Jenkins CI/CD connector using the Jenkins REST API.

    Uses Basic auth (username + API token or password).
    Optionally fetches a crumb for write operations.
    """

    def __init__(self, username: str, token: str, base_url: str = "http://localhost:8080") -> None:
        self._username = username
        self._token = token
        self._base_url = base_url.rstrip("/")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.JENKINS

    def _auth_header(self) -> dict[str, str]:
        raw = f"{self._username}:{self._token}"
        encoded = base64.b64encode(raw.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

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

    async def _fetch_crumb(self, client: httpx.AsyncClient) -> dict[str, str]:
        try:
            r = await client.get("/crumbIssuer/api/json")
            if r.status_code == 200:
                data = r.json()
                field = data.get("crumbRequestField", "Jenkins-Crumb")
                crumb = data.get("crumb", "")
                return {field: crumb}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.debug("Failed to fetch Jenkins crumb: %s", exc)
        return {}

    def _parse_build(self, raw: dict[str, Any], job_name: str = "") -> CIRun:
        """Parse a build payload into a :class:`CIRun`.

        The id is the ``job_name/build_number`` form ``get_run_status``/
        ``get_run_logs`` consume (see the run-id contract on ``CIRunnerBase``);
        ``job_name`` comes from the caller (the build payload carries the
        number, not the job path). Without it — corrupt-payload hardening
        callers only — the raw number is returned.
        """
        raw_id = raw.get("id")
        number = raw.get("number")
        build_number = number if number is not None else raw_id
        if build_number is None or build_number == "":
            run_id = ""
        elif job_name:
            run_id = f"{job_name}/{build_number}"
        else:
            run_id = str(build_number)
        timestamp = raw.get("timestamp")
        raw_result = raw.get("result") or "BUILDING"
        status = _STATUS_MAP.get(raw_result, CIRunStatus.UNKNOWN)
        full_url = raw.get("url", "")
        duration_ms = _safe_int(raw.get("duration")) if raw.get("duration") else None
        return CIRun(
            id=run_id,
            pipeline_id=raw.get("fullDisplayName", raw.get("jobName", "")),
            status=status,
            url=full_url,
            branch="",
            commit_sha="",
            created_at=str(timestamp) if timestamp is not None else "",
            updated_at="",
            duration_seconds=duration_ms // 1000 if duration_ms else None,
            triggered_by="",
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/api/json", params={"tree": "nodeName"})
            if r.status_code == 200:
                return HealthResult(ok=True)
            if r.status_code in (401, 403):
                return HealthResult(ok=False, detail="Authentication failed: invalid username or token")
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Jenkins API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Jenkins API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Jenkins API connection error")
        except ValueError as exc:
            # The outbound SSRF guard in _client() rejects a private/internal
            # base_url. Report it as unhealthy with the remediation text rather
            # than letting it escape as a 502 from GET /connectors/{id}/health.
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        job_name = pipeline_id
        async with self._client() as client:
            crumb_headers = await self._fetch_crumb(client)
            client.headers.update(crumb_headers)
            if variables:
                params = variables
                r = await client.post(f"/job/{job_name}/buildWithParameters", params=params)
            else:
                r = await client.post(f"/job/{job_name}/build")
            r.raise_for_status()
            location = r.headers.get("Location", "")
            queue_match = _QUEUE_ITEM_PATH_RE.search(location)
            if queue_match:
                # Jenkins answers with a QUEUE item id, not a build number.
                # Carry it in the queue form so get_run_status/get_run_logs
                # can resolve it (queue item -> executable -> build) instead of
                # misreading it as a build number under a wrong job.
                run_id = f"{job_name}/queue/{queue_match.group(1)}"
            else:
                build_match = _BUILD_PATH_RE.search(location)
                if not build_match:
                    # No recognisable Location means no resolvable id — fail
                    # loud rather than hand back an id the next call rejects.
                    raise ValueError(
                        f"Jenkins accepted the build for {job_name!r} but returned an unrecognised "
                        f"Location {location!r} — refusing to return an unusable run id",
                    )
                run_id = f"{build_match.group(1)}/{build_match.group(2)}"
            return CIRun(
                id=run_id,
                pipeline_id=job_name,
                status=CIRunStatus.QUEUED,
                url=location,
            )

    async def _queue_executable(
        self,
        client: httpx.AsyncClient,
        queue_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Resolve a queue item to its dispatched build, or None while queued.

        Raises ``ValueError`` when the queue item can no longer be read
        (Jenkins evicts it) — a loud failure beats polling a dead id forever.
        """
        q_r = await client.get(f"/queue/item/{queue_id}/api/json")
        if q_r.status_code == 404:
            raise ValueError(
                f"Jenkins queue item {queue_id} for run {run_id!r} no longer exists — cannot resolve a build for it",
            )
        q_r.raise_for_status()
        data = q_r.json()
        if not isinstance(data, dict):
            raise ValueError(
                f"Jenkins queue item {queue_id} returned a non-object body — cannot resolve a build",
            )
        executable = data.get("executable")
        if isinstance(executable, dict) and executable.get("number") is not None:
            return executable
        return None

    async def get_run_status(self, run_id: str) -> CIRun:
        queue_match = _QUEUE_RUN_ID_RE.match(run_id)
        if queue_match:
            job_name = queue_match.group("job")
            build_number = ""
        else:
            # Validate BEFORE any HTTP call — a bare/garbage id is rejected,
            # never reinterpreted as job=<bare id>.
            job_name, build_number = _split_build_run_id(run_id)
        async with self._client() as client:
            if queue_match:
                queue_id = queue_match.group("qid")
                executable = await self._queue_executable(client, queue_id, run_id)
                if executable is None:
                    # Still waiting in the queue: a NON-terminal status under
                    # the SAME consumable queue-form id, so the await loop
                    # keeps polling until the build is dispatched.
                    return CIRun(
                        id=run_id,
                        pipeline_id=job_name,
                        status=CIRunStatus.QUEUED,
                        url=f"{self._base_url}/queue/item/{queue_id}",
                    )
                build_number = str(executable["number"])
            r = await client.get(f"/job/{job_name}/{build_number}/api/json")
            r.raise_for_status()
            return self._parse_build(r.json(), job_name=job_name)

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        queue_match = _QUEUE_RUN_ID_RE.match(run_id)
        if queue_match:
            job_name = queue_match.group("job")
            build_number = ""
        else:
            job_name, build_number = _split_build_run_id(run_id)
        async with self._client() as client:
            if queue_match:
                executable = await self._queue_executable(client, queue_match.group("qid"), run_id)
                if executable is None:
                    # Fail loud: an empty log list would read as "the build
                    # produced no output" when it has not started at all.
                    raise ValueError(f"Jenkins run {run_id!r} is still queued — no logs until the build starts")
                build_number = str(executable["number"])
            r = await client.get(f"/job/{job_name}/{build_number}/consoleText")
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
        job_name = pipeline_id or ""
        async with self._client() as client:
            r = await client.get(
                f"/job/{job_name}/api/json",
                params={"tree": "builds[number,result,timestamp,duration,url]" + "{0," + str(limit) + "}"},
            )
            r.raise_for_status()
            data = r.json()
            builds: list[dict[str, Any]] = _safe_records(data, "builds")
            runs = [self._parse_build(b, job_name=job_name) for b in builds]
            if status:
                runs = [r for r in runs if r.status == status]
            return runs

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "jobs":
                async with self._client() as client:
                    r = await client.get("/api/json", params={"tree": "jobs[name,url,color]"})
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "jobs")
                    return ConnectorResult(records=records, total=len(records))
            case "builds":
                job_name = q.filters.get("job_name", "")
                async with self._client() as client:
                    tree = "builds[number,result,timestamp,duration,url]"
                    r = await client.get(f"/job/{job_name}/api/json", params={"tree": tree})
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "builds")
                    return ConnectorResult(records=records, total=len(records))
            case "nodes":
                async with self._client() as client:
                    r = await client.get("/computer/api/json", params={"tree": "computer[displayName,offline]"})
                    r.raise_for_status()
                    data = r.json()
                    records = _safe_records(data, "computer")
                    return ConnectorResult(records=records, total=len(records))
            case _:
                raise ValueError(f"Unsupported query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        if payload.resource != "build":
            raise ValueError(f"Unsupported write resource: {payload.resource!r}")
        job_name = payload.data.get("job_name", "")
        variables = payload.data.get("parameters")
        crumb_headers: dict[str, str] = {}
        async with self._client() as client:
            crumb_headers = await self._fetch_crumb(client)
            client.headers.update(crumb_headers)
            if variables:
                r = await client.post(f"/job/{job_name}/buildWithParameters", params=variables)
            else:
                r = await client.post(f"/job/{job_name}/build")
            r.raise_for_status()
            location = r.headers.get("Location", "")
            return {"location": location, "job_name": job_name}


class _JenkinsTestDouble(JenkinsConnector):
    """Minimal test double that does not make HTTP calls."""

    def __init__(self) -> None:
        import uuid as _uuid

        self._uuid = _uuid
        self._username = "test"
        self._token = "test"  # nosec - test double, not a real credential
        self._base_url = "http://jenkins.example.com"
        self._builds: list[dict[str, Any]] = []
        self._jobs: list[dict[str, Any]] = []
        self._nodes: list[dict[str, Any]] = []

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
            pipeline_id="my-job",
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
                pipeline_id=pipeline_id or "my-job",
                status=status or CIRunStatus.SUCCESS,
            ),
        ]

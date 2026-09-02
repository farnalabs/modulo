"""Azure Pipelines CI/CD connector — triggers and observes pipeline runs via the Azure DevOps REST API v7.0."""

import base64
from typing import Any, cast

import httpx

from modulo.connectors._safe_page import safe_paging_total as _safe_paging_total
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

_AZURE_DEVOPS_API = "https://dev.azure.com"


_STATUS_MAP: dict[str, CIRunStatus] = {
    "unknown": CIRunStatus.UNKNOWN,
    "notStarted": CIRunStatus.PENDING,
    "inProgress": CIRunStatus.IN_PROGRESS,
    "cancelling": CIRunStatus.CANCELLED,
    "completed": CIRunStatus.SUCCESS,
    "canceled": CIRunStatus.CANCELLED,
    "failed": CIRunStatus.FAILURE,
    "succeeded": CIRunStatus.SUCCESS,
    "partiallySucceeded": CIRunStatus.SUCCESS,
}


class AzurePipelinesConnector(ConnectorBase):
    """Azure Pipelines CI/CD connector using the Azure DevOps REST API v7.0.

    Authentication uses a Personal Access Token (PAT) via HTTP Basic Auth
    with an empty username (``":" + PAT`` encoded as Base64).

    The *organization* is the Azure DevOps organisation name (e.g. ``"myorg"``
    for ``https://dev.azure.com/myorg``). The *project* name (or GUID) is the
    Azure DevOps project that contains the pipelines.
    """

    def __init__(self, token: str, organization: str, project: str = "") -> None:
        self._token = token
        self._organization = organization
        self._project = project

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.AZURE_PIPELINES

    def _headers(self) -> dict[str, str]:
        encoded = base64.b64encode(f":{self._token}".encode()).decode()
        return {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-520): validate + resolve the base_url's host
        # synchronously and pin the validated IP onto the transport, so the
        # connection never re-resolves the host at connect time (closes the
        # DNS-rebinding window). ``trust_env=False`` stops a proxy from
        # re-resolving the destination server-side and defeating the pin.
        return pinned_async_client_sync(
            _AZURE_DEVOPS_API,
            base_url=_AZURE_DEVOPS_API,
            headers=self._headers(),
            timeout=30,
        )

    def _pipelines_base(self) -> str:
        return f"/{self._organization}/{self._project}/_apis/pipelines"

    def _parse_run(self, raw: dict[str, Any]) -> CIRun:
        raw_id = raw.get("id")
        raw_state = raw.get("state", "unknown")
        raw_result = raw.get("result", "")
        if raw_state == "completed" and raw_result:
            status = _STATUS_MAP.get(raw_result, CIRunStatus.UNKNOWN)
        else:
            status = _STATUS_MAP.get(raw_state, CIRunStatus.UNKNOWN)
        resources = raw.get("resources")
        repositories = resources.get("repositories") if isinstance(resources, dict) else None
        self_repo = repositories.get("self") if isinstance(repositories, dict) else None
        ref_name = self_repo.get("refName", "") if isinstance(self_repo, dict) else ""
        branch = ref_name.replace("refs/heads/", "") if ref_name else ""
        template_parameters = raw.get("templateParameters")
        pipeline = raw.get("pipeline")
        links = raw.get("_links")
        web = links.get("web") if isinstance(links, dict) else None
        return CIRun(
            id=str(raw_id) if raw_id is not None else "",
            pipeline_id=str(pipeline.get("id") or "") if isinstance(pipeline, dict) else "",
            status=status,
            url=web.get("href", "") if isinstance(web, dict) else "",
            branch=branch,
            commit_sha=self_repo.get("version", "") if isinstance(self_repo, dict) else "",
            created_at=raw.get("createdDate", ""),
            updated_at=raw.get("finishedDate", ""),
            duration_seconds=None,
            triggered_by=template_parameters.get("triggeredBy", "") if isinstance(template_parameters, dict) else "",
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get(
                    f"/{self._organization}/_apis/projects",
                    params={"api-version": "7.0"},
                )
            if r.status_code == 200:
                return HealthResult(ok=True)
            if r.status_code in (401, 403):
                return HealthResult(ok=False, detail="Authentication failed: invalid or expired PAT token")
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Azure Pipelines API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Azure Pipelines API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Azure Pipelines API connection error")
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        body: dict[str, Any] = {}
        if variables:
            body["templateParameters"] = variables
        resources: dict[str, Any] = {}
        if branch:
            resources["repositories"] = {
                "self": {"refName": f"refs/heads/{branch}"},
            }
        if resources:
            body["resources"] = resources

        async with self._client() as client:
            r = await client.post(
                f"{self._pipelines_base()}/{pipeline_id}/runs",
                params={"api-version": "7.0"},
                json=body,
            )
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            return self._parse_run(data)

    async def get_run_status(self, run_id: str) -> CIRun:
        parts = run_id.split("/", 1)
        pipeline_id = parts[0] if len(parts) == 2 else ""
        if not pipeline_id:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'pipeline_id/run_id'.")
        run_identifier = parts[1] if len(parts) == 2 else run_id
        async with self._client() as client:
            r = await client.get(
                f"{self._pipelines_base()}/{pipeline_id}/runs/{run_identifier}",
                params={"api-version": "7.0"},
            )
            r.raise_for_status()
            return self._parse_run(r.json())

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        parts = run_id.split("/", 1)
        pipeline_id = parts[0] if len(parts) == 2 else ""
        if not pipeline_id:
            raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'pipeline_id/run_id'.")
        run_identifier = parts[1] if len(parts) == 2 else run_id
        async with self._client() as client:
            logs_r = await client.get(
                f"{self._pipelines_base()}/{pipeline_id}/runs/{run_identifier}/logs",
                params={"api-version": "7.0"},
            )
            logs_r.raise_for_status()
            logs_body = logs_r.json()
            if isinstance(logs_body, list):
                logs_data: list[Any] = logs_body
            else:
                logs_data = _safe_records(logs_body, "value")

            all_lines: list[str] = []
            for log_entry in logs_data if isinstance(logs_data, list) else []:
                if not isinstance(log_entry, dict):
                    continue
                log_id = log_entry.get("id", "")
                log_url = log_entry.get("url", "")
                log_name = log_entry.get("name", f"log-{log_id}")
                all_lines.append(f"--- Log: {log_name} ({log_id}) ---")
                if log_url:
                    log_content_r = await client.get(log_url)
                    if log_content_r.status_code == 200:
                        content = log_content_r.text
                        if content:
                            all_lines.extend(f"  {line}" for line in content.splitlines())
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
        params: dict[str, Any] = {"api-version": "7.0"}
        async with self._client() as client:
            r = await client.get(
                f"{self._pipelines_base()}/{pipeline_id}/runs",
                params=params,
            )
            r.raise_for_status()
            data = r.json()
            raw_runs: list[dict[str, Any]] = _safe_records(data, "value")
            runs = [self._parse_run(run) for run in raw_runs]
            if status:
                runs = [r for r in runs if r.status == status]
            return runs[:limit]

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "projects":
                    r = await client.get(
                        f"/{self._organization}/_apis/projects",
                        params={"api-version": "7.0"},
                    )
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(
                        records=_safe_records(body, "value"),
                        total=_safe_paging_total(body, "count"),
                    )
                case "pipelines":
                    r = await client.get(
                        f"{self._pipelines_base()}",
                        params={"api-version": "7.0"},
                    )
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(
                        records=_safe_records(body, "value"),
                        total=_safe_paging_total(body, "count"),
                    )
                case "runs":
                    pipeline_id = q.filters.get("pipeline_id", "")
                    if not pipeline_id:
                        raise ValueError("Azure Pipelines runs query requires 'pipeline_id' filter")
                    r = await client.get(
                        f"{self._pipelines_base()}/{pipeline_id}/runs",
                        params={"api-version": "7.0"},
                    )
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(
                        records=_safe_records(body, "value"),
                        total=_safe_paging_total(body, "count"),
                    )
                case "releases":
                    r = await client.get(
                        f"/{self._organization}/{self._project}/_apis/release/releases",
                        params={"api-version": "7.0"},
                    )
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(
                        records=_safe_records(body, "value"),
                        total=_safe_paging_total(body, "count"),
                    )
                case _:
                    raise ValueError(f"Unsupported query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "run":
                    pipeline_id = payload.data.get("pipeline_id", "")
                    body: dict[str, Any] = {}
                    variables = payload.data.get("variables") or payload.data.get("templateParameters")
                    if variables:
                        body["templateParameters"] = variables
                    branch = payload.data.get("branch", "")
                    if branch:
                        body["resources"] = {
                            "repositories": {
                                "self": {"refName": f"refs/heads/{branch}"},
                            },
                        }
                    r = await client.post(
                        f"{self._pipelines_base()}/{pipeline_id}/runs",
                        params={"api-version": "7.0"},
                        json=body,
                    )
                    r.raise_for_status()
                    return cast("dict[str, Any]", r.json())
                case "release":
                    definition_id = payload.data.get("definition_id", "")
                    body = {
                        "definitionId": int(definition_id),
                    }
                    description = payload.data.get("description", "")
                    if description:
                        body["description"] = description
                    r = await client.post(
                        f"/{self._organization}/{self._project}/_apis/release/releases",
                        params={"api-version": "7.0"},
                        json=body,
                    )
                    r.raise_for_status()
                    return cast("dict[str, Any]", r.json())
                case _:
                    raise ValueError(f"Unsupported write resource: {payload.resource!r}")


class _AzurePipelinesTestDouble(AzurePipelinesConnector):
    """Minimal test double that does not make HTTP calls."""

    def __init__(self) -> None:
        import uuid as _uuid

        self._uuid = _uuid
        self._token = "apt_test"  # nosec - test double, not a real credential
        self._organization = "test-org"
        self._project = "test-project"
        self._runs: list[dict[str, Any]] = []
        self._status: CIRunStatus = CIRunStatus.QUEUED
        self._run_logs: list[str] = []

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
        self._runs.append({"run": run, "variables": variables or {}})
        self._status = CIRunStatus.QUEUED
        return run

    async def get_run_status(self, run_id: str) -> CIRun:
        return CIRun(
            id=run_id,
            pipeline_id="1",
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
                id="1",
                pipeline_id=pipeline_id or "1",
                status=status or CIRunStatus.SUCCESS,
            ),
        ]

    async def query(self, _q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult(records=[])

    async def write(self, _payload: ConnectorPayload) -> dict[str, Any]:
        return {}

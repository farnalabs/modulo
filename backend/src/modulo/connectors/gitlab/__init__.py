"""GitLabConnector — async GitLab API connector via REST API v4."""

import asyncio
import base64
import json
import random
from typing import Any, cast
from urllib.parse import quote

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_GITLAB_API = "https://gitlab.com/api/v4"

REQUIRED_SCOPES = frozenset({"read_api", "write_repository", "api"})

# Retry/backoff configuration
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from GitLab API response."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return None


def _safe_json(response: httpx.Response) -> Any:
    """Safely parse JSON response, handling decode errors."""
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"GitLab API invalid response: {exc}") from exc


def _safe_json_object(response: httpx.Response) -> dict[str, Any]:
    return cast(dict[str, Any], _safe_json(response))


def _project_path(project_id: str) -> str:
    """URL-encode a project path like 'group/subgroup/project'."""
    return quote(project_id, safe="")


class GitLabConnector(ConnectorBase):
    """Read/write GitLab via the REST API v4.

    Supported query resources:
      "projects"          — list projects accessible to the token
      "file"              — read a file
      "mrs"               — list merge requests (legacy, alias for merge_requests)
      "issues"            — list project issues (filters: state, labels, milestone, search, sort, order_by, assignee_id)
      "issue"             — get single issue by IID
      "labels"            — list project labels
      "label"             — get single label by ID
      "milestones"        — list project milestones
      "issue_notes"       — list notes on an issue
      "issue_discussions" — list discussions on an issue
      "merge_requests"    — list merge requests (filters: state, labels, milestone)
      "merge_request"     — get single MR by IID
      "branch"            — get single branch
      "branches"          — list branches
      "tags"              — list tags
      "pipelines"         — list pipelines
      "jobs"              — list jobs for a pipeline

    Supported write resources:
      "file"              — create/update a file
      "mr"                — create a merge request (legacy)
      "issue"             — create an issue
      "issue_update"      — update an issue (close/reopen, edit title/description)
      "issue_note"        — add a note to an issue
      "issue_label"       — replace labels on an issue
      "label"             — create a project label
      "milestone"         — create a project milestone
      "merge_request"     — create a merge request (filters: source_branch, target_branch, title, description)
      "pipeline_run"      — trigger a pipeline
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @staticmethod
    def _jitter(delay: float) -> float:
        return random.uniform(0, delay)  # noqa: S311 — non-cryptographic jitter for retry delays

    def _require_filter(self, filters: dict[str, Any], key: str, resource: str) -> Any:
        try:
            return filters[key]
        except KeyError:
            raise ValueError(f"Missing required filter {key!r} for GitLab resource {resource!r}") from None

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Call GitLab API with retry/backoff for retryable statuses.

        Retries on 429, 502, 503, 504 with exponential backoff + jitter.
        Wraps HTTP/network/parse errors as ValueError.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    r = await client.request(method, path, **kwargs)
                    if r.status_code == 304:
                        raise ValueError("GitLab API returned 304 Not Modified — resource unchanged")
                    if r.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                        retry_after = _parse_retry_after(r)
                        delay = (
                            min(retry_after, _MAX_DELAY) if retry_after else min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                        )
                        await asyncio.sleep(self._jitter(delay))
                        continue
                    r.raise_for_status()
                    return r
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    retry_after = _parse_retry_after(exc.response)
                    delay = min(retry_after, _MAX_DELAY) if retry_after else min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    await asyncio.sleep(self._jitter(delay))
                    continue
                raise ValueError(f"GitLab API HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    await asyncio.sleep(self._jitter(delay))
                    continue
                raise ValueError("GitLab API timeout") from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    await asyncio.sleep(self._jitter(delay))
                    continue
                raise ValueError("GitLab API connection error") from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    await asyncio.sleep(self._jitter(delay))
                    continue
                raise ValueError(f"GitLab API HTTP error: {exc}") from exc
        raise ValueError("GitLab API request failed after retries") from last_exc

    async def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        """Safely parse JSON response, wrapping decode errors."""
        try:
            return cast(dict[str, Any], response.json())
        except json.JSONDecodeError as exc:
            raise ValueError(f"GitLab API invalid response: {exc}") from exc

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITLAB

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_GITLAB_API, headers=self._headers(), timeout=30)

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/user")
                if r.status_code != 200:
                    return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

                try:
                    user_info = r.json()
                except json.JSONDecodeError:
                    return HealthResult(ok=False, detail=f"Invalid JSON in /user response: {r.text[:200]}")
                username = user_info.get("username", "")

                projects_r = await client.get("/projects", params={"per_page": 1})
                if projects_r.status_code in (401, 403):
                    return HealthResult(ok=False, detail="Missing scopes: API access not granted")
                if not projects_r.is_success:
                    return HealthResult(
                        ok=False,
                        detail=f"Projects API returned HTTP {projects_r.status_code}: {projects_r.text[:200]}",
                    )

            return HealthResult(ok=True, detail=username)
        except httpx.RequestError as e:
            return HealthResult(ok=False, detail=str(e))

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "projects":
                params: dict[str, Any] = {"per_page": q.limit}
                if "search" in q.filters:
                    params["search"] = q.filters["search"]
                if "membership" in q.filters:
                    params["membership"] = q.filters["membership"]
                if "visibility" in q.filters:
                    params["visibility"] = q.filters["visibility"]
                if "owned" in q.filters:
                    params["owned"] = q.filters["owned"]
                r = await self._call_api("GET", "/projects", params=params)
                data = _safe_json(r)
                return ConnectorResult(records=data, total=len(data))
            case "file":
                project = self._require_filter(q.filters, "project", q.resource)
                path = self._require_filter(q.filters, "path", q.resource)
                ref = q.filters.get("ref", "main")
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/files/{quote(path, safe='')}",
                    params={"ref": ref},
                )
                info = _safe_json(r)
                if "content" in info:
                    info["content"] = base64.b64decode(info["content"]).decode("utf-8")
                return ConnectorResult(records=[info])
            case "mrs" | "merge_requests":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                mr_params: dict[str, Any] = {"per_page": q.limit}
                if "state" in q.filters:
                    mr_params["state"] = q.filters["state"]
                if "labels" in q.filters:
                    mr_params["labels"] = q.filters["labels"]
                if "milestone" in q.filters:
                    mr_params["milestone"] = q.filters["milestone"]
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/merge_requests",
                    params=mr_params,
                )
                mrs = _safe_json(r)
                return ConnectorResult(records=mrs, total=len(mrs))
            case "merge_request":
                project = self._require_filter(q.filters, "project", q.resource)
                mr_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/merge_requests/{mr_iid}",
                )
                return ConnectorResult(records=[_safe_json(r)])
            case "issues":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                params = {"per_page": q.limit}
                for key in ("state", "labels", "milestone", "search", "sort", "order_by", "assignee_id"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues",
                    params=params,
                )
                issues = _safe_json(r)
                return ConnectorResult(records=issues, total=len(issues))
            case "issue":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}",
                )
                return ConnectorResult(records=[_safe_json(r)])
            case "labels":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/labels",
                    params={"per_page": q.limit},
                )
                labels = _safe_json(r)
                return ConnectorResult(records=labels, total=len(labels))
            case "label":
                project = self._require_filter(q.filters, "project", q.resource)
                label_id = self._require_filter(q.filters, "label_id", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/labels/{label_id}",
                )
                return ConnectorResult(records=[_safe_json(r)])
            case "milestones":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/milestones",
                    params={"per_page": q.limit},
                )
                milestones = _safe_json(r)
                return ConnectorResult(records=milestones, total=len(milestones))
            case "issue_notes":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                params = {"per_page": q.limit}
                for key in ("sort", "order_by"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}/notes",
                    params=params,
                )
                notes = _safe_json(r)
                return ConnectorResult(records=notes, total=len(notes))
            case "issue_discussions":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}/discussions",
                    params={"per_page": q.limit},
                )
                discussions = _safe_json(r)
                return ConnectorResult(records=discussions, total=len(discussions))
            case "branch":
                project = self._require_filter(q.filters, "project", q.resource)
                branch_name = self._require_filter(q.filters, "name", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/branches/{quote(branch_name, safe='')}",
                )
                return ConnectorResult(records=[_safe_json(r)])
            case "branches":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/branches",
                    params={"per_page": q.limit},
                )
                branches = _safe_json(r)
                return ConnectorResult(records=branches, total=len(branches))
            case "tags":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/tags",
                    params={"per_page": q.limit},
                )
                tags = _safe_json(r)
                return ConnectorResult(records=tags, total=len(tags))
            case "pipelines":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/pipelines",
                    params={"per_page": q.limit},
                )
                pipelines = _safe_json(r)
                return ConnectorResult(records=pipelines, total=len(pipelines))
            case "jobs":
                project = self._require_filter(q.filters, "project", q.resource)
                pipeline_id = self._require_filter(q.filters, "pipeline_id", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/pipelines/{pipeline_id}/jobs",
                    params={"per_page": q.limit},
                )
                jobs = _safe_json(r)
                return ConnectorResult(records=jobs, total=len(jobs))
            case _:
                raise ValueError(f"Unsupported GitLab resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "file":
                project = self._require_filter(payload.data, "project", payload.resource)
                path = self._require_filter(payload.data, "path", payload.resource)
                encoded = _project_path(project)
                body: dict[str, Any] = {
                    "content": payload.data["content"],
                    "commit_message": payload.data.get("message", "Update via Modulo"),
                }
                if payload.data.get("sha"):
                    body["sha"] = payload.data["sha"]
                r = await self._call_api(
                    "PUT",
                    f"/projects/{encoded}/repository/files/{quote(path, safe='')}",
                    json=body,
                )
                return _safe_json_object(r)
            case "mr" | "merge_request":
                project = self._require_filter(payload.data, "project", payload.resource)
                source_branch = self._require_filter(payload.data, "source_branch", payload.resource)
                title = self._require_filter(payload.data, "title", payload.resource)
                encoded = _project_path(project)
                body = {
                    "source_branch": source_branch,
                    "target_branch": payload.data.get("target_branch", "main"),
                    "title": title,
                }
                if "description" in payload.data:
                    body["description"] = payload.data["description"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/merge_requests",
                    json=body,
                )
                return _safe_json_object(r)
            case "issue":
                project = self._require_filter(payload.data, "project", payload.resource)
                title = self._require_filter(payload.data, "title", payload.resource)
                encoded = _project_path(project)
                body = {
                    "title": title,
                }
                if "description" in payload.data:
                    body["description"] = payload.data["description"]
                if "labels" in payload.data:
                    body["labels"] = payload.data["labels"]
                if "milestone_id" in payload.data:
                    body["milestone_id"] = payload.data["milestone_id"]
                if "assignee_ids" in payload.data:
                    body["assignee_ids"] = payload.data["assignee_ids"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/issues",
                    json=body,
                )
                return _safe_json_object(r)
            case "issue_update":
                project = self._require_filter(payload.data, "project", payload.resource)
                issue_iid = self._require_filter(payload.data, "iid", payload.resource)
                encoded = _project_path(project)
                body = {}
                for key in ("state_event", "title", "description"):
                    if key in payload.data:
                        body[key] = payload.data[key]
                r = await self._call_api(
                    "PUT",
                    f"/projects/{encoded}/issues/{issue_iid}",
                    json=body,
                )
                return _safe_json_object(r)
            case "issue_note":
                project = self._require_filter(payload.data, "project", payload.resource)
                issue_iid = self._require_filter(payload.data, "iid", payload.resource)
                encoded = _project_path(project)
                body = self._require_filter(payload.data, "body", payload.resource)
                body = {
                    "body": body,
                }
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/issues/{issue_iid}/notes",
                    json=body,
                )
                return _safe_json_object(r)
            case "issue_label":
                project = self._require_filter(payload.data, "project", payload.resource)
                issue_iid = self._require_filter(payload.data, "iid", payload.resource)
                encoded = _project_path(project)
                labels = self._require_filter(payload.data, "labels", payload.resource)
                body = {
                    "labels": labels,
                }
                r = await self._call_api(
                    "PUT",
                    f"/projects/{encoded}/issues/{issue_iid}",
                    json=body,
                )
                return _safe_json_object(r)
            case "label":
                project = self._require_filter(payload.data, "project", payload.resource)
                name = self._require_filter(payload.data, "name", payload.resource)
                encoded = _project_path(project)
                body = {
                    "name": name,
                    "color": payload.data.get("color", "#428BCA"),
                }
                if "description" in payload.data:
                    body["description"] = payload.data["description"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/labels",
                    json=body,
                )
                return _safe_json_object(r)
            case "milestone":
                project = self._require_filter(payload.data, "project", payload.resource)
                title = self._require_filter(payload.data, "title", payload.resource)
                encoded = _project_path(project)
                body = {
                    "title": title,
                }
                if "description" in payload.data:
                    body["description"] = payload.data["description"]
                if "due_date" in payload.data:
                    body["due_date"] = payload.data["due_date"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/milestones",
                    json=body,
                )
                return _safe_json_object(r)
            case "pipeline_run":
                project = self._require_filter(payload.data, "project", payload.resource)
                ref = self._require_filter(payload.data, "ref", payload.resource)
                encoded = _project_path(project)
                body = {
                    "ref": ref,
                }
                if "variables" in payload.data:
                    body["variables"] = payload.data["variables"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/pipeline",
                    json=body,
                )
                return _safe_json_object(r)
            case _:
                raise ValueError(f"Unsupported GitLab write resource: {payload.resource!r}")

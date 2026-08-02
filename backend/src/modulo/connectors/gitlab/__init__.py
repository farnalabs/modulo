"""GitLabConnector — async GitLab API connector via REST API v4."""

import asyncio
import base64
import json
import random
import time
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

# GitLab RateLimit-* headers reported on API responses
_RATE_LIMIT_HEADERS = (
    "RateLimit-Limit",
    "RateLimit-Remaining",
    "RateLimit-Observed",
    "RateLimit-Reset",
    "RateLimit-ResetTime",
)

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


def _parse_rate_limit_reset(response: httpx.Response) -> float | None:
    """Parse GitLab's rate-limit reset header (epoch seconds) into a retry delay.

    When a 429 response includes ``RateLimit-ResetTime`` (or ``RateLimit-Reset``
    from some proxies), the client can wait until the quota window resets
    instead of guessing with backoff.
    """
    value = response.headers.get("RateLimit-ResetTime") or response.headers.get("RateLimit-Reset")
    if not value:
        return None
    try:
        reset_epoch = float(value)
    except (ValueError, TypeError):
        return None
    delay = reset_epoch - time.time()
    return delay if delay > 0 else None


def _rate_limit_detail(response: httpx.Response) -> str:
    """Summarise GitLab rate-limit quota headers for error/health detail strings."""
    parts = []
    for header in _RATE_LIMIT_HEADERS:
        value = response.headers.get(header)
        if value:
            parts.append(f"{header}={value}")
    return "; ".join(parts)


def _rate_limit_metadata(response: httpx.Response) -> dict[str, Any]:
    """Extract GitLab ``RateLimit-*`` headers into a metadata dict.

    GitLab (and many self-hosted deployments behind a rate-limiting proxy)
    report quota state via ``RateLimit-Limit`` / ``RateLimit-Remaining`` /
    ``RateLimit-Reset`` (and friends). Only headers present on the response
    are included, so an empty dict simply means no rate-limit reporting.
    """
    return {name: response.headers.get(name) for name in _RATE_LIMIT_HEADERS if name in response.headers}


def _parse_next_page(response: httpx.Response) -> str | None:
    """Parse the X-Next-Page header for pagination cursor on list endpoints.

    GitLab reports ``X-Next-Page`` (the next page number) on paginated
    responses. Absent or "0" means this is the last page.
    """
    value = response.headers.get("X-Next-Page", "")
    if not value:
        return None
    try:
        page = int(value)
    except (ValueError, TypeError):
        return None
    return str(page) if page > 0 else None


def _paginate_params(params: dict[str, Any], cursor: str | None) -> None:
    """Add GitLab page param from a pagination cursor, if present."""
    if cursor:
        try:
            params["page"] = int(cursor)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid GitLab pagination cursor: {cursor!r}") from None


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

    Supports self-hosted GitLab instances via the ``base_url`` constructor
    argument (defaults to the hosted ``https://gitlab.com/api/v4`` endpoint).
    Pass ``base_url`` (e.g. ``https://gitlab.example.com/api/v4``) to target
    a self-hosted GitLab instance.
    List resources return ``next_cursor`` from GitLab's ``X-Next-Page``
    header; pass it back as ``ConnectorQuery.cursor`` to fetch the next page
    (GitLab ``page`` query param). List results also expose ``metadata["rate_limit"]``
    mirroring GitLab's ``RateLimit-*`` response headers when present.

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
      "file_delete"       — delete a file
      "mr"                — create a merge request (legacy)
      "issue"             — create an issue
      "issue_update"      — update an issue (close/reopen, edit title/description)
      "issue_note"        — add a note to an issue
      "issue_label"       — replace labels on an issue
      "label"             — create a project label
      "milestone"         — create a project milestone
      "merge_request"     — create a merge request (filters: source_branch, target_branch, title, description)
      "file_delete"       — delete a file (data: project, path, ref, commit_message)
      "mr_merge"          — merge a merge request (data: project, iid, optional squash)
      "mr_approve"        — approve a merge request (data: project, iid)
      "mr_comment"        — add a comment to a merge request (data: project, iid, body)
      "mr_note"           — add a comment to a merge request (data: project, iid, body)
      "mr_labels"         — set labels on a merge request (data: project, iid, labels)
      "pipeline_run"      — trigger a pipeline
    """

    def __init__(self, token: str, base_url: str = _GITLAB_API) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    @staticmethod
    def _jitter(delay: float, *, tight: bool = False) -> float:
        """Add jitter to a retry delay.

        Full jitter (``[0, delay)``) is used for exponential backoff to avoid
        the thundering herd. Server-derived waits (quota reset / Retry-After)
        use tight jitter around the requested value so the window is honoured
        instead of being collapsed to near-immediate retries.
        """
        if tight:
            return random.uniform(delay * 0.9, delay)  # noqa: S311 — non-cryptographic jitter for retry delays
        return random.uniform(0, delay)  # noqa: S311 — non-cryptographic jitter for retry delays

    @staticmethod
    def _has_server_delay(response: httpx.Response) -> bool:
        """Whether the response carries an explicit server-provided retry delay.

        ``Retry-After`` is honoured on any retryable status. GitLab reports the
        ``RateLimit-Reset`` headers on *every* response while rate limiting is
        active, so they only count as a server delay on HTTP 429 (the quota
        window); on other retryable statuses they would otherwise switch the
        backoff to tight jitter and undermine thundering-herd protection.
        """
        if _parse_retry_after(response) is not None:
            return True
        return response.status_code == 429 and _parse_rate_limit_reset(response) is not None

    def _sleep_delay(self, response: httpx.Response, attempt: int) -> float:
        """Compute the sleep before a retry, honouring server-provided wait times."""
        delay = self._retry_delay(response, attempt)
        if self._has_server_delay(response):
            return self._jitter(delay, tight=True)
        return self._jitter(delay)

    def _require_filter(self, filters: dict[str, Any], key: str, resource: str) -> Any:
        try:
            return filters[key]
        except KeyError:
            raise ValueError(f"Missing required filter {key!r} for GitLab resource {resource!r}") from None

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Call GitLab API with retry/backoff for retryable statuses.

        Retries on 429, 502, 503, 504 with exponential backoff + jitter.
        On 429 responses, prefers ``Retry-After`` then ``RateLimit-ResetTime``
        to compute the wait instead of blind backoff. Wraps HTTP/network/parse
        errors as ValueError.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    r = await client.request(method, path, **kwargs)
                    if r.status_code == 304:
                        raise ValueError("GitLab API returned 304 Not Modified — resource unchanged")
                    if r.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                        await asyncio.sleep(self._sleep_delay(r, attempt))
                        continue
                    r.raise_for_status()
                    return r
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    await asyncio.sleep(self._sleep_delay(exc.response, attempt))
                    continue
                detail = exc.response.text[:200]
                if exc.response.status_code == 429:
                    quota = _rate_limit_detail(exc.response)
                    if quota:
                        detail = f"{detail} (quota: {quota})"
                raise ValueError(f"GitLab API HTTP {exc.response.status_code}: {detail}") from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(self._jitter(min(_BASE_DELAY * (1 << attempt), _MAX_DELAY)))
                    continue
                raise ValueError("GitLab API timeout") from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(self._jitter(min(_BASE_DELAY * (1 << attempt), _MAX_DELAY)))
                    continue
                raise ValueError("GitLab API connection error") from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(self._jitter(min(_BASE_DELAY * (1 << attempt), _MAX_DELAY)))
                    continue
                raise ValueError(f"GitLab API HTTP error: {exc}") from exc
        raise ValueError("GitLab API request failed after retries") from last_exc

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Compute the delay before the next retry attempt.

        Prefers ``Retry-After``, then GitLab's rate-limit reset headers
        (``RateLimit-ResetTime`` / ``RateLimit-Reset`` — only on HTTP 429, the
        quota reset window), then exponential backoff.

        The quota reset window is left uncapped so a GitLab quota window longer
        than ``_MAX_DELAY`` is truly honoured (capping it would fire the retry
        early and hit another 429). ``Retry-After`` and backoff remain capped
        at ``_MAX_DELAY``.
        """
        if response.status_code == 429:
            reset_delay = _parse_rate_limit_reset(response)
            if reset_delay is not None:
                return reset_delay
        retry_after = _parse_retry_after(response)
        if retry_after is not None:
            return min(retry_after, _MAX_DELAY)
        return min(_BASE_DELAY * (1 << attempt), _MAX_DELAY)

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
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers(), timeout=30)

    @staticmethod
    def _result(records: list[dict[str, Any]], response: httpx.Response, total: int | None = None) -> ConnectorResult:
        """Build a ConnectorResult, wiring pagination cursor + rate-limit metadata."""
        return ConnectorResult(
            records=records,
            total=len(records) if total is None else total,
            next_cursor=_parse_next_page(response),
            metadata={"rate_limit": _rate_limit_metadata(response)},
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/user")
                if r.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid or expired GitLab token (HTTP 401)")
                if r.status_code == 403:
                    return HealthResult(
                        ok=False,
                        detail="Missing scopes: token cannot access /user (needs read_user/api)",
                    )
                if r.status_code != 200:
                    return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

                try:
                    user_info = r.json()
                except json.JSONDecodeError:
                    return HealthResult(ok=False, detail=f"Invalid JSON in /user response: {r.text[:200]}")
                username = user_info.get("username", "")

                projects_r = await client.get("/projects", params={"per_page": 1})
                if projects_r.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid or expired GitLab token (HTTP 401)")
                if projects_r.status_code == 403:
                    return HealthResult(
                        ok=False,
                        detail="Missing scopes: read_api/api not granted (projects API denied)",
                    )
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
                _paginate_params(params, q.cursor)
                r = await self._call_api("GET", "/projects", params=params)
                data = _safe_json(r)
                return self._result(data, r)
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
                return ConnectorResult(records=[info], metadata={"rate_limit": _rate_limit_metadata(r)})
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
                _paginate_params(mr_params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/merge_requests",
                    params=mr_params,
                )
                mrs = _safe_json(r)
                return self._result(mrs, r)
            case "merge_request":
                project = self._require_filter(q.filters, "project", q.resource)
                mr_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/merge_requests/{mr_iid}",
                )
                return ConnectorResult(records=[_safe_json(r)], metadata={"rate_limit": _rate_limit_metadata(r)})
            case "issues":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                params = {"per_page": q.limit}
                for key in ("state", "labels", "milestone", "search", "sort", "order_by", "assignee_id"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                _paginate_params(params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues",
                    params=params,
                )
                issues = _safe_json(r)
                return self._result(issues, r)
            case "issue":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}",
                )
                return ConnectorResult(records=[_safe_json(r)], metadata={"rate_limit": _rate_limit_metadata(r)})
            case "labels":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                label_params: dict[str, Any] = {"per_page": q.limit}
                _paginate_params(label_params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/labels",
                    params=label_params,
                )
                labels = _safe_json(r)
                return self._result(labels, r)
            case "label":
                project = self._require_filter(q.filters, "project", q.resource)
                label_id = self._require_filter(q.filters, "label_id", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/labels/{label_id}",
                )
                return ConnectorResult(records=[_safe_json(r)], metadata={"rate_limit": _rate_limit_metadata(r)})
            case "milestones":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                milestone_params: dict[str, Any] = {"per_page": q.limit}
                _paginate_params(milestone_params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/milestones",
                    params=milestone_params,
                )
                milestones = _safe_json(r)
                return self._result(milestones, r)
            case "issue_notes":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                params = {"per_page": q.limit}
                for key in ("sort", "order_by"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                _paginate_params(params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}/notes",
                    params=params,
                )
                notes = _safe_json(r)
                return self._result(notes, r)
            case "issue_discussions":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                discussion_params: dict[str, Any] = {"per_page": q.limit}
                _paginate_params(discussion_params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}/discussions",
                    params=discussion_params,
                )
                discussions = _safe_json(r)
                return self._result(discussions, r)
            case "branch":
                project = self._require_filter(q.filters, "project", q.resource)
                branch_name = self._require_filter(q.filters, "name", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/branches/{quote(branch_name, safe='')}",
                )
                return ConnectorResult(records=[_safe_json(r)], metadata={"rate_limit": _rate_limit_metadata(r)})
            case "branches":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                branch_params: dict[str, Any] = {"per_page": q.limit}
                _paginate_params(branch_params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/branches",
                    params=branch_params,
                )
                branches = _safe_json(r)
                return self._result(branches, r)
            case "tags":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                tag_params: dict[str, Any] = {"per_page": q.limit}
                _paginate_params(tag_params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/tags",
                    params=tag_params,
                )
                tags = _safe_json(r)
                return self._result(tags, r)
            case "pipelines":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                pipeline_params: dict[str, Any] = {"per_page": q.limit}
                _paginate_params(pipeline_params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/pipelines",
                    params=pipeline_params,
                )
                pipelines = _safe_json(r)
                return self._result(pipelines, r)
            case "jobs":
                project = self._require_filter(q.filters, "project", q.resource)
                pipeline_id = self._require_filter(q.filters, "pipeline_id", q.resource)
                encoded = _project_path(project)
                job_params: dict[str, Any] = {"per_page": q.limit}
                _paginate_params(job_params, q.cursor)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/pipelines/{pipeline_id}/jobs",
                    params=job_params,
                )
                jobs = _safe_json(r)
                return self._result(jobs, r)
            case _:
                raise ValueError(f"Unsupported GitLab resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "file":
                project = self._require_filter(payload.data, "project", payload.resource)
                path = self._require_filter(payload.data, "path", payload.resource)
                encoded = _project_path(project)
                body: dict[str, Any] = {
                    "branch": payload.data.get("ref", "main"),
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
            case "file_delete":
                project = self._require_filter(payload.data, "project", payload.resource)
                path = self._require_filter(payload.data, "path", payload.resource)
                encoded = _project_path(project)
                delete_params: dict[str, Any] = {
                    "branch": payload.data.get("ref", payload.data.get("branch", "main")),
                }
                if payload.data.get("sha"):
                    delete_params["sha"] = payload.data["sha"]
                delete_body: dict[str, Any] = {
                    "commit_message": payload.data.get("message", f"Delete {path} via Modulo"),
                }
                r = await self._call_api(
                    "DELETE",
                    f"/projects/{encoded}/repository/files/{quote(path, safe='')}",
                    params=delete_params,
                    json=delete_body,
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
            case "mr_comment" | "mr_note":
                project = self._require_filter(payload.data, "project", payload.resource)
                mr_iid = self._require_filter(payload.data, "iid", payload.resource)
                note_body = self._require_filter(payload.data, "body", payload.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/merge_requests/{mr_iid}/notes",
                    json={"body": note_body},
                )
                return _safe_json_object(r)
            case "mr_merge":
                project = self._require_filter(payload.data, "project", payload.resource)
                mr_iid = self._require_filter(payload.data, "iid", payload.resource)
                encoded = _project_path(project)
                merge_body: dict[str, Any] = {}
                if "merge_commit_message" in payload.data:
                    merge_body["merge_commit_message"] = payload.data["merge_commit_message"]
                if "squash" in payload.data:
                    merge_body["squash"] = payload.data["squash"]
                if "should_remove_source_branch" in payload.data:
                    merge_body["should_remove_source_branch"] = payload.data["should_remove_source_branch"]
                if "merge_when_pipeline_succeeds" in payload.data:
                    merge_body["merge_when_pipeline_succeeds"] = payload.data["merge_when_pipeline_succeeds"]
                r = await self._call_api(
                    "PUT",
                    f"/projects/{encoded}/merge_requests/{mr_iid}/merge",
                    json=merge_body,
                )
                return _safe_json_object(r)
            case "mr_approve":
                project = self._require_filter(payload.data, "project", payload.resource)
                mr_iid = self._require_filter(payload.data, "iid", payload.resource)
                encoded = _project_path(project)
                approve_body: dict[str, Any] = {}
                if "sha" in payload.data:
                    approve_body["sha"] = payload.data["sha"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/merge_requests/{mr_iid}/approve",
                    json=approve_body,
                )
                return _safe_json_object(r)
            case "mr_labels":
                project = self._require_filter(payload.data, "project", payload.resource)
                mr_iid = self._require_filter(payload.data, "iid", payload.resource)
                labels = self._require_filter(payload.data, "labels", payload.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "PUT",
                    f"/projects/{encoded}/merge_requests/{mr_iid}",
                    json={"labels": labels},
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

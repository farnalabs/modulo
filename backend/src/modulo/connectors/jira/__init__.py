"""JiraConnector — async Jira Cloud / Data Center REST API connector."""

import asyncio
import base64
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from modulo.connectors._retry_headers import (
    BASE_DELAY,
    MAX_DELAY,
    MAX_RETRIES,
    RETRYABLE_STATUSES,
    backoff_delay,
    extract_rate_limit_metadata,
    format_rate_limit_detail,
    parse_rate_limit_reset,
    parse_retry_after,
    should_retry_network,
    should_retry_status,
)
from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)
from modulo.connectors.security import CredentialRedactor, redacting
from modulo.core.ssrf import pinned_async_client_sync

# Retry/backoff configuration (canonical values live in _retry_headers)
_RETRYABLE_STATUSES = RETRYABLE_STATUSES
_MAX_RETRIES = MAX_RETRIES
_BASE_DELAY = BASE_DELAY
_MAX_DELAY = MAX_DELAY

# Jira Cloud reports quota state via X-RateLimit-* headers on every response
_RATE_LIMIT_HEADERS = (
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
)

# Preferred header (epoch seconds) for the quota-reset retry delay.
_RATE_LIMIT_RESET_HEADERS = ("X-RateLimit-Reset",)

# Fallback content-type for attachment downloads (S1192).
_OCTET_STREAM = "application/octet-stream"


def _compute_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Compute retry delay with exponential backoff, jitter, and optional Retry-After."""
    if response:
        retry_after = _parse_retry_after(response)
        if retry_after is not None:
            return float(min(retry_after, _MAX_DELAY))
    jitter = random.uniform(0, 1)  # noqa: S311  # nosec B311 — non-cryptographic jitter for retry delays
    return float(min(_BASE_DELAY * (2**attempt) + jitter, _MAX_DELAY))


def _jitter(delay: float, *, tight: bool = False) -> float:
    """Add jitter to a retry delay.

    Full jitter (``[0, delay)``) is used for exponential backoff to avoid the
    thundering herd. Server-derived waits (quota reset) use tight jitter around
    the requested value so the window is honoured instead of collapsing to a
    near-immediate retry.
    """
    if tight:
        return random.uniform(delay * 0.9, delay)  # noqa: S311  # nosec B311 — non-cryptographic jitter for retry delays
    return random.uniform(0, delay)  # noqa: S311  # nosec B311 — non-cryptographic jitter for retry delays


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from Jira API response."""
    return parse_retry_after(response)


def _parse_rate_limit_reset(response: httpx.Response) -> float | None:
    """Parse Jira Cloud's ``X-RateLimit-Reset`` header (epoch seconds) into a retry delay."""
    return parse_rate_limit_reset(response, _RATE_LIMIT_RESET_HEADERS)


def _rate_limit_detail(response: httpx.Response) -> str:
    """Summarise Jira Cloud ``X-RateLimit-*`` quota headers for error strings."""
    return format_rate_limit_detail(response, _RATE_LIMIT_HEADERS)


def _rate_limit_metadata(response: httpx.Response) -> dict[str, str | None]:
    """Extract Jira Cloud ``X-RateLimit-*`` headers into a metadata dict."""
    return extract_rate_limit_metadata(response, _RATE_LIMIT_HEADERS)


def _require_filter(q: ConnectorQuery, key: str, resource: str) -> Any:
    """Return ``q.filters[key]`` or raise the standard missing-filter error."""
    if key not in q.filters:
        raise ValueError(f"Jira {resource} query requires '{key}' filter")
    return q.filters[key]


def _require_data_key(data: dict[str, Any], key: str, op: str) -> Any:
    """Return ``data[key]`` or raise the standard missing-data-key error."""
    if key not in data:
        raise ValueError(f"Jira {op} requires '{key}' in data")
    return data[key]


def _paginate(body: dict[str, Any], records: list[Any], max_results_fallback: int) -> tuple[int, str | None]:
    """Derive ``(total, next_cursor)`` from a Jira offset-paginated list body.

    Shared cursor-pagination arithmetic for the ``startAt`` + ``maxResults``
    responses: the next cursor is the next offset while the current window
    does not reach ``total``. ``max_results_fallback`` is the ``maxResults``
    value used when the body omits it (the requested page size, or the API
    default of 50).
    """
    total = _safe_int(body.get("total"), len(records))
    start_at = _safe_int(body.get("startAt"), 0)
    max_results = _safe_int(body.get("maxResults"), max_results_fallback)
    next_cursor: str | None = None
    if start_at + max_results < total:
        next_cursor = str(start_at + max_results)
    return total, next_cursor


class JiraConnector(ConnectorBase):
    """Read/write Jira issues via the REST API (Cloud API v3 / Data Center API v2).

    Supports both Jira Cloud and self-hosted Jira Data Center / Server
    instances. Cloud is the default: ``base_url`` defaults to
    ``https://{instance}/rest/api/3``. For a self-hosted instance pass the full
    API base URL (e.g. ``https://jira.example.com/rest/api/2``) via ``base_url``
    or set ``api_version=2``.

    Config (from config_json):
      "instance"    — your-domain.atlassian.net (without https://)
      "base_url"    — optional full API base URL for self-hosted Jira Server /
                      Data Center instances, e.g. "https://jira.example.com/rest/api/2".
                      When omitted, "https://{instance}/rest/api/{api_version}" (Jira Cloud)
                      is used. A bare host (e.g. "https://jira.example.com") has
                      "/rest/api/{api_version}" appended automatically.
      "api_version" — optional REST API version, default 3 (Cloud)

    Credentials (from credentials_ciphertext):
      "email"    — Atlassian account email (for Basic auth)
      "api_token" — Atlassian API token (for Basic auth)
    Or:
      "token"    — OAuth/Personal Access Token

    Supported query resources:
      "issue"               — get a single issue; filters: {"issue_key": "PROJ-123"}
      "search"              — JQL search; filters: {"jql": "project = PROJ", "max_results": 50}
      "issue_comments"      — list comments on an issue; filters: {"issue_key": "PROJ-123"}
      "issue_attachments"   — list attachments on an issue; filters: {"issue_key": "PROJ-123"}
      "issue_remote_links"  — list remote links on an issue; filters: {"issue_key": "PROJ-123"}
      "transitions"         — get available transitions for an issue; filters: {"issue_key": "PROJ-123"}
      "projects"            — list accessible projects
      "project_components"  — list components for a project; filters: {"project": "PROJ"}
      "project_versions"    — list versions/releases for a project; filters: {"project": "PROJ"}
      "field_metadata"      — issue types + create-issue fields for a project; filters: {"project": "PROJ"}
      "fields"              — list all system + custom fields across the instance
      "statuses"            — issue types + their statuses for a project; filters: {"project": "PROJ"}
      "attachments"         — list attachments on an issue; filters: {"issue_key": "PROJ-123"}
      "attachment"          — download an attachment's content (base64); filters: {"attachment_id": "10001"}

    Supported write resources:
      "issue"           — create an issue; data: {"project": {"key": "PROJ"}, "summary": "...",
                           "issuetype": {"name": "Task"}, ...}
      "issue_update"    — update an issue; data: {"issue_key": "PROJ-123", "fields": {...}}
      "issue_comment"   — add a comment to an issue; data: {"issue_key": "PROJ-123", "body": "..."}
      "transition"      — transition an issue; data: {"issue_key": "PROJ-123", "transition_id": "..."}
      "issue_assign"    — assign an issue to an account; data: {"issue_key": "PROJ-123",
                           "account_id": "712020:...", "email": "a@example.com", "display_name": "..."}
                           (all three id lookups accepted; explicit null/unassign flag removes the assignee)
      "issue_label"     — add/remove labels; data: {"issue_key": "PROJ-123", "add": ["bug"], "remove": [...]}
      "issue_delete"    — delete an issue; data: {"issue_key": "PROJ-123"}
      "issue_attachment" — upload an attachment; data: {"issue_key": "PROJ-123", "filename": "a.txt",
                           "content": "..." or "file": <bytes>} (exactly one of content/file)
      "issue_remote_link" — add a remote link; data: {"issue_key": "PROJ-123", "url": "https://...",
                           "title": "..."}
      "remote_link_delete" — delete a remote link; data: {"issue_key": "PROJ-123", "link_id": "..."}
      "attachment"      — upload an attachment; data: {"issue_key": "PROJ-123", "filename": "a.txt",
                           "content": "..." | "file": <bytes>, "mime_type": optional}

    Query results expose ``metadata["rate_limit"]`` mirroring Jira Cloud's
    ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset``
    response headers when present (empty dict when absent; Jira Data Center
    does not report these headers). On HTTP 429 the connector waits until
    ``X-RateLimit-Reset`` instead of blind backoff.
    """

    def __init__(
        self,
        instance: str = "",
        creds: dict[str, str] | None = None,
        *,
        base_url: str | None = None,
        api_version: int | str = 3,
    ) -> None:
        creds = creds or {}
        self._instance = instance.rstrip("/")
        if base_url:
            normalized = base_url.rstrip("/")
            if "/rest/api/" not in normalized:
                normalized = f"{normalized}/rest/api/{api_version}"
            self._base_url = normalized
        elif self._instance:
            self._base_url = f"https://{self._instance}/rest/api/{api_version}"
        else:
            raise ValueError("JiraConnector requires 'instance' or 'base_url'")
        self._auth: httpx.Auth | None = None
        self._token: str | None = None

        if "token" in creds:
            self._token = creds["token"]
        elif "email" in creds and "api_token" in creds:
            self._auth = httpx.BasicAuth(username=creds["email"], password=creds["api_token"])
        else:
            raise ValueError(
                "Jira credentials must contain either 'token' (PAT/OAuth) or 'email' + 'api_token' (Basic auth)",
            )
        self._redactor = CredentialRedactor.from_creds(creds)

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.JIRA

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-512): validate + resolve the base_url's host and
        # pin the validated IP onto the transport so the connection never
        # re-resolves at connect time (closes DNS-rebind). ``trust_env=False``
        # stops a proxy from re-resolving the destination and defeating the pin.
        return pinned_async_client_sync(
            self._base_url,
            base_url=self._base_url,
            headers=self._headers(),
            auth=self._auth,
            timeout=30,
        )

    async def _attempt_request(
        self,
        method: str,
        path: str,
        attempt: int,
        **kwargs: Any,
    ) -> httpx.Response | None:
        """Perform one API request attempt.

        Returns the successful response, or ``None`` when this attempt signalled
        a retry (the retry delay has already been slept). Raises ``ValueError``
        on a 304 Not Modified — the resource is unchanged, never retried.
        """
        async with self._client() as client:
            r = await client.request(method, path, **kwargs)
            if r.status_code == 304:
                raise ValueError("Jira API returned 304 Not Modified — resource unchanged")
            if should_retry_status(r.status_code, attempt):
                await asyncio.sleep(self._sleep_delay(r, attempt))
                return None
            r.raise_for_status()
            return r

    async def _handle_status_error(self, exc: httpx.HTTPStatusError, attempt: int) -> bool:
        """Handle an ``httpx.HTTPStatusError`` for the current attempt.

        Returns ``True`` when the caller should retry (the server-suggested
        delay has already been slept); raises a redacted ``ValueError`` when
        the terminal status must surface instead.
        """
        if should_retry_status(exc.response.status_code, attempt):
            await asyncio.sleep(self._sleep_delay(exc.response, attempt))
            return True
        detail = exc.response.text[:200]
        if exc.response.status_code == 429:
            quota = _rate_limit_detail(exc.response)
            if quota:
                detail = f"{detail} (quota: {quota})"
        raise ValueError(self._redactor.redact(f"Jira API HTTP {exc.response.status_code}: {detail}")) from exc

    async def _retry_after_network_failure(self, attempt: int) -> bool:
        """Sleep and signal a retry when the network-attempt budget allows, else ``False``."""
        if not should_retry_network(attempt):
            return False
        await asyncio.sleep(_jitter(backoff_delay(attempt)))
        return True

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Call Jira API with retry/backoff for retryable statuses.

        Retries on 429, 502, 503, 504 with exponential backoff + jitter.
        On 429 responses, prefers ``Retry-After`` then Jira Cloud's
        ``X-RateLimit-Reset`` (quota window) to compute the wait instead of
        blind backoff. Wraps HTTP/network/parse errors as ValueError.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                result = await self._attempt_request(method, path, attempt, **kwargs)
                if result is not None:
                    return result
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if await self._handle_status_error(exc, attempt):
                    continue
            except httpx.TimeoutException as exc:
                last_exc = exc
                if await self._retry_after_network_failure(attempt):
                    continue
                raise ValueError("Jira API timeout") from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if await self._retry_after_network_failure(attempt):
                    continue
                raise ValueError("Jira API connection error") from exc
        raise ValueError("Jira API request failed after retries") from last_exc

    @staticmethod
    def _sleep_delay(response: httpx.Response, attempt: int) -> float:
        """Compute the sleep before a retry, honouring server-provided wait times.

        On HTTP 429 with Jira Cloud's ``X-RateLimit-Reset`` present, wait until
        the quota window resets (tight jitter so the window is honoured).
        Otherwise fall back to ``_compute_delay`` (``Retry-After`` then
        exponential backoff + jitter).
        """
        if response.status_code == 429:
            reset_delay = _parse_rate_limit_reset(response)
            if reset_delay is not None:
                return _jitter(reset_delay, tight=True)
        return _compute_delay(attempt, response)

    async def _parse_json(self, response: httpx.Response) -> Any:
        """Safely parse JSON response, wrapping decode errors."""
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(self._redactor.redact(f"Jira API invalid response: {response.text[:200]}")) from exc

    @redacting
    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the current user's profile."""
        try:
            r = await self._call_api("GET", "/myself")
            user_info = await self._parse_json(r)
            display_name = user_info.get("displayName", "")
            return HealthResult(ok=True, detail=display_name)
        except ValueError as exc:
            return health_check_failure(self._redactor.redact_exc(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(self._redactor.redact_exc(exc))

    @redacting
    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        """Dispatch a read query to its per-resource handler."""
        handler = _QUERY_HANDLERS.get(q.resource)
        if handler is None:
            raise ValueError(f"Unsupported Jira resource: {q.resource!r}")
        return await handler(self, q)

    async def _query_issue(self, q: ConnectorQuery) -> ConnectorResult:
        """Get a single issue by ``issue_key``."""
        issue_key = _require_filter(q, "issue_key", "issue")
        r = await self._call_api("GET", f"/issue/{issue_key}")
        data: dict[str, Any] = await self._parse_json(r)
        return ConnectorResult(
            records=[data],
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_search(self, q: ConnectorQuery) -> ConnectorResult:
        """Run a JQL search with cursor pagination."""
        jql = q.filters.get("jql", "")
        max_results = q.filters.get("max_results", q.limit)
        params: dict[str, Any] = {"jql": jql, "maxResults": max_results}
        if q.cursor:
            params["startAt"] = int(q.cursor)
        r = await self._call_api("POST", "/search", json=params)
        payload: dict[str, Any] = await self._parse_json(r)
        issues = payload.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        total, next_cursor = _paginate(payload, issues, max_results)
        return ConnectorResult(
            records=issues,
            total=total,
            next_cursor=next_cursor,
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_issue_comments(self, q: ConnectorQuery) -> ConnectorResult:
        """List comments on an issue with cursor pagination."""
        issue_key = _require_filter(q, "issue_key", "issue_comments")
        comment_params: dict[str, Any] = {}
        if q.cursor:
            comment_params["startAt"] = int(q.cursor)
        r = await self._call_api("GET", f"/issue/{issue_key}/comment", params=comment_params)
        body = await self._parse_json(r)
        comments = body.get("comments", [])
        if not isinstance(comments, list):
            comments = []
        total, next_cursor = _paginate(body, comments, 50)
        return ConnectorResult(
            records=comments,
            total=total,
            next_cursor=next_cursor,
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_transitions(self, q: ConnectorQuery) -> ConnectorResult:
        """List available transitions for an issue."""
        issue_key = _require_filter(q, "issue_key", "transitions")
        r = await self._call_api("GET", f"/issue/{issue_key}/transitions")
        body = await self._parse_json(r)
        transitions = body.get("transitions", [])
        return ConnectorResult(
            records=transitions,
            total=len(transitions),
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_issue_attachments(self, q: ConnectorQuery) -> ConnectorResult:
        """List attachments on an issue via the issue's ``fields.attachment``."""
        issue_key = _require_filter(q, "issue_key", "issue_attachments")
        r = await self._call_api("GET", f"/issue/{issue_key}")
        body = await self._parse_json(r)
        attachments = body.get("fields", {}).get("attachment") or []
        return ConnectorResult(
            records=attachments,
            total=len(attachments),
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_issue_remote_links(self, q: ConnectorQuery) -> ConnectorResult:
        """List remote links on an issue (list or ``links``-keyed response)."""
        issue_key = _require_filter(q, "issue_key", "issue_remote_links")
        r = await self._call_api("GET", f"/issue/{issue_key}/remotelink")
        body = await self._parse_json(r)
        remote_links: list[Any] = body if isinstance(body, list) else body.get("links", [])
        return ConnectorResult(
            records=remote_links,
            total=len(remote_links),
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_project_listing(self, q: ConnectorQuery, resource: str, sub_path: str) -> ConnectorResult:
        """List one of a project's sub-resources (components / versions / statuses).

        Shared body of the structurally identical project-subresource handlers:
        require the ``project`` filter, GET ``/project/{project}/{sub_path}``,
        parse a bare list response, and expose the project in the metadata.
        """
        project = _require_filter(q, "project", resource)
        r = await self._call_api("GET", f"/project/{project}/{sub_path}")
        data = await self._parse_json(r)
        records: list[Any] = data if isinstance(data, list) else []
        return ConnectorResult(
            records=records,
            total=len(records),
            metadata={
                "rate_limit": _rate_limit_metadata(r),
                "project": project,
            },
        )

    async def _query_project_components(self, q: ConnectorQuery) -> ConnectorResult:
        """List components for a project."""
        return await self._query_project_listing(q, "project_components", "components")

    async def _query_project_versions(self, q: ConnectorQuery) -> ConnectorResult:
        """List versions/releases for a project."""
        return await self._query_project_listing(q, "project_versions", "versions")

    async def _query_projects(self, q: ConnectorQuery) -> ConnectorResult:
        """List accessible projects (list or ``values``-keyed response)."""
        r = await self._call_api("GET", "/project")
        data = await self._parse_json(r)
        projects = data if isinstance(data, list) else data.get("values", [])
        return ConnectorResult(
            records=projects,
            total=len(projects),
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_field_metadata(self, q: ConnectorQuery) -> ConnectorResult:
        """List issue types + create-issue fields for a project (createmeta)."""
        project = _require_filter(q, "project", "field_metadata")
        createmeta_params: dict[str, Any] = {
            "projectKeys": project,
            "expand": "projects.issuetypes.fields",
        }
        r = await self._call_api("GET", "/issue/createmeta", params=createmeta_params)
        body = await self._parse_json(r)
        projects_meta = body.get("projects", [])
        issue_types = projects_meta[0].get("issuetypes", []) if projects_meta else []
        return ConnectorResult(
            records=issue_types,
            total=len(issue_types),
            metadata={
                "rate_limit": _rate_limit_metadata(r),
                "project": project,
            },
        )

    async def _query_fields(self, q: ConnectorQuery) -> ConnectorResult:
        """List all system + custom fields across the instance."""
        r = await self._call_api("GET", "/field")
        data = await self._parse_json(r)
        fields: list[Any] = data if isinstance(data, list) else []
        return ConnectorResult(
            records=fields,
            total=len(fields),
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_statuses(self, q: ConnectorQuery) -> ConnectorResult:
        """List issue types + their statuses for a project."""
        return await self._query_project_listing(q, "statuses", "statuses")

    async def _query_attachments(self, q: ConnectorQuery) -> ConnectorResult:
        """List attachments on an issue (fields-scoped fetch)."""
        issue_key = _require_filter(q, "issue_key", "attachments")
        r = await self._call_api("GET", f"/issue/{issue_key}", params={"fields": "attachment"})
        body = await self._parse_json(r)
        attachments = body.get("fields", {}).get("attachment", [])
        return ConnectorResult(
            records=attachments,
            total=len(attachments),
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    async def _query_attachment(self, q: ConnectorQuery) -> ConnectorResult:
        """Download an attachment's content (base64-encoded)."""
        attachment_id = _require_filter(q, "attachment_id", "attachment")
        r = await self._call_api("GET", f"/attachment/{attachment_id}/content")
        content_type = r.headers.get("content-type", _OCTET_STREAM)
        encoded = base64.b64encode(r.content).decode("ascii")
        return ConnectorResult(
            records=[
                {
                    "attachment_id": attachment_id,
                    "content": encoded,
                    "encoding": "base64",
                    "content_type": content_type,
                }
            ],
            total=1,
            metadata={"rate_limit": _rate_limit_metadata(r)},
        )

    @redacting
    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Dispatch a write operation to its per-resource handler."""
        handler = _WRITE_HANDLERS.get(payload.resource)
        if handler is None:
            raise ValueError(f"Unsupported Jira write resource: {payload.resource!r}")
        return await handler(self, payload)

    async def _write_issue(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Create an issue."""
        r = await self._call_api("POST", "/issue", json=payload.data)
        created: dict[str, Any] = await self._parse_json(r)
        return created

    async def _write_issue_update(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Update an issue's fields."""
        issue_key = _require_data_key(payload.data, "issue_key", "issue update")
        fields: dict[str, Any] = payload.data.get("fields", {})
        await self._call_api("PUT", f"/issue/{issue_key}", json={"fields": fields})
        return {"issue_key": issue_key, "updated": True}

    async def _write_issue_assign(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Assign (or unassign) an issue's assignee."""
        issue_key = _require_data_key(payload.data, "issue_key", "issue assign")
        assignee_field = await self._resolve_assignee(payload.data)
        await self._call_api("PUT", f"/issue/{issue_key}", json={"fields": {"assignee": assignee_field}})
        return {"issue_key": issue_key, "assignee": assignee_field}

    async def _write_issue_label(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Add/remove labels on an issue (set semantics against the current labels)."""
        issue_key = _require_data_key(payload.data, "issue_key", "issue label")
        add_labels = payload.data.get("add") or []
        remove_labels = payload.data.get("remove") or []
        if not add_labels and not remove_labels:
            raise ValueError("Jira issue_label requires 'add' and/or 'remove' in data")
        target_labels = await self._compute_target_labels(issue_key, add_labels, remove_labels)
        await self._call_api("PUT", f"/issue/{issue_key}", json={"fields": {"labels": target_labels}})
        return {"issue_key": issue_key, "labels": target_labels}

    async def _write_issue_delete(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Delete an issue."""
        issue_key = _require_data_key(payload.data, "issue_key", "issue delete")
        await self._call_api("DELETE", f"/issue/{issue_key}")
        return {"issue_key": issue_key, "deleted": True}

    async def _write_issue_comment(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Add a comment to an issue."""
        issue_key = _require_data_key(payload.data, "issue_key", "issue comment")
        if "body" not in payload.data:
            raise ValueError("Jira issue comment requires 'body' in data")
        body = payload.data["body"]
        r = await self._call_api("POST", f"/issue/{issue_key}/comment", json={"body": body})
        comment: dict[str, Any] = await self._parse_json(r)
        return comment

    async def _write_issue_attachment(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Upload an attachment to an issue."""
        return await self._upload_attachment(payload.data)

    async def _write_issue_remote_link(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Add a remote link to an issue."""
        issue_key = _require_data_key(payload.data, "issue_key", "issue_remote_link")
        if "url" not in payload.data:
            raise ValueError("Jira issue_remote_link requires 'url' in data")
        link_object: dict[str, Any] = {"url": payload.data["url"]}
        if "title" in payload.data:
            link_object["title"] = payload.data["title"]
        r = await self._call_api(
            "POST",
            f"/issue/{issue_key}/remotelink",
            json={"object": link_object},
        )
        remote_link: dict[str, Any] = await self._parse_json(r)
        return remote_link

    async def _write_remote_link_delete(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Delete a remote link from an issue."""
        issue_key = _require_data_key(payload.data, "issue_key", "remote_link_delete")
        if "link_id" not in payload.data:
            raise ValueError("Jira remote_link_delete requires 'link_id' in data")
        link_id = payload.data["link_id"]
        await self._call_api("DELETE", f"/issue/{issue_key}/remotelink/{link_id}")
        return {"issue_key": issue_key, "link_id": link_id, "deleted": True}

    async def _write_transition(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Transition an issue via its transition id."""
        issue_key = _require_data_key(payload.data, "issue_key", "transition")
        if "transition_id" not in payload.data:
            raise ValueError("Jira transition requires 'transition_id' in data")
        transition_id = payload.data["transition_id"]
        await self._call_api(
            "POST",
            f"/issue/{issue_key}/transitions",
            json={"transition": {"id": transition_id}},
        )
        return {"issue_key": issue_key, "transitioned": True}

    async def _write_attachment(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Upload an attachment and report it against the issue key."""
        uploaded = await self._upload_attachment(payload.data)
        attachments = uploaded if isinstance(uploaded, list) else [uploaded]
        return {"issue_key": payload.data["issue_key"], "attachments": attachments}

    async def _upload_attachment(self, data: dict[str, Any]) -> dict[str, Any]:
        """Upload a file as an issue attachment via the Jira attachments API.

        Accepts ``issue_key`` + ``filename`` plus exactly one of ``content``
        (str) or ``file`` (bytes/str). Optional ``mime_type`` sets the upload
        content type (defaults to octet-stream). Optional extra keys
        (e.g. ``comment``) are passed through as multipart form fields. Sends
        the ``X-Atlassian-Token: no-check`` header Jira requires for attachment
        uploads to bypass XSRF protection.
        """
        issue_key = _require_data_key(data, "issue_key", "issue attachment")
        if "filename" not in data:
            raise ValueError("Jira issue attachment requires 'filename' in data")
        filename = data["filename"]
        content = data.get("content")
        file_content = data.get("file")
        if content is None and file_content is None:
            raise ValueError("Jira issue attachment requires 'content' or 'file' in data")
        if content is not None and file_content is not None:
            raise ValueError("Jira issue attachment must provide exactly one of 'content' or 'file'")
        raw = content if content is not None else file_content
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        mime_type = data.get("mime_type") or _OCTET_STREAM
        files: dict[str, Any] = {"file": (filename, raw_bytes, mime_type)}
        form_data = {
            k: v for k, v in data.items() if k not in ("issue_key", "filename", "content", "file", "mime_type")
        }
        r = await self._call_api(
            "POST",
            f"/issue/{issue_key}/attachments",
            files=files,
            data=form_data,
            headers={"X-Atlassian-Token": "no-check"},
        )
        uploaded: dict[str, Any] = await self._parse_json(r)
        return uploaded

    async def _resolve_assignee(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve the ``assignee`` field value for an issue.

        Accepts ``account_id`` (direct), ``email`` or ``display_name`` (looked up
        via the Jira user-search API), or an explicit ``unassign`` flag / ``null``
        ``account_id`` to clear the assignee. Returns ``None`` to unassign.
        """
        if "account_id" in data:
            if data["account_id"] is None:
                return None
            return {"accountId": data["account_id"]}
        if "email" in data or "display_name" in data:
            query = data.get("email") or data.get("display_name")
            key = "email" if "email" in data else "display_name"
            r = await self._call_api("GET", "/user/search", params={"query": query, "maxResults": 1})
            users = await self._parse_json(r)
            if not isinstance(users, list) or not users:
                raise ValueError(f"Jira user not found for {key} {query!r}")
            account_id = users[0].get("accountId")
            if not account_id:
                raise ValueError(f"Jira user search for {key} {query!r} returned no accountId")
            return {"accountId": account_id}
        if data.get("unassign"):
            return None
        raise ValueError("Jira issue_assign requires 'account_id', 'email', 'display_name', or 'unassign' in data")

    async def _compute_target_labels(
        self,
        issue_key: str,
        add: list[str],
        remove: list[str],
    ) -> list[str]:
        """Compute the target label set for an issue.

        Jira's ``labels`` field is a *set* — PUT replaces the full list. To make
        ``issue_label`` a true add/remove (not a replace), the current labels are
        fetched first and the target set computed from them.
        """
        r = await self._call_api("GET", f"/issue/{issue_key}")
        body = await self._parse_json(r)
        current = body.get("fields", {}).get("labels") or []
        remove_set = frozenset(remove)
        target = [label for label in current if label not in remove_set]
        for label in add:
            if label not in target:
                target.append(label)
        return target


# Per-resource dispatch tables for :meth:`JiraConnector.query` / :meth:`JiraConnector.write`
# (resource string → unbound handler; unknown resources raise ValueError in the dispatcher).
_QUERY_HANDLERS: dict[str, Callable[[JiraConnector, ConnectorQuery], Awaitable[ConnectorResult]]] = {
    "issue": JiraConnector._query_issue,
    "search": JiraConnector._query_search,
    "issue_comments": JiraConnector._query_issue_comments,
    "transitions": JiraConnector._query_transitions,
    "issue_attachments": JiraConnector._query_issue_attachments,
    "issue_remote_links": JiraConnector._query_issue_remote_links,
    "project_components": JiraConnector._query_project_components,
    "project_versions": JiraConnector._query_project_versions,
    "projects": JiraConnector._query_projects,
    "field_metadata": JiraConnector._query_field_metadata,
    "fields": JiraConnector._query_fields,
    "statuses": JiraConnector._query_statuses,
    "attachments": JiraConnector._query_attachments,
    "attachment": JiraConnector._query_attachment,
}

_WRITE_HANDLERS: dict[str, Callable[[JiraConnector, ConnectorPayload], Awaitable[dict[str, Any]]]] = {
    "issue": JiraConnector._write_issue,
    "issue_update": JiraConnector._write_issue_update,
    "issue_assign": JiraConnector._write_issue_assign,
    "issue_label": JiraConnector._write_issue_label,
    "issue_delete": JiraConnector._write_issue_delete,
    "issue_comment": JiraConnector._write_issue_comment,
    "issue_attachment": JiraConnector._write_issue_attachment,
    "issue_remote_link": JiraConnector._write_issue_remote_link,
    "remote_link_delete": JiraConnector._write_remote_link_delete,
    "transition": JiraConnector._write_transition,
    "attachment": JiraConnector._write_attachment,
}

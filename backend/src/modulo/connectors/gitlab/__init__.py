"""GitLabConnector — async GitLab API connector via REST API v4."""

import asyncio
import base64
import contextlib
import json
import random
import re
import time
from collections.abc import Iterable
from typing import Any, cast
from urllib.parse import quote

import httpx

from modulo.connectors._retry_headers import (
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
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.security import CredentialRedactor, redacting
from modulo.core.ssrf import pinned_async_client_sync

_GITLAB_API = "https://gitlab.com/api/v4"

REQUIRED_SCOPES = frozenset({"read_api", "write_repository", "api"})

# GitLab's ``api`` scope grants full API access, subsuming the read and
# repository-write scopes — a token declaring ``api`` satisfies them all.
_SCOPE_SUPERSETS: dict[str, frozenset[str]] = {
    "api": frozenset({"read_api", "write_repository"}),
}

# GitLab RateLimit-* headers reported on API responses
_RATE_LIMIT_HEADERS = (
    "RateLimit-Limit",
    "RateLimit-Remaining",
    "RateLimit-Observed",
    "RateLimit-Reset",
    "RateLimit-ResetTime",
)

# Preferred headers (epoch seconds) for the quota-reset retry delay, in order.
_RATE_LIMIT_RESET_HEADERS = ("RateLimit-ResetTime", "RateLimit-Reset")

# Retry/backoff configuration (canonical values live in _retry_headers)
_RETRYABLE_STATUSES = RETRYABLE_STATUSES
_MAX_RETRIES = MAX_RETRIES
_MAX_DELAY = MAX_DELAY

# Actions accepted by the Commits API for multi-file (batch) operations
_COMMIT_ACTIONS = frozenset({"create", "update", "delete", "move", "chmod"})

# Per-operation scope requirements. GitLab PAT scopes are coarse: ``api``
# grants full read/write API access, ``write_repository`` grants repository
# (git + repository-files API) writes, and ``read_api`` is read-only.
# Repository-file writes require ``write_repository`` (the ``api`` superset
# also satisfies them); every other write operation requires ``api``.
_WRITE_SCOPE_REQUIREMENTS: dict[str, str] = {
    "file": "write_repository",
    "files": "write_repository",
    "commit": "write_repository",
    "file_delete": "write_repository",
    "mr": "api",
    "merge_request": "api",
    "mr_comment": "api",
    "mr_note": "api",
    "mr_merge": "api",
    "mr_approve": "api",
    "mr_approval_request": "api",
    "mr_labels": "api",
    "issue": "api",
    "issue_update": "api",
    "issue_note": "api",
    "issue_label": "api",
    "label": "api",
    "milestone": "api",
    "pipeline_run": "api",
}

# How long declared-scope results are cached before a write re-probes the
# instance. Bounds per-write token-info round-trips to one per window.
_SCOPE_CACHE_TTL = 300.0


def _instance_root(base_url: str) -> str:
    """Derive the GitLab instance root from an API base URL.

    The Doorkeeper token-introspection endpoint (``/oauth/token/info``) lives
    at the instance root, *outside* the versioned API path — i.e.
    ``https://gitlab.com/oauth/token/info`` rather than
    ``https://gitlab.com/api/v4/oauth/token/info``. Strip a trailing
    ``/api/vN`` segment when present so the root is correct for both hosted and
    self-hosted instances (including those mounted under a reverse-proxy path).
    """
    root = base_url.rstrip("/")
    return re.sub(r"/api/v\d+$", "", root) or root


def _effective_scopes(declared: frozenset[str]) -> frozenset[str]:
    """Expand declared scopes through the known superset relations.

    ``api`` implies ``read_api`` and ``write_repository`` on GitLab, so a token
    declaring only ``api`` satisfies all of ``REQUIRED_SCOPES``.
    """
    effective = set(declared)
    for scope in declared:
        effective.update(_SCOPE_SUPERSETS.get(scope, ()))
    return frozenset(effective)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from GitLab API response."""
    return parse_retry_after(response)


def _parse_rate_limit_reset(response: httpx.Response) -> float | None:
    """Parse GitLab's rate-limit reset header (epoch seconds) into a retry delay."""
    return parse_rate_limit_reset(response, _RATE_LIMIT_RESET_HEADERS)


def _rate_limit_detail(response: httpx.Response) -> str:
    """Summarise GitLab rate-limit quota headers for error/health detail strings."""
    return format_rate_limit_detail(response, _RATE_LIMIT_HEADERS)


def _rate_limit_metadata(response: httpx.Response) -> dict[str, str | None]:
    """Extract GitLab ``RateLimit-*`` headers into a metadata dict."""
    return extract_rate_limit_metadata(response, _RATE_LIMIT_HEADERS)


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


def _request_id(response: httpx.Response) -> str | None:
    """Read GitLab's ``X-Request-Id`` header for support debugging.

    GitLab tags every API response (including errors) with a request id that
    support can correlate against server logs. Surfacing it on failures makes
    it possible to open a meaningful support ticket.
    """
    value = response.headers.get("X-Request-Id")
    return value or None


def _error_detail(response: httpx.Response) -> str:
    """Build an error detail string, appending the request id when present."""
    detail = response.text[:200]
    request_id = _request_id(response)
    if request_id:
        detail = f"{detail} (request_id: {request_id})"
    return detail


def _id_suffix(response: httpx.Response) -> str:
    """Return the `` (request_id: ...)`` suffix for a response, if reported."""
    request_id = _request_id(response)
    return f" (request_id: {request_id})" if request_id else ""


def _validate_path(path: str, resource: str) -> None:
    """Reject path traversal attempts before they reach the GitLab API.

    GitLab validates repository file paths server-side, but a local check
    gives a fast, unambiguous error and prevents ``..`` segments from ever
    being URL-encoded and sent to the repository-files endpoints.
    """
    if not isinstance(path, str) or not path:
        raise ValueError(f"GitLab resource {resource!r} requires a non-empty 'path'")
    if path.startswith(("/", "\\")):
        raise ValueError(f"GitLab resource {resource!r}: path must be relative: {path!r}")
    normalized = path.replace("\\", "/")
    if any(segment == ".." for segment in normalized.split("/")):
        raise ValueError(f"GitLab resource {resource!r}: path traversal blocked: {path!r}")


def _paginate_params(params: dict[str, Any], cursor: str | None) -> None:
    """Add GitLab page param from a pagination cursor, if present."""
    if cursor:
        try:
            params["page"] = int(cursor)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid GitLab pagination cursor: {cursor!r}") from None


def _copy_optional(source: dict[str, Any], target: dict[str, Any], keys: Iterable[str]) -> None:
    """Copy present keys from ``source`` into ``target``, skipping absent ones."""
    for key in keys:
        if key in source:
            target[key] = source[key]


def _safe_json(response: httpx.Response) -> Any:
    """Safely parse JSON response, handling decode errors."""
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"GitLab API invalid response: {exc}") from exc


def _safe_json_object(response: httpx.Response) -> dict[str, Any]:
    return cast("dict[str, Any]", _safe_json(response))


def _single_result(response: httpx.Response) -> ConnectorResult:
    """Build a single-record ConnectorResult carrying rate-limit metadata."""
    return ConnectorResult(records=[_safe_json(response)], metadata={"rate_limit": _rate_limit_metadata(response)})


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff delay (capped at ``_MAX_DELAY``) for a retry attempt."""
    return backoff_delay(attempt)


def _should_retry_status(status_code: int, attempt: int) -> bool:
    """Whether a retryable HTTP status may be retried on this attempt."""
    return should_retry_status(status_code, attempt)


def _should_retry_attempt(attempt: int) -> bool:
    """Whether a transport-level failure may be retried on this attempt."""
    return should_retry_network(attempt)


def _http_error_message(exc: httpx.HTTPStatusError) -> str:
    """Build the ValueError detail for an HTTPStatusError, adding quota info on 429."""
    detail = _error_detail(exc.response)
    if exc.response.status_code == 429:
        quota = _rate_limit_detail(exc.response)
        if quota:
            detail = f"{detail} (quota: {quota})"
    return f"GitLab API HTTP {exc.response.status_code}: {detail}"


def _parse_scope_field(raw: Any) -> frozenset[str]:
    """Parse a token-info ``scope``/``scopes`` value into a declared-scope set.

    Accepts a space-delimited string or a list/tuple of scope strings. Anything
    else (``None``, non-collection, empty) yields an empty set.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(s for s in raw if isinstance(s, str) and s)


def _project_path(project_id: str) -> str:
    """URL-encode a project path like 'group/subgroup/project'.

    Numeric project IDs are accepted (coerced to strings) because GitLab
    resolves them. ``None``, booleans, empty/whitespace-only strings, and other
    non-scalar values raise a descriptive ``ValueError`` instead of a raw
    ``TypeError`` from ``quote()``.
    """
    if project_id is None or isinstance(project_id, bool):
        raise ValueError(f"GitLab 'project' filter must be a project ID or path, got {project_id!r}")
    if not isinstance(project_id, str):
        if not isinstance(project_id, (int, float)):
            raise ValueError(f"GitLab 'project' filter must be a project ID or path, got {project_id!r}")
        project_id = str(project_id)
    if not project_id.strip():
        raise ValueError("GitLab 'project' filter must be a non-empty project ID or path")
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
      "tree"              — list repository tree entries (optional recursive + path filters)
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
      "mr_changes"        — get the diff/changed files of a merge request (records[0]["changes"])
      "branch"            — get single branch
      "branches"          — list branches
      "tags"              — list tags
      "pipelines"         — list pipelines
      "jobs"              — list jobs for a pipeline

    Supported write resources:
      "file"              — create/update a file
      "files"             — batch file operations in one commit via the Commits API (create/update/delete/move)
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
      "mr_approval_request" — request approval from specific users via an approval rule
                             (data: project, iid, user_ids/user_emails)
      "mr_comment"        — add a comment to a merge request (data: project, iid, body)
      "mr_note"           — add a comment to a merge request (data: project, iid, body)
      "mr_labels"         — set labels on a merge request (data: project, iid, labels)
      "pipeline_run"      — trigger a pipeline
    """

    def __init__(self, token: str, base_url: str = _GITLAB_API) -> None:
        if not token or not token.strip():
            raise ValueError("GitLabConnector requires a non-empty token")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._scope_cache: tuple[float, frozenset[str]] | None = None
        self._redactor = CredentialRedactor([token])

    @staticmethod
    def _jitter(delay: float, *, tight: bool = False) -> float:
        """Add jitter to a retry delay.

        Full jitter (``[0, delay)``) is used for exponential backoff to avoid
        the thundering herd. Server-derived waits (quota reset / Retry-After)
        use tight jitter around the requested value so the window is honoured
        instead of being collapsed to near-immediate retries.
        """
        if tight:
            return random.uniform(delay * 0.9, delay)  # noqa: S311  # nosec B311 — non-cryptographic jitter for retry delays
        return random.uniform(0, delay)  # noqa: S311  # nosec B311 — non-cryptographic jitter for retry delays

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
                    if _should_retry_status(r.status_code, attempt):
                        await asyncio.sleep(self._sleep_delay(r, attempt))
                        continue
                    r.raise_for_status()
                    return r
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if _should_retry_status(exc.response.status_code, attempt):
                    await asyncio.sleep(self._sleep_delay(exc.response, attempt))
                    continue
                raise ValueError(self._redactor.redact(_http_error_message(exc))) from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if _should_retry_attempt(attempt):
                    await asyncio.sleep(self._jitter(_backoff_delay(attempt)))
                    continue
                raise ValueError("GitLab API timeout") from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if _should_retry_attempt(attempt):
                    await asyncio.sleep(self._jitter(_backoff_delay(attempt)))
                    continue
                raise ValueError("GitLab API connection error") from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if _should_retry_attempt(attempt):
                    await asyncio.sleep(self._jitter(_backoff_delay(attempt)))
                    continue
                raise ValueError(self._redactor.redact(f"GitLab API HTTP error: {exc}")) from exc
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
        return _backoff_delay(attempt)

    async def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        """Safely parse JSON response, wrapping decode errors."""
        return cast("dict[str, Any]", _safe_json(response))

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITLAB

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-512): validate + resolve the base_url's host and
        # pin the validated IP onto the transport so the connection never
        # re-resolves at connect time (closes DNS-rebind). ``trust_env=False``
        # stops a proxy from re-resolving the destination and defeating the pin.
        return pinned_async_client_sync(
            self._base_url,
            base_url=self._base_url,
            headers=self._headers(),
            timeout=30,
        )

    async def _declared_effective_scopes(self, client: httpx.AsyncClient) -> frozenset[str]:
        """Return the token's declared scopes, expanded through superset relations.

        Reads the token's declared scopes from the instance ``/oauth/token/info``
        endpoint (best-effort, reusing the open *client*). When the endpoint is
        unavailable (older self-hosted versions return 404), unreachable, or
        returns no usable scope list, returns an empty set so callers degrade
        to the endpoint probes already performed.
        """
        root = _instance_root(self._base_url)
        try:
            r = await client.get(f"{root}/oauth/token/info")
        except (httpx.RequestError, ValueError):
            return frozenset()
        if not r.is_success:
            return frozenset()
        try:
            info = r.json()
        except json.JSONDecodeError:
            return frozenset()
        if not isinstance(info, dict):
            return frozenset()
        declared = _parse_scope_field(info.get("scope", info.get("scopes")))
        if not declared:
            return frozenset()
        return _effective_scopes(declared)

    async def _probe_declared_scopes(self) -> frozenset[str]:
        """Probe the instance for the token's declared scopes via a fresh client.

        Strictly best-effort and never raises: any failure (network error,
        non-2xx, unparseable body, unavailable endpoint, or a test harness
        rejecting the request) yields an empty set so callers treat an unknown
        scope set as "allow and let the GitLab API enforce".
        """
        try:
            async with self._client() as client:
                return await self._declared_effective_scopes(client)
        except Exception:
            return frozenset()

    async def _declared_scopes_cached(self) -> frozenset[str]:
        """Return the token's effective declared scopes, cached for ``_SCOPE_CACHE_TTL``.

        Populates the cache with a best-effort probe on first use. A failure to
        determine the scopes caches an empty set (unknown) so write verification
        degrades to allow rather than failing closed on a flaky endpoint.
        """
        now = time.monotonic()
        if self._scope_cache is not None:
            cached_at, scopes = self._scope_cache
            if now - cached_at < _SCOPE_CACHE_TTL:
                return scopes
        scopes = await self._probe_declared_scopes()
        self._scope_cache = (now, scopes)
        return scopes

    async def verify_write_scopes(self, resource: str) -> frozenset[str]:
        """Return the scopes a write resource requires that the token lacks.

        Probes the instance for the token's declared scopes (cached per instance
        for ``_SCOPE_CACHE_TTL`` seconds). Returns an empty set when the token
        satisfies the requirement or when the token's declared scopes cannot be
        determined (best-effort — the GitLab API enforces scopes on every call).
        """
        required = _WRITE_SCOPE_REQUIREMENTS.get(resource)
        if required is None:
            return frozenset()
        declared = await self._declared_scopes_cached()
        if not declared:
            return frozenset()
        if required in declared:
            return frozenset()
        return frozenset({required})

    async def _ensure_write_scope(self, resource: str) -> None:
        """Fail fast when the token lacks the scope a write operation requires.

        Best-effort: when the token's declared scopes cannot be determined the
        check is skipped (the API still enforces scope). Declared scopes are
        cached per instance so the token-info round-trip happens at most once
        per ``_SCOPE_CACHE_TTL`` window rather than on every write.
        """
        required = _WRITE_SCOPE_REQUIREMENTS.get(resource)
        if required is None:
            return
        declared = await self._declared_scopes_cached()
        if not declared or required in declared:
            return
        raise ValueError(
            f"GitLab write resource {resource!r} requires scope {required!r}; "
            f"token declares: {', '.join(sorted(declared))}. "
            "Grant the scope or use a token with the 'api' scope, which covers "
            "all GitLab write operations.",
        )

    @staticmethod
    def _result(records: list[dict[str, Any]], response: httpx.Response, total: int | None = None) -> ConnectorResult:
        """Build a ConnectorResult, wiring pagination cursor + rate-limit metadata."""
        return ConnectorResult(
            records=records,
            total=len(records) if total is None else total,
            next_cursor=_parse_next_page(response),
            metadata={"rate_limit": _rate_limit_metadata(response)},
        )

    def _user_health_result(self, r: httpx.Response) -> HealthResult | None:
        """Map a ``/user`` response to a failure HealthResult, or ``None`` when healthy."""
        if r.status_code == 401:
            detail = f"Invalid or expired GitLab token (HTTP 401){_id_suffix(r)}"
        elif r.status_code == 403:
            detail = "Missing scopes: token cannot access /user (needs read_user/api)" + _id_suffix(r)
        elif r.status_code != 200:
            detail = f"HTTP {r.status_code}: {_error_detail(r)}"
        else:
            return None
        return HealthResult(ok=False, detail=self._redactor.redact(detail))

    def _projects_health_result(self, r: httpx.Response) -> HealthResult | None:
        """Map a ``/projects`` response to a failure HealthResult, or ``None`` when healthy."""
        if r.status_code == 401:
            detail = f"Invalid or expired GitLab token (HTTP 401){_id_suffix(r)}"
        elif r.status_code == 403:
            detail = "Missing scopes: read_api/api not granted (projects API denied)" + _id_suffix(r)
        elif not r.is_success:
            detail = f"Projects API returned HTTP {r.status_code}: {_error_detail(r)}"
        else:
            return None
        return HealthResult(ok=False, detail=self._redactor.redact(detail))

    @staticmethod
    def _scope_missing_result(declared_scopes: frozenset[str]) -> HealthResult | None:
        """Return a missing-scopes HealthResult, or ``None`` when satisfied/unknown."""
        if not declared_scopes:
            return None
        missing_scopes = REQUIRED_SCOPES - declared_scopes
        if not missing_scopes:
            return None
        return HealthResult(
            ok=False,
            detail=(
                "Missing scopes: "
                + ", ".join(sorted(missing_scopes))
                + f". Required: {', '.join(sorted(REQUIRED_SCOPES))}"
            ),
        )

    async def _probe_instance_version(self, client: httpx.AsyncClient) -> str | None:
        """Best-effort fetch of the instance version (self-hosted diagnostics only).

        Never fails the health check: a missing or inaccessible ``/version``
        endpoint simply yields ``None``.
        """
        if self._base_url == _GITLAB_API:
            return None
        try:
            version_r = await client.get("/version")
            if version_r.is_success:
                version_info = _safe_json(version_r)
                if isinstance(version_info, dict):
                    return version_info.get("version")
        except (httpx.RequestError, ValueError):
            pass
        return None

    @redacting
    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/user")
                user_health = self._user_health_result(r)
                if user_health is not None:
                    return user_health

                try:
                    user_info = r.json()
                except json.JSONDecodeError:
                    return HealthResult(
                        ok=False, detail=self._redactor.redact(f"Invalid JSON in /user response: {r.text[:200]}")
                    )
                username = user_info.get("username", "")

                projects_r = await client.get("/projects", params={"per_page": 1})
                projects_health = self._projects_health_result(projects_r)
                if projects_health is not None:
                    return projects_health

                # Diagnostic-only: report the instance version (most useful for
                # self-hosted GitLab). Best-effort — a missing/inaccessible
                # /version endpoint must never fail the health check.
                version = await self._probe_instance_version(client)

                # Scope verification — read the token's declared scopes from the
                # Doorkeeper token-introspection endpoint so the
                # write_repository/api scopes can be reported individually
                # instead of only being inferred from endpoint HTTP status.
                # Also warms the instance scope cache so the first write() after
                # a successful health check is verified without re-probing.
                declared_scopes = await self._declared_effective_scopes(client)
                if declared_scopes:
                    self._scope_cache = (time.monotonic(), declared_scopes)
                scope_health = self._scope_missing_result(declared_scopes)
                if scope_health is not None:
                    return scope_health

            detail = username
            if version:
                detail = f"{username} (GitLab {version})"
            return HealthResult(ok=True, detail=detail)
        except httpx.RequestError as e:
            return HealthResult(ok=False, detail=self._redactor.redact(str(e)))
        except ValueError as e:
            # The outbound SSRF guard in ``_client`` rejects a private/internal
            # base_url by raising. A health check must REPORT that as unhealthy
            # (with the remediation text the guard produced), never propagate it.
            return HealthResult(ok=False, detail=self._redactor.redact(str(e))[:200])

    @redacting
    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "projects":
                return await self._query_projects(q)
            case "file":
                return await self._query_file(q)
            case "tree":
                return await self._query_tree(q)
            case "mrs" | "merge_requests":
                return await self._query_merge_requests(q)
            case "merge_request":
                return await self._query_merge_request(q)
            case "mr_changes":
                return await self._query_mr_changes(q)
            case "issues":
                return await self._query_issues(q)
            case "issue":
                return await self._query_issue(q)
            case "labels":
                return await self._query_labels(q)
            case "label":
                return await self._query_label(q)
            case "milestones":
                return await self._query_milestones(q)
            case "issue_notes":
                return await self._query_issue_notes(q)
            case "issue_discussions":
                return await self._query_issue_discussions(q)
            case "branch":
                return await self._query_branch(q)
            case "branches":
                return await self._query_branches(q)
            case "tags":
                return await self._query_tags(q)
            case "pipelines":
                return await self._query_pipelines(q)
            case "jobs":
                return await self._query_jobs(q)
            case _:
                raise ValueError(f"Unsupported GitLab resource: {q.resource!r}")

    @staticmethod
    def _list_params(q: ConnectorQuery, filter_keys: Iterable[str] = ()) -> dict[str, Any]:
        """Build base list-query params: per-page limit, optional filters, and cursor."""
        params: dict[str, Any] = {"per_page": q.limit}
        _copy_optional(q.filters, params, filter_keys)
        _paginate_params(params, q.cursor)
        return params

    def _query_project_encoded(self, q: ConnectorQuery) -> str:
        """Return the URL-encoded project path required by a query resource."""
        project = self._require_filter(q.filters, "project", q.resource)
        return _project_path(project)

    def _write_project_encoded(self, payload: ConnectorPayload) -> str:
        """Return the URL-encoded project path required by a write resource."""
        project = self._require_filter(payload.data, "project", payload.resource)
        return _project_path(project)

    async def _query_projects(self, q: ConnectorQuery) -> ConnectorResult:
        """List projects accessible to the token."""
        params = self._list_params(q, ("search", "membership", "visibility", "owned"))
        r = await self._call_api("GET", "/projects", params=params)
        data = _safe_json(r)
        return self._result(data, r)

    async def _query_file(self, q: ConnectorQuery) -> ConnectorResult:
        """Read a single repository file, base64-decoding its content."""
        project = self._require_filter(q.filters, "project", q.resource)
        path = self._require_filter(q.filters, "path", q.resource)
        _validate_path(path, q.resource)
        ref = q.filters.get("ref", "main")
        encoded = _project_path(project)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/repository/files/{quote(path, safe='')}",
            params={"ref": ref},
        )
        info = _safe_json(r)
        if "content" in info:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                info["content"] = base64.b64decode(info["content"]).decode("utf-8")
        return ConnectorResult(records=[info], metadata={"rate_limit": _rate_limit_metadata(r)})

    async def _query_tree(self, q: ConnectorQuery) -> ConnectorResult:
        """List repository tree entries (optional recursive + path filters)."""
        encoded = self._query_project_encoded(q)
        tree_params: dict[str, Any] = {"per_page": q.limit}
        if "path" in q.filters:
            path = q.filters["path"]
            _validate_path(path, q.resource)
            tree_params["path"] = path
        if "ref" in q.filters:
            tree_params["ref"] = q.filters["ref"]
        if q.filters.get("recursive"):
            tree_params["recursive"] = True
        _paginate_params(tree_params, q.cursor)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/repository/tree",
            params=tree_params,
        )
        entries = _safe_json(r)
        return self._result(entries, r)

    async def _query_merge_requests(self, q: ConnectorQuery) -> ConnectorResult:
        """List project merge requests (filters: state, labels, milestone)."""
        encoded = self._query_project_encoded(q)
        mr_params = self._list_params(q, ("state", "labels", "milestone"))
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/merge_requests",
            params=mr_params,
        )
        mrs = _safe_json(r)
        return self._result(mrs, r)

    async def _query_merge_request(self, q: ConnectorQuery) -> ConnectorResult:
        """Get a single merge request by IID."""
        project = self._require_filter(q.filters, "project", q.resource)
        mr_iid = self._require_filter(q.filters, "iid", q.resource)
        encoded = _project_path(project)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/merge_requests/{mr_iid}",
        )
        return _single_result(r)

    async def _query_mr_changes(self, q: ConnectorQuery) -> ConnectorResult:
        """Get the diff/changed files of a merge request."""
        project = self._require_filter(q.filters, "project", q.resource)
        mr_iid = self._require_filter(q.filters, "iid", q.resource)
        encoded = _project_path(project)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/merge_requests/{mr_iid}/changes",
        )
        return _single_result(r)

    async def _query_issues(self, q: ConnectorQuery) -> ConnectorResult:
        """List project issues (filters: state, labels, milestone, search, sort, order_by, assignee_id)."""
        encoded = self._query_project_encoded(q)
        params = self._list_params(
            q,
            ("state", "labels", "milestone", "search", "sort", "order_by", "assignee_id"),
        )
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/issues",
            params=params,
        )
        issues = _safe_json(r)
        return self._result(issues, r)

    async def _query_issue(self, q: ConnectorQuery) -> ConnectorResult:
        """Get a single issue by IID."""
        project = self._require_filter(q.filters, "project", q.resource)
        issue_iid = self._require_filter(q.filters, "iid", q.resource)
        encoded = _project_path(project)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/issues/{issue_iid}",
        )
        return _single_result(r)

    async def _query_labels(self, q: ConnectorQuery) -> ConnectorResult:
        """List project labels."""
        encoded = self._query_project_encoded(q)
        label_params = self._list_params(q)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/labels",
            params=label_params,
        )
        labels = _safe_json(r)
        return self._result(labels, r)

    async def _query_label(self, q: ConnectorQuery) -> ConnectorResult:
        """Get a single label by ID."""
        project = self._require_filter(q.filters, "project", q.resource)
        label_id = self._require_filter(q.filters, "label_id", q.resource)
        encoded = _project_path(project)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/labels/{label_id}",
        )
        return _single_result(r)

    async def _query_milestones(self, q: ConnectorQuery) -> ConnectorResult:
        """List project milestones."""
        encoded = self._query_project_encoded(q)
        milestone_params = self._list_params(q)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/milestones",
            params=milestone_params,
        )
        milestones = _safe_json(r)
        return self._result(milestones, r)

    async def _query_issue_notes(self, q: ConnectorQuery) -> ConnectorResult:
        """List notes on an issue."""
        encoded = self._query_project_encoded(q)
        issue_iid = self._require_filter(q.filters, "iid", q.resource)
        params = self._list_params(q, ("sort", "order_by"))
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/issues/{issue_iid}/notes",
            params=params,
        )
        notes = _safe_json(r)
        return self._result(notes, r)

    async def _query_issue_discussions(self, q: ConnectorQuery) -> ConnectorResult:
        """List discussions on an issue."""
        encoded = self._query_project_encoded(q)
        issue_iid = self._require_filter(q.filters, "iid", q.resource)
        discussion_params = self._list_params(q)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/issues/{issue_iid}/discussions",
            params=discussion_params,
        )
        discussions = _safe_json(r)
        return self._result(discussions, r)

    async def _query_branch(self, q: ConnectorQuery) -> ConnectorResult:
        """Get a single branch by name."""
        project = self._require_filter(q.filters, "project", q.resource)
        branch_name = self._require_filter(q.filters, "name", q.resource)
        encoded = _project_path(project)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/repository/branches/{quote(branch_name, safe='')}",
        )
        return _single_result(r)

    async def _query_branches(self, q: ConnectorQuery) -> ConnectorResult:
        """List repository branches."""
        encoded = self._query_project_encoded(q)
        branch_params = self._list_params(q)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/repository/branches",
            params=branch_params,
        )
        branches = _safe_json(r)
        return self._result(branches, r)

    async def _query_tags(self, q: ConnectorQuery) -> ConnectorResult:
        """List repository tags."""
        encoded = self._query_project_encoded(q)
        tag_params = self._list_params(q)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/repository/tags",
            params=tag_params,
        )
        tags = _safe_json(r)
        return self._result(tags, r)

    async def _query_pipelines(self, q: ConnectorQuery) -> ConnectorResult:
        """List pipelines for a project."""
        encoded = self._query_project_encoded(q)
        pipeline_params = self._list_params(q)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/pipelines",
            params=pipeline_params,
        )
        pipelines = _safe_json(r)
        return self._result(pipelines, r)

    async def _query_jobs(self, q: ConnectorQuery) -> ConnectorResult:
        """List jobs for a pipeline."""
        encoded = self._query_project_encoded(q)
        pipeline_id = self._require_filter(q.filters, "pipeline_id", q.resource)
        job_params = self._list_params(q)
        r = await self._call_api(
            "GET",
            f"/projects/{encoded}/pipelines/{pipeline_id}/jobs",
            params=job_params,
        )
        jobs = _safe_json(r)
        return self._result(jobs, r)

    @redacting
    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        await self._ensure_write_scope(payload.resource)
        match payload.resource:
            case "file":
                return await self._write_file(payload)
            case "files" | "commit":
                return await self._write_files(payload)
            case "file_delete":
                return await self._write_file_delete(payload)
            case "mr" | "merge_request":
                return await self._write_merge_request(payload)
            case "mr_comment" | "mr_note":
                return await self._write_mr_comment(payload)
            case "mr_merge":
                return await self._write_mr_merge(payload)
            case "mr_approve":
                return await self._write_mr_approve(payload)
            case "mr_approval_request":
                return await self._write_mr_approval_request(payload)
            case "mr_labels":
                return await self._write_mr_labels(payload)
            case "issue":
                return await self._write_issue(payload)
            case "issue_update":
                return await self._write_issue_update(payload)
            case "issue_note":
                return await self._write_issue_note(payload)
            case "issue_label":
                return await self._write_issue_label(payload)
            case "label":
                return await self._write_label(payload)
            case "milestone":
                return await self._write_milestone(payload)
            case "pipeline_run":
                return await self._write_pipeline_run(payload)
            case _:
                raise ValueError(f"Unsupported GitLab write resource: {payload.resource!r}")

    async def _write_file(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Create/update a repository file."""
        project = self._require_filter(payload.data, "project", payload.resource)
        path = self._require_filter(payload.data, "path", payload.resource)
        _validate_path(path, payload.resource)
        encoded = _project_path(project)
        body: dict[str, Any] = {
            "branch": payload.data.get("ref", payload.data.get("branch", "main")),
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

    @staticmethod
    def _normalize_commit_action(action: Any, resource: str) -> dict[str, Any]:
        """Validate and normalise a single Commits-API action into its wire payload."""
        if not isinstance(action, dict):
            raise ValueError(f"GitLab resource {resource!r}: each action must be an object")
        action_type = action.get("action")
        if action_type not in _COMMIT_ACTIONS:
            raise ValueError(
                f"GitLab resource {resource!r}: action {action_type!r} must be one of {sorted(_COMMIT_ACTIONS)}",
            )
        file_path = action.get("file_path")
        if not file_path:
            raise ValueError(f"GitLab resource {resource!r}: each action requires 'file_path'")
        _validate_path(file_path, resource)
        normalized: dict[str, Any] = {"action": action_type, "file_path": file_path}
        if "content" in action:
            normalized["content"] = action["content"]
        if action_type == "move":
            previous_path = action.get("previous_path")
            if not previous_path:
                msg = f"GitLab resource {resource!r}: move action requires 'previous_path'"
                raise ValueError(msg)
            _validate_path(previous_path, resource)
            normalized["previous_path"] = previous_path
        return normalized

    async def _write_files(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Batch file operations in one commit via the Commits API."""
        encoded = self._write_project_encoded(payload)
        actions = self._require_filter(payload.data, "actions", payload.resource)
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"GitLab resource {payload.resource!r} requires a non-empty 'actions' list")
        commit_body: dict[str, Any] = {
            "branch": payload.data.get("ref", payload.data.get("branch", "main")),
            "commit_message": payload.data.get("message", "Update via Modulo"),
            "actions": [self._normalize_commit_action(a, payload.resource) for a in actions],
        }
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/repository/commits",
            json=commit_body,
        )
        return _safe_json_object(r)

    async def _write_file_delete(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Delete a repository file."""
        project = self._require_filter(payload.data, "project", payload.resource)
        path = self._require_filter(payload.data, "path", payload.resource)
        _validate_path(path, payload.resource)
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

    async def _write_merge_request(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Create a merge request."""
        encoded = self._write_project_encoded(payload)
        source_branch = self._require_filter(payload.data, "source_branch", payload.resource)
        title = self._require_filter(payload.data, "title", payload.resource)
        body = {
            "source_branch": source_branch,
            "target_branch": payload.data.get("target_branch", "main"),
            "title": title,
        }
        _copy_optional(payload.data, body, ("description",))
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/merge_requests",
            json=body,
        )
        return _safe_json_object(r)

    async def _write_mr_comment(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Add a note to a merge request."""
        encoded = self._write_project_encoded(payload)
        mr_iid = self._require_filter(payload.data, "iid", payload.resource)
        note_body = self._require_filter(payload.data, "body", payload.resource)
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/merge_requests/{mr_iid}/notes",
            json={"body": note_body},
        )
        return _safe_json_object(r)

    async def _write_mr_merge(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Merge a merge request."""
        encoded = self._write_project_encoded(payload)
        mr_iid = self._require_filter(payload.data, "iid", payload.resource)
        merge_body: dict[str, Any] = {}
        _copy_optional(
            payload.data,
            merge_body,
            ("merge_commit_message", "squash", "should_remove_source_branch", "merge_when_pipeline_succeeds"),
        )
        r = await self._call_api(
            "PUT",
            f"/projects/{encoded}/merge_requests/{mr_iid}/merge",
            json=merge_body,
        )
        return _safe_json_object(r)

    async def _write_mr_approve(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Approve a merge request."""
        encoded = self._write_project_encoded(payload)
        mr_iid = self._require_filter(payload.data, "iid", payload.resource)
        approve_body: dict[str, Any] = {}
        _copy_optional(payload.data, approve_body, ("sha",))
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/merge_requests/{mr_iid}/approve",
            json=approve_body,
        )
        return _safe_json_object(r)

    async def _write_mr_approval_request(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Request approval from specific users via an approval rule."""
        encoded = self._write_project_encoded(payload)
        mr_iid = self._require_filter(payload.data, "iid", payload.resource)
        user_ids = payload.data.get("user_ids") or []
        user_emails = payload.data.get("user_emails") or []
        if not user_ids and not user_emails:
            raise ValueError(
                f"GitLab resource {payload.resource!r} requires 'user_ids' and/or 'user_emails'",
            )
        rule_body: dict[str, Any] = {
            "name": payload.data.get("name", "Requested approvers"),
            "rule_type": "approval",
            "approvals_required": payload.data.get("approvals_required", 1),
        }
        if user_ids:
            rule_body["user_ids"] = user_ids
        if user_emails:
            rule_body["user_emails"] = user_emails
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/merge_requests/{mr_iid}/approval_rules",
            json=rule_body,
        )
        return _safe_json_object(r)

    async def _write_mr_labels(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Set labels on a merge request."""
        encoded = self._write_project_encoded(payload)
        mr_iid = self._require_filter(payload.data, "iid", payload.resource)
        labels = self._require_filter(payload.data, "labels", payload.resource)
        r = await self._call_api(
            "PUT",
            f"/projects/{encoded}/merge_requests/{mr_iid}",
            json={"labels": labels},
        )
        return _safe_json_object(r)

    async def _write_issue(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Create an issue."""
        encoded = self._write_project_encoded(payload)
        title = self._require_filter(payload.data, "title", payload.resource)
        body = {
            "title": title,
        }
        _copy_optional(payload.data, body, ("description", "labels", "milestone_id", "assignee_ids"))
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/issues",
            json=body,
        )
        return _safe_json_object(r)

    async def _write_issue_update(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Update an issue (close/reopen, edit title/description)."""
        encoded = self._write_project_encoded(payload)
        issue_iid = self._require_filter(payload.data, "iid", payload.resource)
        body: dict[str, Any] = {}
        _copy_optional(payload.data, body, ("state_event", "title", "description"))
        r = await self._call_api(
            "PUT",
            f"/projects/{encoded}/issues/{issue_iid}",
            json=body,
        )
        return _safe_json_object(r)

    async def _write_issue_note(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Add a note to an issue."""
        encoded = self._write_project_encoded(payload)
        issue_iid = self._require_filter(payload.data, "iid", payload.resource)
        note_body = self._require_filter(payload.data, "body", payload.resource)
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/issues/{issue_iid}/notes",
            json={"body": note_body},
        )
        return _safe_json_object(r)

    async def _write_issue_label(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Replace labels on an issue."""
        encoded = self._write_project_encoded(payload)
        issue_iid = self._require_filter(payload.data, "iid", payload.resource)
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

    async def _write_label(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Create a project label."""
        encoded = self._write_project_encoded(payload)
        name = self._require_filter(payload.data, "name", payload.resource)
        body = {
            "name": name,
            "color": payload.data.get("color", "#428BCA"),
        }
        _copy_optional(payload.data, body, ("description",))
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/labels",
            json=body,
        )
        return _safe_json_object(r)

    async def _write_milestone(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Create a project milestone."""
        encoded = self._write_project_encoded(payload)
        title = self._require_filter(payload.data, "title", payload.resource)
        body = {
            "title": title,
        }
        _copy_optional(payload.data, body, ("description", "due_date"))
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/milestones",
            json=body,
        )
        return _safe_json_object(r)

    async def _write_pipeline_run(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Trigger a pipeline."""
        encoded = self._write_project_encoded(payload)
        ref = self._require_filter(payload.data, "ref", payload.resource)
        body = {
            "ref": ref,
        }
        _copy_optional(payload.data, body, ("variables",))
        r = await self._call_api(
            "POST",
            f"/projects/{encoded}/pipeline",
            json=body,
        )
        return _safe_json_object(r)

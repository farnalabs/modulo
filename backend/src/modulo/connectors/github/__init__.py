"""GitHubConnector — async GitHub API connector."""

import asyncio
import base64
import binascii
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn, cast

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
    should_retry_status,
)
from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.connectors.base import (
    CompensationContext,
    CompensationOperation,
    CompensationOutcome,
    CompensationResult,
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_GITHUB_API = "https://api.github.com"
_API_VERSION = "2022-11-28"


def _search_total(body: dict[str, Any]) -> int | None:
    """Extract GitHub Search's ``total_count`` as a safe int.

    Guards against non-finite floats (``inf``/``nan``) which otherwise poison
    aggregation — Python's json parser produces ``inf`` for overflowing
    literals such as ``1e999``, so a corrupt or hostile response must not be
    able to poison the reported total. A missing ``total_count`` keeps the
    historical ``None`` behaviour.
    """
    raw = body.get("total_count")
    if raw is None:
        return None
    return _safe_int(raw)


REQUIRED_SCOPES = frozenset({"repo", "read:org"})

# PRD §7.11 minimum permissions for fine-grained PATs. Fine-grained PATs use a
# different permission system from classic PAT scopes — GitHub prefixes them with
# ``github_pat_`` and never returns the classic ``X-OAuth-Scopes`` header for them.
REQUIRED_FINE_GRAINED_PERMISSIONS = frozenset({"contents:read", "contents:write", "pull_requests:write"})

_FINE_GRAINED_PAT_PREFIX = "github_pat_"


def is_fine_grained_pat(token: str) -> bool:
    """True when *token* is a fine-grained personal access token.

    GitHub prefixes fine-grained PATs with ``github_pat_`` (classic PATs use
    ``ghp_``). Fine-grained PATs grant per-repository *permissions* instead of
    classic OAuth *scopes*, so they must never be verified against the classic
    ``X-OAuth-Scopes`` header — GitHub does not return it for them.
    """
    return token.startswith(_FINE_GRAINED_PAT_PREFIX)


# Actions accepted by the batch commit resource (write("commit") / write("files"))
_COMMIT_ACTIONS = frozenset({"create", "update", "delete", "move"})

# Review events accepted by the PR review resource (write("pr_review"))
_REVIEW_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES", "COMMENT"})

# Retry/backoff configuration (canonical values live in _retry_headers)
_RETRYABLE_STATUSES = RETRYABLE_STATUSES
_MAX_RETRIES = MAX_RETRIES
_MAX_DELAY = MAX_DELAY

# Circuit breaker configuration — a sustained run of service-level failures
# (server errors, exhausted rate limits, transport failures) opens the circuit
# so the connector fails fast instead of hammering an unhealthy API.
_CIRCUIT_FAILURE_THRESHOLD = 5
_CIRCUIT_COOLDOWN_SECONDS = 30.0

# Link header regex for pagination
_LINK_HEADER_RE = re.compile(r'<([^>]+)>\s*;\s*rel="(\w+)"')

# GitHub X-RateLimit-* headers reported on API responses
_RATE_LIMIT_HEADERS = (
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "X-RateLimit-Used",
    "X-RateLimit-Resource",
)

# Preferred header (epoch seconds) for the quota-reset retry delay.
_RATE_LIMIT_RESET_HEADERS = ("X-RateLimit-Reset",)


class GitHubError(ValueError):
    """Base class for all GitHub connector errors.

    Carries a machine-parseable ``error_code`` so callers can branch on
    failure modes (expired token, missing scope, rate limit, network) without
    parsing human-readable messages. ``status_code`` holds the GitHub HTTP
    status when the error originated from an HTTP response (``None`` for
    transport-level failures and local validation).
    """

    error_code = "github_error"

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code
        self.status_code = status_code


class GitHubAPIError(GitHubError):
    """Raised when GitHub returns a non-retryable business-level HTTP error."""

    error_code = "api_error"


class GitHubRateLimitError(GitHubAPIError):
    """Raised when GitHub rate-limits the request and automatic retries are exhausted."""

    error_code = "rate_limited"


class GitHubAuthError(GitHubAPIError):
    """Raised when the token is invalid/expired (HTTP 401) or lacks required scopes (HTTP 403).

    The ``error_code`` distinguishes the two auth failure modes:
    ``token_expired`` (401 — bad credentials) vs ``insufficient_scope``
    (403 — the token is valid but lacks the required permission).
    """

    error_code = "authentication_failed"


class GitHubNotFoundError(GitHubAPIError):
    """Raised when GitHub reports a resource does not exist (HTTP 404)."""

    error_code = "not_found"


class GitHubNetworkError(GitHubError):
    """Raised on transport-level failures (timeout, connection error)."""

    error_code = "network_error"


class GitHubCircuitOpenError(GitHubError):
    """Raised when the circuit breaker is open and the call fails fast.

    Indicates the upstream API is in a sustained failure state (>= the
    configured failure threshold of consecutive service-level errors) and the
    connector refuses to keep contacting it until the cooldown elapses.
    ``retry_after_seconds`` is the remaining cooldown before a half-open probe
    is allowed (``None`` when unknown).
    """

    error_code = "circuit_open"

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message, error_code="circuit_open")
        self.retry_after_seconds = retry_after_seconds


def _error_for_status(status_code: int, detail: str) -> GitHubError:
    """Map a GitHub HTTP status to the matching structured error type."""
    if status_code == 429:
        return GitHubRateLimitError(detail, status_code=429)
    if status_code == 401:
        return GitHubAuthError(detail, status_code=401, error_code="token_expired")
    if status_code == 403:
        return GitHubAuthError(detail, status_code=403, error_code="insufficient_scope")
    if status_code == 404:
        return GitHubNotFoundError(detail, status_code=404)
    return GitHubAPIError(detail, status_code=status_code)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from GitHub API response."""
    return parse_retry_after(response)


def _parse_rate_limit_reset(response: httpx.Response) -> float | None:
    """Parse GitHub's rate-limit reset header (epoch seconds) into a retry delay."""
    return parse_rate_limit_reset(response, _RATE_LIMIT_RESET_HEADERS)


def _rate_limit_detail(response: httpx.Response) -> str:
    """Summarise GitHub rate-limit quota headers for error/health detail strings."""
    return format_rate_limit_detail(response, _RATE_LIMIT_HEADERS)


def _rate_limit_metadata(response: httpx.Response) -> dict[str, str | None]:
    """Extract GitHub ``X-RateLimit-*`` headers into a metadata dict."""
    return extract_rate_limit_metadata(response, _RATE_LIMIT_HEADERS)


def _parse_link_header(response: httpx.Response) -> dict[str, str]:
    """Parse GitHub Link header into {rel: url} dict."""
    link_value = response.headers.get("Link", "")
    if not link_value:
        return {}
    return {rel: url for url, rel in _LINK_HEADER_RE.findall(link_value)}


def _validate_path(path: Any, resource: str) -> str:
    """Validate a repository file path before it reaches the GitHub API.

    Rejects absolute paths and ``..`` segments locally so a path traversal
    attempt is blocked with a fast, unambiguous error instead of relying only
    on GitHub's server-side validation.
    """
    if not isinstance(path, str) or not path:
        raise ValueError(f"GitHub resource {resource!r} requires a non-empty 'path'")
    if path.startswith(("/", "\\")):
        raise ValueError(f"GitHub resource {resource!r}: path must be relative: {path!r}")
    normalized = path.replace("\\", "/")
    if any(segment == ".." for segment in normalized.split("/")):
        raise ValueError(f"GitHub resource {resource!r}: path traversal blocked: {path!r}")
    return path


def _encode_write_content(data: dict[str, Any], resource: str) -> str:
    """Encode file content for the GitHub Contents API (which requires base64).

    Accepts either ``content`` (raw text, base64-encoded here) or
    ``content_base64`` (already base64-encoded, passed through unchanged for
    binary content). Exactly one of the two is required.
    """
    has_raw = "content" in data
    has_encoded = "content_base64" in data
    if has_raw and has_encoded:
        raise ValueError(f"GitHub {resource} write requires exactly one of 'content' or 'content_base64' in data")
    if has_encoded:
        value = data["content_base64"]
        if not isinstance(value, str) or not value:
            raise ValueError(f"GitHub {resource} write field 'content_base64' must be a non-empty string")
        return value
    if has_raw:
        value = data["content"]
        if not isinstance(value, str):
            raise ValueError(f"GitHub {resource} write field 'content' must be a string")
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    raise ValueError(f"GitHub {resource} write requires 'content' (raw text) or 'content_base64' (pre-encoded) in data")


def _is_base64_text(info: dict[str, Any]) -> bool:
    """True when a Contents API payload carries base64-encoded text content."""
    return info.get("encoding") == "base64" and isinstance(info.get("content"), str)


def _decode_read_content(info: Any) -> None:
    """Decode base64 file content from the GitHub Contents API in place.

    Text files are returned with ``encoding == "base64"``; the decoded UTF-8
    text replaces ``content`` so agents can consume it directly. Content that
    is not decodable text (binary blobs) is left as the raw base64 value.
    Non-object responses (e.g. a directory listing array) are left untouched.
    """
    if not isinstance(info, dict):
        return
    if not _is_base64_text(info):
        return
    try:
        info["content"] = base64.b64decode(info["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return


class GitHubConnector(ConnectorBase):
    """Read/write GitHub via the REST API.

    Supports configurable ``base_url`` for GHES (default: ``https://api.github.com``).
    Retries 429/502/503/504 with exponential backoff + jitter (max 3 retries).
    Includes random jitter in retry delays to avoid thundering herd.
    Parses Link header for pagination cursor on list endpoints.

    Supported query resources:
      "repos"           — list repositories accessible to the token
      "file"            — read a file; filters: {"repo": "owner/repo", "path": "...", "ref": "main"}
                         (base64 content is decoded to UTF-8 text)
      "tree"            — recursive file/directory listing; filters: {"repo": "owner/repo",
                         "ref": "main", "path": "subdir", "recursive": true}
      "pulls"           — list pull requests; filters: {"repo": ..., "state": "open", "sort": ..., "direction": ...}
      "pr_commits"      — list commits on a PR; filters: {"repo": ..., "pull_number": ...}
      "pr_files"        — list changed files on a PR; filters: {"repo": ..., "pull_number": ...}
      "issues"          — list issues; filters: {"repo": ..., "state": ..., "labels": ..., ...}
      "issue"           — get a single issue; filters: {"repo": ..., "issue_number": ...}
      "labels"          — list labels; filters: {"repo": ...}
      "milestones"      — list milestones; filters: {"repo": ..., "state": ..., "sort": ...}
      "issue_comments"  — list issue comments; filters: {"repo": ..., "issue_number": ...}
      "issue_events"    — list issue events; filters: {"repo": ..., "issue_number": ...}
      "assignees"       — list assignees; filters: {"repo": ...}
      "timeline"        — list issue timeline; filters: {"repo": ..., "issue_number": ...}
      "pr_diff"         — get raw diff of a PR; filters: {"repo": ..., "pull_number": ...}
      "search_issues"   — search issues via the Search API; filters: {"q": ..., optional sort/order/state/labels}
                          (returns total_count and Link-header pagination)
      "rate_limit"      — current rate-limit budget via GET /rate_limit; records[0] is the full
                          {"core": ..., "search": ..., ...} resources map

    Every query result exposes ``metadata["rate_limit"]`` — the ``X-RateLimit-*`` headers
    reported by GitHub on the response (limit/remaining/used/reset/resource) so agents can
    schedule work within the remaining quota window.

    Supported write resources:
      "commit"          — batch file operations in one commit via the Git Database API
                           (create/update/delete/move); data: {"repo": ..., "actions": [...],
                           optional "message", "ref"/"branch" (default "main")}; each action is
                           {"action": "create"|"update"|"delete"|"move", "path": ..., "content": <raw text>
                           for create/update, "previous_path": ... for move}
      "files"           — alias for "commit"
      "file"            — create/update a file; data: {"repo": ..., "path": ..., "content": <raw text, encoded here>
                           or "content_base64": <pre-encoded>, "message": ..., "sha": <required for update>}
      "issue"           — create an issue; data: {"repo": ..., "title": ..., "body": ...,
                           "labels": [...], "assignees": [...], "milestone": ...}
      "issue_update"    — update an issue; data: {"repo": ..., "issue_number": ..., ...}
      "issue_comment"   — comment on an issue; data: {"repo": ..., "issue_number": ..., "body": ...}
      "issue_label"     — add labels to an issue; data: {"repo": ..., "issue_number": ..., "labels": [...]}
      "issue_reaction"  — react to an issue; data: {"repo": ..., "issue_number": ..., "content": ...}
      "label"           — create a label; data: {"repo": ..., "name": ..., "color": ..., "description": ...}
      "milestone"       — create a milestone; data: {"repo": ..., "title": ..., "description": ..., "due_on": ...}
      "pr"              — create a pull request; data: {"repo": ..., "title": ..., "head": ..., "base": ...,
                           "body": ..., "draft": ..., "maintainer_can_modify": ...}
      "pr_review"       — submit a PR review; data: {"repo": ..., "pull_number": ..., "event": "APPROVE"|
                           "REQUEST_CHANGES"|"COMMENT", "body": ..., "comments": [{"path": ..., "position": ...,
                           "body": ...}]}
      "pr_comment"      — review comment on a PR; data: {"repo": ..., "pull_number": ..., "body": ...}
      "pr_update"       — update a pull request; data: {"repo": ..., "pull_number": ..., "title": ...,
                           "body": ..., "state": ..., "base": ...}
      "pr_merge"        — merge a pull request; data: {"repo": ..., "pull_number": ..., optional
                           "commit_title", "commit_message", "merge_method", "sha"}
      "pr_review_request" — request reviewers on a PR; data: {"repo": ..., "pull_number": ...,
                           "reviewers": [...] and/or "team_reviewers": [...]}
      "pr_label"        — add labels to a pull request; data: {"repo": ..., "pull_number": ..., "labels": [...]}
      "issue_assign"    — assign an issue; data: {"repo": ..., "issue_number": ..., "assignees": [...]}
    """

    def __init__(
        self,
        token: str,
        base_url: str = _GITHUB_API,
        *,
        circuit_failure_threshold: int = _CIRCUIT_FAILURE_THRESHOLD,
        circuit_cooldown_seconds: float = _CIRCUIT_COOLDOWN_SECONDS,
    ) -> None:
        self._token = token
        self._base_url = base_url
        if circuit_failure_threshold < 1:
            raise ValueError("circuit_failure_threshold must be >= 1")
        if circuit_cooldown_seconds <= 0:
            raise ValueError("circuit_cooldown_seconds must be > 0")
        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_cooldown_seconds = circuit_cooldown_seconds
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_open_until = 0.0
        self._circuit_half_open = False

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITHUB

    def circuit_state(self) -> dict[str, Any]:
        """Expose the circuit breaker state for observability.

        Lets agents and the health UI see whether the connector is currently
        failing fast (``open``), how many consecutive service-level failures led
        there, and how long until a half-open probe is allowed.
        """
        remaining = max(0.0, self._circuit_open_until - time.monotonic()) if self._circuit_open else 0.0
        return {
            "open": self._circuit_open,
            "half_open": self._circuit_half_open,
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self._circuit_failure_threshold,
            "cooldown_seconds": self._circuit_cooldown_seconds,
            "remaining_cooldown": round(remaining, 2),
        }

    def _record_success(self) -> None:
        """Record a successful API call, closing the circuit if it was open."""
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_half_open = False
        self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        """Record a service-level failure, tripping the breaker at the threshold.

        Only service-level failures (server errors, exhausted rate limits,
        transport failures) count — client errors (4xx) never open the circuit
        from the closed state. A half-open recovery probe is the exception: any
        terminal error on the probe (including 4xx) is recorded so the breaker
        re-trips with a fresh cooldown and the half-open flag is always cleared
        — a probe can never wedge the circuit half-open forever.
        """
        self._consecutive_failures += 1
        self._circuit_half_open = False
        if self._consecutive_failures >= self._circuit_failure_threshold:
            self._circuit_open = True
            self._circuit_open_until = time.monotonic() + self._circuit_cooldown_seconds

    def _check_circuit(self) -> None:
        """Fail fast while the circuit is open; allow a half-open probe on cooldown expiry.

        While open, every API call raises ``GitHubCircuitOpenError`` without
        contacting the network. When the cooldown window has elapsed, exactly
        one half-open probe is admitted: a probe success closes the circuit,
        a probe failure re-opens it for another cooldown period.
        """
        if not self._circuit_open:
            return
        if time.monotonic() < self._circuit_open_until:
            remaining = self._circuit_open_until - time.monotonic()
            raise GitHubCircuitOpenError(
                f"GitHub circuit is open after {self._consecutive_failures} consecutive failures; "
                f"retry after circuit cooldown ({remaining:.1f}s remaining)",
                retry_after_seconds=remaining,
            )
        if self._circuit_half_open:
            raise GitHubCircuitOpenError(
                "GitHub circuit is half-open — a recovery probe is already in flight",
                retry_after_seconds=0.0,
            )
        self._circuit_half_open = True

    @staticmethod
    def _ref_to_git_ref(ref: str) -> str:
        """Convert a short branch name to a Git Database ref path.

        ``main`` -> ``refs/heads/main``; already-qualified refs pass through.
        """
        if ref.startswith("refs/"):
            return ref
        return f"refs/heads/{ref}"

    async def _resolve_commit_sha(self, owner_repo: str, ref: str, resource: str) -> str:
        """Resolve a ref (branch/tag/SHA) to a commit SHA via the commits API."""
        commit_r = await self._call_api("GET", f"/repos/{owner_repo}/commits/{ref}")
        commit_body = await self._parse_json_object(commit_r)
        commit_sha = commit_body.get("sha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise ValueError(f"GitHub {resource} could not resolve ref {ref!r} to a commit SHA")
        return commit_sha

    async def _read_file_text(self, owner_repo: str, path: str, ref: str) -> str:
        """Read a file's text content from the Contents API (used by move actions).

        Mirrors ``query("file")`` decoding: base64 content is decoded to UTF-8
        text so a ``move`` can carry the file's content into the new blob. A
        file that is not decodable as UTF-8 text is rejected with a descriptive
        ``ValueError`` instead of surfacing a raw decode error.
        """
        r = await self._call_api("GET", f"/repos/{owner_repo}/contents/{path}", params={"ref": ref})
        info = await self._parse_json_object(r)
        content = info.get("content")
        if not isinstance(content, str):
            raise ValueError(f"GitHub commit write: could not read content of {path!r}")
        if info.get("encoding") == "base64":
            try:
                return base64.b64decode(content).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                raise ValueError(
                    f"GitHub commit write: file {path!r} is not decodable UTF-8 text; cannot move a binary file",
                ) from None
        return content

    async def _create_blob(self, owner_repo: str, content: str) -> str:
        """Create a Git blob and return its SHA."""
        r = await self._call_api(
            "POST",
            f"/repos/{owner_repo}/git/blobs",
            json={"content": content, "encoding": "utf-8"},
        )
        blob = await self._parse_json_object(r)
        blob_sha = blob.get("sha")
        if not isinstance(blob_sha, str) or not blob_sha:
            raise ValueError("GitHub commit write: blob creation did not return a sha")
        return blob_sha

    async def _create_tree(self, owner_repo: str, base_sha: str, entries: list[dict[str, Any]]) -> str:
        """Create a Git tree on top of a base commit and return its SHA."""
        r = await self._call_api(
            "POST",
            f"/repos/{owner_repo}/git/trees",
            json={"base_tree": base_sha, "tree": entries},
        )
        tree = await self._parse_json_object(r)
        tree_sha = tree.get("sha")
        if not isinstance(tree_sha, str) or not tree_sha:
            raise ValueError("GitHub commit write: tree creation did not return a sha")
        return tree_sha

    async def _create_commit(self, owner_repo: str, message: str, tree_sha: str, parent_sha: str) -> str:
        """Create a Git commit with a single parent and return its SHA."""
        r = await self._call_api(
            "POST",
            f"/repos/{owner_repo}/git/commits",
            json={"message": message, "tree": tree_sha, "parents": [parent_sha]},
        )
        commit = await self._parse_json_object(r)
        commit_sha = commit.get("sha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise ValueError("GitHub commit write: commit creation did not return a sha")
        return commit_sha

    async def _update_ref(self, owner_repo: str, ref: str, commit_sha: str) -> httpx.Response:
        """Fast-forward a ref to a new commit via the Git refs API."""
        git_ref = self._ref_to_git_ref(ref)
        return await self._call_api(
            "PATCH",
            f"/repos/{owner_repo}/git/refs/{git_ref}",
            json={"sha": commit_sha, "force": False},
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "Accept": "application/vnd.github+json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers(), timeout=30)

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

        ``Retry-After`` is honoured on any retryable status. GitHub reports the
        ``X-RateLimit-Reset`` header on *every* response while rate limiting is
        active, so it only counts as a server delay on HTTP 429 (the quota
        window); on other retryable statuses it would otherwise switch the
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

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Compute the delay before the next retry attempt.

        On HTTP 429 prefers GitHub's ``X-RateLimit-Reset`` (epoch seconds — the
        quota reset window), then ``Retry-After``, then exponential backoff.

        The quota reset window is left uncapped so a quota window longer than
        ``_MAX_DELAY`` is truly honoured (capping it would fire the retry early
        and hit another 429). ``Retry-After`` and backoff remain capped at
        ``_MAX_DELAY``.
        """
        if response.status_code == 429:
            reset_delay = _parse_rate_limit_reset(response)
            if reset_delay is not None:
                return reset_delay
        retry_after = _parse_retry_after(response)
        if retry_after is not None:
            return min(retry_after, _MAX_DELAY)
        return backoff_delay(attempt)

    async def _call_api(
        self,
        method: str,
        path: str,
        *,
        _bypass_circuit: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Call GitHub API with retry/backoff for retryable statuses.

        Retries on 429, 502, 503, 504 with exponential backoff + jitter.
        On 429 responses, prefers ``X-RateLimit-Reset`` (the quota window) then
        ``Retry-After`` to compute the wait instead of blind backoff.
        Wraps HTTP/network/parse errors as ValueError.

        A circuit breaker fails fast while the upstream is in a sustained
        failure state (``_bypass_circuit`` skips the gate, used by health
        checks so the diagnostic path can always probe recovery).
        """
        if not _bypass_circuit:
            self._check_circuit()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    r = await client.request(method, path, **kwargs)
                    if r.status_code == 304:
                        self._raise_not_modified()
                    if self._should_retry_status(r.status_code, attempt):
                        await asyncio.sleep(self._sleep_delay(r, attempt))
                        continue
                    r.raise_for_status()
                    self._record_success()
                    return r
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if self._should_retry_status(exc.response.status_code, attempt):
                    await asyncio.sleep(self._sleep_delay(exc.response, attempt))
                    continue
                self._raise_status_error(exc)
            except httpx.TimeoutException as exc:
                last_exc = exc
                if await self._sleep_network_retry(attempt):
                    continue
                self._raise_network_error(exc, "GitHub API timeout", "network_timeout")
            except httpx.ConnectError as exc:
                last_exc = exc
                if await self._sleep_network_retry(attempt):
                    continue
                self._raise_network_error(exc, "GitHub API connection error", "network_connection")
        self._record_failure()
        raise GitHubNetworkError("GitHub API request failed after retries") from last_exc

    def _should_trip_circuit(self, status_code: int) -> bool:
        """Whether a terminal HTTP status should be recorded as a circuit failure.

        Service-level failures (retryable statuses and 5xx) count toward the
        breaker. A 4xx never counts from the closed state, but a client error
        on a half-open recovery probe means the probe did not confirm recovery
        — re-trip so a fresh probe is admitted after the next cooldown.
        """
        return status_code in _RETRYABLE_STATUSES or status_code >= 500 or self._circuit_half_open

    def _raise_status_error(self, exc: httpx.HTTPStatusError) -> NoReturn:
        """Raise a structured GitHub error for a terminal (non-retryable) HTTP status.

        Also re-trips the circuit breaker for service-level failures. A 4xx
        never counts toward the breaker from the closed state, but a client
        error on a half-open recovery probe means the probe did not confirm
        recovery — re-trip so a fresh probe is admitted after the next
        cooldown instead of wedging half-open.
        """
        status_code = exc.response.status_code
        if self._should_trip_circuit(status_code):
            self._record_failure()
        detail = f"GitHub API HTTP {status_code}: {exc.response.text[:200]}"
        if status_code == 429:
            quota = _rate_limit_detail(exc.response)
            if quota:
                detail = f"{detail} (quota: {quota})"
        raise _error_for_status(status_code, detail) from exc

    @staticmethod
    def _should_retry_status(status_code: int, attempt: int) -> bool:
        """True when a retryable status still has retry budget remaining."""
        return should_retry_status(status_code, attempt)

    async def _sleep_network_retry(self, attempt: int) -> bool:
        """Sleep before a transport-level retry; True when a retry should run."""
        if attempt < _MAX_RETRIES:
            await asyncio.sleep(self._jitter(self._backoff_delay(attempt)))
            return True
        return False

    def _raise_network_error(self, exc: Exception, message: str, error_code: str) -> NoReturn:
        """Record a transport failure and raise a typed network error."""
        self._record_failure()
        raise GitHubNetworkError(message, error_code=error_code) from exc

    def _raise_not_modified(self) -> NoReturn:
        """Record a healthy 304 response and surface it as a typed API error.

        304 is a healthy service response (the resource is unchanged) — record it
        as a success so a half-open probe closes the circuit instead of wedging it.
        """
        self._record_success()
        raise GitHubAPIError(
            "GitHub API returned 304 Not Modified — resource unchanged",
            error_code="not_modified",
            status_code=304,
        )

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Compute the exponential backoff delay for a retry attempt (capped)."""
        return backoff_delay(attempt)

    async def _parse_json(self, response: httpx.Response) -> Any:
        """Parse JSON response, wrapping decode errors as a typed API error."""
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise GitHubAPIError(
                f"GitHub API returned invalid JSON: {response.text[:200]}",
                error_code="invalid_response",
            ) from exc

    async def _parse_json_object(self, response: httpx.Response) -> dict[str, Any]:
        return cast("dict[str, Any]", await self._parse_json(response))

    @staticmethod
    def _parse_scopes_from_headers(response: httpx.Response) -> set[str]:
        header_value = response.headers.get("X-OAuth-Scopes", "")
        if header_value.strip():
            return {s.strip() for s in header_value.split(",")}
        return set()

    @staticmethod
    def _parse_accepted_permissions(response: httpx.Response) -> set[str]:
        """Parse GitHub's ``X-Accepted-GitHub-Permissions`` header.

        GitHub attaches this header to responses whose endpoint exercised a
        permission, listing the fine-grained permissions (e.g. ``contents:read``,
        ``pull_requests:write``) the token holds for the accessed resource. It is
        the only permission signal GitHub exposes for fine-grained PATs.
        """
        header_value = response.headers.get("X-Accepted-GitHub-Permissions", "")
        if header_value.strip():
            return {s.strip() for s in header_value.split(",")}
        return set()

    def _fine_grained_missing_permissions(self, response: httpx.Response) -> set[str]:
        """Return the missing PRD §7.11 fine-grained permissions for this token.

        GitHub does not return ``X-OAuth-Scopes`` for fine-grained PATs and
        exposes no endpoint that enumerates a token's permissions, so the only
        signal is the ``X-Accepted-GitHub-Permissions`` header GitHub attaches to
        responses whose endpoint required a permission. When the header is absent
        (typical for the ``/user`` probe, which needs no repository permission)
        the missing set is empty — the GitHub API remains the enforcement point
        and a denied request already surfaces as a typed ``insufficient_scope``
        error.
        """
        accepted = self._parse_accepted_permissions(response)
        if not accepted:
            return set()
        return set(REQUIRED_FINE_GRAINED_PERMISSIONS - accepted)

    async def verify_scopes(self) -> set[str]:
        """Verify the token's scopes/permissions.

        Returns the set of missing required scopes (empty if all present).

        Classic PATs are verified against ``REQUIRED_SCOPES`` via GitHub's
        ``X-OAuth-Scopes`` header. Fine-grained PATs (``github_pat_`` prefix)
        never receive that header and are verified against the PRD §7.11
        fine-grained permissions via ``X-Accepted-GitHub-Permissions`` when
        GitHub reports them.

        Raises ``GitHubAuthError`` when the token itself is invalid/expired
        (401) or lacks the required permission (403).
        """
        r = await self._call_api("GET", "/user")

        if is_fine_grained_pat(self._token):
            return self._fine_grained_missing_permissions(r)

        token_scopes = self._parse_scopes_from_headers(r)
        # admin:org is a superset of read:org
        if "admin:org" in token_scopes:
            token_scopes.add("read:org")
        return set(REQUIRED_SCOPES - token_scopes)

    async def health_check(self) -> HealthResult:
        """Check API access and verify required scopes/permissions.

        Classic PATs are verified against the required scopes (``repo``,
        ``read:org``) via the ``X-OAuth-Scopes`` header; fine-grained PATs
        (``github_pat_`` prefix) are verified against the PRD §7.11 permissions
        (``contents:read``, ``contents:write``, ``pull_requests:write``) via
        ``X-Accepted-GitHub-Permissions`` when GitHub reports them. Distinguishes
        expired/invalid tokens (HTTP 401), missing scopes (HTTP 403),
        rate-limit exhaustion (HTTP 429), and transport failures so the failure
        mode is actionable.

        Health checks bypass the circuit breaker so the diagnostic path always
        probes the API — a healthy probe closes an open circuit, a failing
        probe re-opens it.
        """
        try:
            r = await self._call_api("GET", "/user", _bypass_circuit=True)
        except GitHubAuthError as exc:
            if exc.status_code == 401:
                return HealthResult(ok=False, detail="Invalid or expired GitHub token (HTTP 401)")
            return HealthResult(ok=False, detail="Missing scopes: token lacks required permission (HTTP 403)")
        except GitHubRateLimitError as exc:
            return HealthResult(ok=False, detail=f"GitHub rate limit exhausted: {exc}")
        except GitHubNetworkError as exc:
            return HealthResult(ok=False, detail=f"GitHub network error: {exc}")
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

        try:
            user_login = (await self._parse_json(r)).get("login", "")
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

        token_scopes = self._parse_scopes_from_headers(r)
        if is_fine_grained_pat(self._token):
            missing = self._fine_grained_missing_permissions(r)
            if missing:
                return self._missing_scopes_result(missing, REQUIRED_FINE_GRAINED_PERMISSIONS)
            return HealthResult(ok=True, detail=user_login)

        missing_classic = REQUIRED_SCOPES - token_scopes
        if missing_classic:
            return self._missing_scopes_result(missing_classic, REQUIRED_SCOPES)

        return HealthResult(ok=True, detail=user_login)

    @staticmethod
    def _missing_scopes_result(missing: set[str] | frozenset[str], required: frozenset[str]) -> HealthResult:
        """Build a failing HealthResult describing missing scopes/permissions."""
        missing_codes = ", ".join(f"missing_scope:{scope}" for scope in sorted(missing))
        return HealthResult(
            ok=False,
            detail=(
                f"Missing scopes: {missing_codes} ({', '.join(sorted(missing))}). "
                f"Required: {', '.join(sorted(required))}"
            ),
        )

    async def compensate(
        self,
        operation: CompensationOperation,
        *,
        context: CompensationContext,
        error: str,
    ) -> CompensationResult:
        """Compensate a performed GitHub write (FAR-213): close a PR the run opened.

        Supports the ``pr`` write resource (a pull request the run created is
        closed). Any other resource has no invertible inverse and returns
        ``not_supported``. Best-effort: an API failure returns ``failed`` with a
        summary detail and never raises into the terminalization path.
        """
        if operation.resource != "pr":
            return CompensationResult(
                outcome=CompensationOutcome.NOT_SUPPORTED,
                detail=f"no inverse for GitHub write resource {operation.resource!r}",
            )
        repo = operation.data.get("repo")
        pr_number = operation.output.get("number")
        if not isinstance(repo, str) or not repo:
            return CompensationResult(
                outcome=CompensationOutcome.NOT_SUPPORTED,
                detail="cannot compensate a GitHub pr write without a 'repo' in the write payload",
            )
        if pr_number is None:
            return CompensationResult(
                outcome=CompensationOutcome.NOT_SUPPORTED,
                detail="cannot compensate a GitHub pr write without a 'number' in the created PR output",
            )
        try:
            r = await self._call_api(
                "PATCH",
                f"/repos/{repo}/pulls/{pr_number}",
                json={"state": "closed"},
            )
            await self._parse_json_object(r)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return CompensationResult(
                outcome=CompensationOutcome.FAILED,
                detail=f"close PR failed: {type(exc).__name__}",
            )
        return CompensationResult(
            outcome=CompensationOutcome.COMPENSATED,
            detail=f"closed PR #{pr_number} on {repo}",
            resource_id=str(pr_number),
        )

    def _require_filter(self, filters: dict[str, Any], key: str, resource: str) -> str:
        """Get a required filter or raise a descriptive ValueError."""
        if key not in filters:
            raise ValueError(f"GitHub {resource} query requires '{key}' filter")
        value = filters[key]
        if not isinstance(value, str):
            raise ValueError(f"GitHub {resource} query filter '{key}' must be a string")
        return value

    def _result(
        self,
        records: list[dict[str, Any]],
        response: httpx.Response,
        *,
        total: int | None = None,
        next_cursor: str | None = None,
    ) -> ConnectorResult:
        """Build a ConnectorResult, wiring GitHub rate-limit budget metadata."""
        return ConnectorResult(
            records=records,
            total=total,
            next_cursor=next_cursor,
            metadata={"rate_limit": _rate_limit_metadata(response)},
        )

    def _paginated_result(
        self,
        records: list[dict[str, Any]],
        response: httpx.Response,
    ) -> ConnectorResult:
        """Build a paginated list result with total and the Link-header cursor."""
        links = _parse_link_header(response)
        return self._result(records, response, total=len(records), next_cursor=links.get("next"))

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        handlers: dict[str, Callable[[ConnectorQuery], Awaitable[ConnectorResult]]] = {
            "repos": self._query_repos,
            "file": self._query_file,
            "tree": self._query_tree,
            "pulls": self._query_pulls,
            "pr_commits": self._query_pr_commits,
            "pr_files": self._query_pr_files,
            "issues": self._query_issues,
            "issue": self._query_issue,
            "labels": self._query_labels,
            "milestones": self._query_milestones,
            "issue_comments": self._query_issue_comments,
            "issue_events": self._query_issue_events,
            "assignees": self._query_assignees,
            "timeline": self._query_timeline,
            "pr_diff": self._query_pr_diff,
            "search_issues": self._query_search_issues,
            "rate_limit": self._query_rate_limit,
        }
        handler = handlers.get(q.resource)
        if handler is None:
            raise ValueError(f"Unsupported GitHub resource: {q.resource!r}")
        return await handler(q)

    async def _query_repos(self, q: ConnectorQuery) -> ConnectorResult:
        r = await self._call_api("GET", "/user/repos", params={"per_page": q.limit})
        data: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(data, r)

    async def _query_file(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "file")
        path = _validate_path(self._require_filter(q.filters, "path", "file"), "file")
        ref = q.filters.get("ref", "main")
        r = await self._call_api("GET", f"/repos/{owner_repo}/contents/{path}", params={"ref": ref})
        info = await self._parse_json_object(r)
        _decode_read_content(info)
        return self._result([info], r)

    async def _query_tree(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "tree")
        path_filter = _validate_path(q.filters["path"], "tree") if "path" in q.filters else None
        ref = q.filters.get("ref", "main")
        tree_sha = await self._resolve_commit_sha(owner_repo, ref, "tree")
        tree_params: dict[str, Any] = {}
        if q.filters.get("recursive", True):
            tree_params["recursive"] = "1"
        tree_r = await self._call_api(
            "GET",
            f"/repos/{owner_repo}/git/trees/{tree_sha}",
            params=tree_params,
        )
        body = await self._parse_json_object(tree_r)
        entries: list[dict[str, Any]] = cast("list[dict[str, Any]]", body.get("tree", []))
        if path_filter is not None:
            path_prefix = path_filter.rstrip("/") + "/"
            entries = [e for e in entries if e.get("path", "").startswith(path_prefix)]
        return self._result(entries, tree_r, total=len(entries))

    async def _query_pulls(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "pulls")
        state = q.filters.get("state", "open")
        params: dict[str, Any] = {"state": state, "per_page": q.limit}
        if "sort" in q.filters:
            params["sort"] = q.filters["sort"]
        if "direction" in q.filters:
            params["direction"] = q.filters["direction"]
        r = await self._call_api("GET", f"/repos/{owner_repo}/pulls", params=params)
        prs: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(prs, r)

    async def _query_pr_commits(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "pr_commits")
        pull_number = self._require_filter(q.filters, "pull_number", "pr_commits")
        r = await self._call_api(
            "GET",
            f"/repos/{owner_repo}/pulls/{pull_number}/commits",
            params={"per_page": q.limit},
        )
        commits: list[dict[str, Any]] = await self._parse_json(r)
        return self._result(commits, r, total=len(commits))

    async def _query_pr_files(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "pr_files")
        pull_number = self._require_filter(q.filters, "pull_number", "pr_files")
        r = await self._call_api(
            "GET",
            f"/repos/{owner_repo}/pulls/{pull_number}/files",
            params={"per_page": q.limit},
        )
        files: list[dict[str, Any]] = await self._parse_json(r)
        return self._result(files, r, total=len(files))

    async def _query_issues(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "issues")
        params = {"per_page": q.limit}
        for key in ("state", "labels", "sort", "direction", "milestone", "assignee", "since"):
            if key in q.filters:
                params[key] = q.filters[key]
        r = await self._call_api("GET", f"/repos/{owner_repo}/issues", params=params)
        issues: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(issues, r)

    async def _query_issue(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "issue")
        issue_number = self._require_filter(q.filters, "issue_number", "issue")
        r = await self._call_api("GET", f"/repos/{owner_repo}/issues/{issue_number}")
        return self._result([await self._parse_json(r)], r)

    async def _query_labels(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "labels")
        r = await self._call_api("GET", f"/repos/{owner_repo}/labels", params={"per_page": q.limit})
        labels: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(labels, r)

    async def _query_milestones(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "milestones")
        params = {"per_page": q.limit}
        if "state" in q.filters:
            params["state"] = q.filters["state"]
        if "sort" in q.filters:
            params["sort"] = q.filters["sort"]
        if "direction" in q.filters:
            params["direction"] = q.filters["direction"]
        r = await self._call_api("GET", f"/repos/{owner_repo}/milestones", params=params)
        milestones: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(milestones, r)

    async def _query_issue_comments(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "issue_comments")
        issue_number = self._require_filter(q.filters, "issue_number", "issue_comments")
        r = await self._call_api(
            "GET",
            f"/repos/{owner_repo}/issues/{issue_number}/comments",
            params={"per_page": q.limit},
        )
        comments: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(comments, r)

    async def _query_issue_events(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "issue_events")
        issue_number = self._require_filter(q.filters, "issue_number", "issue_events")
        r = await self._call_api(
            "GET",
            f"/repos/{owner_repo}/issues/{issue_number}/events",
            params={"per_page": q.limit},
        )
        events: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(events, r)

    async def _query_assignees(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "assignees")
        r = await self._call_api("GET", f"/repos/{owner_repo}/assignees", params={"per_page": q.limit})
        assignees: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(assignees, r)

    async def _query_timeline(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "timeline")
        issue_number = self._require_filter(q.filters, "issue_number", "timeline")
        r = await self._call_api(
            "GET",
            f"/repos/{owner_repo}/issues/{issue_number}/timeline",
            params={"per_page": q.limit},
        )
        timeline: list[dict[str, Any]] = await self._parse_json(r)
        return self._paginated_result(timeline, r)

    async def _query_pr_diff(self, q: ConnectorQuery) -> ConnectorResult:
        owner_repo = self._require_filter(q.filters, "repo", "pr_diff")
        pull_number = self._require_filter(q.filters, "pull_number", "pr_diff")
        r = await self._call_api(
            "GET",
            f"/repos/{owner_repo}/pulls/{pull_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return self._result([{"diff": r.text}], r, total=1)

    async def _query_search_issues(self, q: ConnectorQuery) -> ConnectorResult:
        search_query = self._require_filter(q.filters, "q", "search_issues")
        params = {"q": search_query, "per_page": q.limit}
        for key in ("sort", "order", "state", "labels", "assignee", "created", "updated"):
            if key in q.filters:
                params[key] = q.filters[key]
        r = await self._call_api("GET", "/search/issues", params=params)
        body = await self._parse_json_object(r)
        items = cast("list[dict[str, Any]]", body.get("items", []))
        links = _parse_link_header(r)
        return self._result(items, r, total=_search_total(body), next_cursor=links.get("next"))

    async def _query_rate_limit(self, q: ConnectorQuery) -> ConnectorResult:
        r = await self._call_api("GET", "/rate_limit")
        body = await self._parse_json_object(r)
        resources = cast("dict[str, Any]", body.get("resources", {}))
        return self._result([resources], r, total=1)

    def _require_write_filter(self, data: dict[str, Any], key: str, resource: str) -> str:
        """Get a required write field or raise a descriptive ValueError."""
        if key not in data:
            raise ValueError(f"GitHub {resource} write requires '{key}' in data")
        value = data[key]
        if not isinstance(value, str):
            raise ValueError(f"GitHub {resource} write field '{key}' must be a string")
        return value

    def _require_string_list(self, data: dict[str, Any], key: str, resource: str) -> list[str]:
        """Get a required non-empty list of strings or raise a descriptive ValueError."""
        if key not in data:
            raise ValueError(f"GitHub {resource} write requires '{key}' in data")
        value = data[key]
        if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
            raise ValueError(f"GitHub {resource} write field '{key}' must be a non-empty list of strings")
        return value

    def _write_body(self, data: dict[str, Any], required: dict[str, str], optional: tuple[str, ...]) -> dict[str, Any]:
        """Build a write body from required fields (validated) plus present optional fields."""
        body: dict[str, Any] = {}
        for key, resource in required.items():
            body[key] = self._require_write_filter(data, key, resource)
        for key in optional:
            if key in data:
                body[key] = data[key]
        return body

    async def _post_subresource(
        self,
        owner_repo: str,
        entity: str,
        item_number: str,
        subpath: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST a write to a GitHub item sub-resource and parse the JSON response."""
        r = await self._call_api(
            "POST",
            f"/repos/{owner_repo}/{entity}/{item_number}/{subpath}",
            json=body,
            headers=headers,
        )
        return await self._parse_json_object(r)

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        handlers: dict[str, Callable[[ConnectorPayload], Awaitable[dict[str, Any]]]] = {
            "commit": self._write_commit,
            "files": self._write_commit,
            "file": self._write_file,
            "issue": self._write_issue,
            "issue_update": self._write_issue_update,
            "issue_comment": self._write_issue_comment,
            "issue_label": self._write_issue_label,
            "issue_reaction": self._write_issue_reaction,
            "label": self._write_label,
            "milestone": self._write_milestone,
            "pr": self._write_pr,
            "pr_review": self._write_pr_review,
            "pr_comment": self._write_pr_comment,
            "pr_update": self._write_pr_update,
            "pr_merge": self._write_pr_merge,
            "pr_review_request": self._write_pr_review_request,
            "pr_label": self._write_pr_label,
            "issue_assign": self._write_issue_assign,
        }
        handler = handlers.get(payload.resource)
        if handler is None:
            raise ValueError(f"Unsupported GitHub write resource: {payload.resource!r}")
        return await handler(payload)

    def _require_commit_actions(self, data: dict[str, Any], resource: str) -> list[Any]:
        """Get the required non-empty 'actions' list for a batch commit."""
        actions = data.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"GitHub resource {resource!r} requires a non-empty 'actions' list")
        return actions

    def _require_commit_ref(self, data: dict[str, Any], resource: str) -> str:
        """Get the target ref/branch for a batch commit (default 'main')."""
        ref = data.get("ref", data.get("branch", "main"))
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"GitHub resource {resource!r} requires a non-empty 'ref' or 'branch'")
        return ref

    def _require_commit_message(self, data: dict[str, Any], resource: str) -> str:
        """Get the commit message for a batch commit (default 'Update via Modulo')."""
        message = data.get("message", "Update via Modulo")
        if not isinstance(message, str) or not message:
            raise ValueError(f"GitHub resource {resource!r} requires a non-empty 'message'")
        return message

    def _validate_commit_actions(self, actions: list[Any], resource: str) -> set[str]:
        """Validate batch-commit actions, returning the set of targeted paths.

        Each action must be an object with a known action type, a relative
        ``path`` (``previous_path`` for moves), and string ``content`` for
        create/update actions. No path may be targeted more than once.
        """
        targeted_paths: set[str] = set()
        for action in actions:
            self._validate_commit_action(action, resource)
            action_type = action["action"]
            path = action["path"]
            targets: tuple[str, ...] = (action["previous_path"], path) if action_type == "move" else (path,)
            for targeted in targets:
                if targeted in targeted_paths:
                    raise ValueError(
                        f"GitHub resource {resource!r}: path {targeted!r} is targeted more than once by the batch",
                    )
                targeted_paths.add(targeted)
        return targeted_paths

    def _validate_commit_action(self, action: Any, resource: str) -> None:
        """Validate a single batch-commit action in place."""
        if not isinstance(action, dict):
            raise ValueError(f"GitHub resource {resource!r}: each action must be an object")
        action_type = action.get("action")
        if action_type not in _COMMIT_ACTIONS:
            raise ValueError(
                f"GitHub resource {resource!r}: action {action_type!r} must be one of {sorted(_COMMIT_ACTIONS)}",
            )
        path = action.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"GitHub resource {resource!r}: each action requires 'path'")
        _validate_path(path, resource)
        if action_type == "move":
            previous_path = action.get("previous_path")
            if not isinstance(previous_path, str) or not previous_path:
                raise ValueError(f"GitHub resource {resource!r}: move action requires 'previous_path'")
            _validate_path(previous_path, resource)
        if action_type not in {"move", "delete"} and not isinstance(action.get("content"), str):
            raise ValueError(
                f"GitHub resource {resource!r}: action {action_type!r} requires string 'content'",
            )

    async def _write_commit(self, payload: ConnectorPayload) -> dict[str, Any]:
        resource = payload.resource
        owner_repo = self._require_write_filter(payload.data, "repo", resource)
        actions = self._require_commit_actions(payload.data, resource)
        ref = self._require_commit_ref(payload.data, resource)
        message = self._require_commit_message(payload.data, resource)
        self._validate_commit_actions(actions, resource)
        base_sha = await self._resolve_commit_sha(owner_repo, ref, resource)
        tree_entries = await self._build_tree_entries(actions, owner_repo, ref)
        tree_sha = await self._create_tree(owner_repo, base_sha, tree_entries)
        commit_sha = await self._create_commit(owner_repo, message, tree_sha, base_sha)
        ref_response = await self._update_ref(owner_repo, ref, commit_sha)
        return await self._parse_json_object(ref_response)

    async def _build_tree_entries(
        self,
        actions: list[Any],
        owner_repo: str,
        ref: str,
    ) -> list[dict[str, Any]]:
        """Turn batch actions into Git tree entries, creating blobs as needed."""
        tree_entries: list[dict[str, Any]] = []
        for action in actions:
            tree_entries.extend(await self._build_tree_entry(action, owner_repo, ref))
        return tree_entries

    async def _build_tree_entry(self, action: dict[str, Any], owner_repo: str, ref: str) -> list[dict[str, Any]]:
        """Build the Git tree entries for a single commit action.

        ``delete`` removes the old path; ``move`` deletes the old path and adds
        the content read from it under the new path; create/update add a blob
        for the path with the supplied content.
        """
        action_type = action["action"]
        path = action["path"]
        if action_type == "delete":
            return [self._tree_blob_entry(path, None)]
        if action_type == "move":
            previous_path = action["previous_path"]
            content = await self._read_file_text(owner_repo, previous_path, ref)
            return [
                self._tree_blob_entry(previous_path, None),
                self._tree_blob_entry(path, await self._create_blob(owner_repo, content)),
            ]
        blob_sha = await self._create_blob(owner_repo, action["content"])
        return [self._tree_blob_entry(path, blob_sha)]

    @staticmethod
    def _tree_blob_entry(path: str, sha: str | None) -> dict[str, Any]:
        """Build a single Git tree blob entry dict."""
        return {"path": path, "mode": "100644", "type": "blob", "sha": sha}

    async def _write_file(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "file")
        path = _validate_path(self._require_write_filter(payload.data, "path", "file"), "file")
        body: dict[str, Any] = {
            "message": payload.data.get("message", "Update via Modulo"),
            "content": _encode_write_content(payload.data, "file"),
        }
        if "sha" in payload.data:
            body["sha"] = payload.data["sha"]
        r = await self._call_api("PUT", f"/repos/{owner_repo}/contents/{path}", json=body)
        return await self._parse_json_object(r)

    async def _write_issue(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "issue")
        issue_body = self._write_body(
            payload.data,
            {"title": "issue"},
            ("body", "labels", "assignees", "milestone"),
        )
        r = await self._call_api("POST", f"/repos/{owner_repo}/issues", json=issue_body)
        return await self._parse_json_object(r)

    async def _write_issue_update(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "issue_update")
        issue_number = self._require_write_filter(payload.data, "issue_number", "issue_update")
        update_body: dict[str, Any] = {}
        for key in ("state", "title", "body", "labels", "milestone"):
            if key in payload.data:
                update_body[key] = payload.data[key]
        r = await self._call_api("PATCH", f"/repos/{owner_repo}/issues/{issue_number}", json=update_body)
        return await self._parse_json_object(r)

    async def _write_issue_comment(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "issue_comment")
        issue_number = self._require_write_filter(payload.data, "issue_number", "issue_comment")
        return await self._post_subresource(
            owner_repo,
            "issues",
            issue_number,
            "comments",
            {"body": self._require_write_filter(payload.data, "body", "issue_comment")},
        )

    async def _write_issue_label(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "issue_label")
        issue_number = self._require_write_filter(payload.data, "issue_number", "issue_label")
        return await self._post_subresource(
            owner_repo,
            "issues",
            issue_number,
            "labels",
            {"labels": self._require_string_list(payload.data, "labels", "issue_label")},
        )

    async def _write_issue_reaction(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "issue_reaction")
        issue_number = self._require_write_filter(payload.data, "issue_number", "issue_reaction")
        return await self._post_subresource(
            owner_repo,
            "issues",
            issue_number,
            "reactions",
            {"content": self._require_write_filter(payload.data, "content", "issue_reaction")},
            headers={"Accept": "application/vnd.github.squirrel-girl-preview+json"},
        )

    async def _write_label(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "label")
        label_body = self._write_body(payload.data, {"name": "label", "color": "label"}, ("description",))
        r = await self._call_api("POST", f"/repos/{owner_repo}/labels", json=label_body)
        return await self._parse_json_object(r)

    async def _write_milestone(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "milestone")
        milestone_body = self._write_body(payload.data, {"title": "milestone"}, ("description", "due_on"))
        r = await self._call_api("POST", f"/repos/{owner_repo}/milestones", json=milestone_body)
        return await self._parse_json_object(r)

    async def _write_pr(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "pr")
        pr_body = self._write_body(
            payload.data,
            {"title": "pr", "head": "pr", "base": "pr"},
            ("body", "draft", "maintainer_can_modify"),
        )
        r = await self._call_api("POST", f"/repos/{owner_repo}/pulls", json=pr_body)
        return await self._parse_json_object(r)

    async def _write_pr_review(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "pr_review")
        pull_number = self._require_write_filter(payload.data, "pull_number", "pr_review")
        event = self._require_write_filter(payload.data, "event", "pr_review")
        if event not in _REVIEW_EVENTS:
            raise ValueError(
                f"GitHub pr_review 'event' must be one of APPROVE, REQUEST_CHANGES, COMMENT; got {event!r}"
            )
        review_body = self._write_body(payload.data, {}, ("comments",))
        review_body["event"] = event
        review_body["body"] = payload.data.get("body", "")
        return await self._post_subresource(
            owner_repo,
            "pulls",
            pull_number,
            "reviews",
            review_body,
        )

    async def _write_pr_comment(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "pr_comment")
        pull_number = self._require_write_filter(payload.data, "pull_number", "pr_comment")
        body_value = self._require_write_filter(payload.data, "body", "pr_comment")
        return await self._post_subresource(
            owner_repo,
            "pulls",
            pull_number,
            "comments",
            {"body": body_value},
        )

    async def _write_pr_update(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "pr_update")
        pull_number = self._require_write_filter(payload.data, "pull_number", "pr_update")
        update: dict[str, Any] = {}
        for key in ("title", "body", "state", "base"):
            if key in payload.data:
                update[key] = payload.data[key]
        r = await self._call_api("PATCH", f"/repos/{owner_repo}/pulls/{pull_number}", json=update)
        return await self._parse_json_object(r)

    async def _write_pr_merge(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "pr_merge")
        pull_number = self._require_write_filter(payload.data, "pull_number", "pr_merge")
        merge_body: dict[str, Any] = {}
        for key in ("commit_title", "commit_message", "merge_method", "sha"):
            if key in payload.data:
                merge_body[key] = payload.data[key]
        r = await self._call_api("PUT", f"/repos/{owner_repo}/pulls/{pull_number}/merge", json=merge_body)
        return await self._parse_json_object(r)

    async def _write_pr_review_request(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "pr_review_request")
        pull_number = self._require_write_filter(payload.data, "pull_number", "pr_review_request")
        request_body = self._optional_reviewer_lists(payload.data)
        return await self._post_subresource(
            owner_repo,
            "pulls",
            pull_number,
            "requested_reviewers",
            request_body,
        )

    async def _write_pr_label(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "pr_label")
        pull_number = self._require_write_filter(payload.data, "pull_number", "pr_label")
        return await self._post_subresource(
            owner_repo,
            "issues",
            pull_number,
            "labels",
            {"labels": self._require_string_list(payload.data, "labels", "pr_label")},
        )

    async def _write_issue_assign(self, payload: ConnectorPayload) -> dict[str, Any]:
        owner_repo = self._require_write_filter(payload.data, "repo", "issue_assign")
        issue_number = self._require_write_filter(payload.data, "issue_number", "issue_assign")
        return await self._post_subresource(
            owner_repo,
            "issues",
            issue_number,
            "assignees",
            {"assignees": self._require_string_list(payload.data, "assignees", "issue_assign")},
        )

    def _optional_reviewer_lists(self, data: dict[str, Any]) -> dict[str, Any]:
        """Build the reviewers/team_reviewers request body, requiring at least one."""
        reviewers = data.get("reviewers")
        team_reviewers = data.get("team_reviewers")
        if not reviewers and not team_reviewers:
            raise ValueError("GitHub pr_review_request write requires 'reviewers' or 'team_reviewers' in data")
        request_body: dict[str, Any] = {}
        if reviewers:
            request_body["reviewers"] = self._require_string_list(data, "reviewers", "pr_review_request")
        if team_reviewers:
            request_body["team_reviewers"] = self._require_string_list(data, "team_reviewers", "pr_review_request")
        return request_body

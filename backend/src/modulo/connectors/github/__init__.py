"""GitHubConnector — async GitHub API connector."""

import asyncio
import base64
import binascii
import json
import random
import re
import time
from typing import Any, cast

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_GITHUB_API = "https://api.github.com"
_API_VERSION = "2022-11-28"

REQUIRED_SCOPES = frozenset({"repo", "read:org"})

# Actions accepted by the batch commit resource (write("commit") / write("files"))
_COMMIT_ACTIONS = frozenset({"create", "update", "delete", "move"})

# Retry/backoff configuration
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0

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


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from GitHub API response."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return None


def _parse_rate_limit_reset(response: httpx.Response) -> float | None:
    """Parse GitHub's rate-limit reset header (epoch seconds) into a retry delay.

    GitHub reports quota state via ``X-RateLimit-Reset`` (epoch seconds) on
    every response. When a 429 includes it, the client can wait until the quota
    window resets instead of guessing with backoff.
    """
    value = response.headers.get("X-RateLimit-Reset")
    if not value:
        return None
    try:
        reset_epoch = float(value)
    except (ValueError, TypeError):
        return None
    delay = reset_epoch - time.time()
    return delay if delay > 0 else None


def _rate_limit_detail(response: httpx.Response) -> str:
    """Summarise GitHub rate-limit quota headers for error/health detail strings."""
    parts = [f"{header}={value}" for header in _RATE_LIMIT_HEADERS if (value := response.headers.get(header))]
    return "; ".join(parts)


def _rate_limit_metadata(response: httpx.Response) -> dict[str, Any]:
    """Extract GitHub ``X-RateLimit-*`` headers into a metadata dict.

    GitHub reports quota state via ``X-RateLimit-Limit`` / ``X-RateLimit-Reset``
    etc. on every API response. Only headers present on the response are
    included, so an empty dict simply means no rate-limit reporting.
    """
    return {name: response.headers.get(name) for name in _RATE_LIMIT_HEADERS if name in response.headers}


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


def _decode_read_content(info: Any) -> None:
    """Decode base64 file content from the GitHub Contents API in place.

    Text files are returned with ``encoding == "base64"``; the decoded UTF-8
    text replaces ``content`` so agents can consume it directly. Content that
    is not decodable text (binary blobs) is left as the raw base64 value.
    Non-object responses (e.g. a directory listing array) are left untouched.
    """
    if not isinstance(info, dict):
        return
    if info.get("encoding") != "base64" or not isinstance(info.get("content"), str):
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

    def __init__(self, token: str, base_url: str = _GITHUB_API) -> None:
        self._token = token
        self._base_url = base_url

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITHUB

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
            return random.uniform(delay * 0.9, delay)  # noqa: S311 — non-cryptographic jitter for retry delays
        return random.uniform(0, delay)  # noqa: S311 — non-cryptographic jitter for retry delays

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
        return min(_BASE_DELAY * (1 << attempt), _MAX_DELAY)

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Call GitHub API with retry/backoff for retryable statuses.

        Retries on 429, 502, 503, 504 with exponential backoff + jitter.
        On 429 responses, prefers ``X-RateLimit-Reset`` (the quota window) then
        ``Retry-After`` to compute the wait instead of blind backoff.
        Wraps HTTP/network/parse errors as ValueError.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    r = await client.request(method, path, **kwargs)
                    if r.status_code == 304:
                        raise ValueError("GitHub API returned 304 Not Modified — resource unchanged")
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
                detail = f"GitHub API HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                if exc.response.status_code == 429:
                    quota = _rate_limit_detail(exc.response)
                    if quota:
                        detail = f"{detail} (quota: {quota})"
                raise ValueError(detail) from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    await asyncio.sleep(self._jitter(delay))
                    continue
                raise ValueError("GitHub API timeout") from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    await asyncio.sleep(self._jitter(delay))
                    continue
                raise ValueError("GitHub API connection error") from exc
        raise ValueError("GitHub API request failed after retries") from last_exc

    async def _parse_json(self, response: httpx.Response) -> Any:
        """Parse JSON response, wrapping decode errors as ValueError."""
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"GitHub API returned invalid JSON: {response.text[:200]}") from exc

    async def _parse_json_object(self, response: httpx.Response) -> dict[str, Any]:
        return cast("dict[str, Any]", await self._parse_json(response))

    @staticmethod
    def _parse_scopes_from_headers(response: httpx.Response) -> set[str]:
        header_value = response.headers.get("X-OAuth-Scopes", "")
        if header_value.strip():
            return {s.strip() for s in header_value.split(",")}
        return set()

    async def verify_scopes(self) -> set[str]:
        """Verify the token has required OAuth scopes via ``X-OAuth-Scopes`` header.

        Returns the set of missing scopes (empty if all present).
        Raises ``ValueError`` if the API call fails (non-200).
        """
        try:
            r = await self._call_api("GET", "/user")
        except ValueError as exc:
            raise ValueError(f"Cannot verify scopes: {exc}") from exc

        token_scopes = self._parse_scopes_from_headers(r)
        # admin:org is a superset of read:org
        if "admin:org" in token_scopes:
            token_scopes.add("read:org")
        return set(REQUIRED_SCOPES - token_scopes)

    async def health_check(self) -> HealthResult:
        """Check API access and verify required OAuth scopes via ``X-OAuth-Scopes`` header.

        Required scopes: ``repo``, ``read:org``.
        """
        try:
            r = await self._call_api("GET", "/user")
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

        try:
            user_login = (await self._parse_json(r)).get("login", "")
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

        token_scopes = self._parse_scopes_from_headers(r)
        missing = REQUIRED_SCOPES - token_scopes
        if missing:
            return HealthResult(
                ok=False,
                detail=f"Missing scopes: {', '.join(sorted(missing))}. Required: {', '.join(sorted(REQUIRED_SCOPES))}",
            )

        return HealthResult(ok=True, detail=user_login)

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

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "repos":
                r = await self._call_api("GET", "/user/repos", params={"per_page": q.limit})
                data: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return self._result(data, r, total=len(data), next_cursor=links.get("next"))
            case "file":
                owner_repo = self._require_filter(q.filters, "repo", "file")
                path = _validate_path(self._require_filter(q.filters, "path", "file"), "file")
                ref = q.filters.get("ref", "main")
                r = await self._call_api("GET", f"/repos/{owner_repo}/contents/{path}", params={"ref": ref})
                info = await self._parse_json_object(r)
                _decode_read_content(info)
                return self._result([info], r)
            case "tree":
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
            case "pulls":
                owner_repo = self._require_filter(q.filters, "repo", "pulls")
                state = q.filters.get("state", "open")
                params: dict[str, Any] = {"state": state, "per_page": q.limit}
                if "sort" in q.filters:
                    params["sort"] = q.filters["sort"]
                if "direction" in q.filters:
                    params["direction"] = q.filters["direction"]
                r = await self._call_api("GET", f"/repos/{owner_repo}/pulls", params=params)
                prs: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return self._result(prs, r, total=len(prs), next_cursor=links.get("next"))
            case "pr_commits":
                owner_repo = self._require_filter(q.filters, "repo", "pr_commits")
                pull_number = self._require_filter(q.filters, "pull_number", "pr_commits")
                r = await self._call_api(
                    "GET",
                    f"/repos/{owner_repo}/pulls/{pull_number}/commits",
                    params={"per_page": q.limit},
                )
                commits: list[dict[str, Any]] = await self._parse_json(r)
                return self._result(commits, r, total=len(commits))
            case "pr_files":
                owner_repo = self._require_filter(q.filters, "repo", "pr_files")
                pull_number = self._require_filter(q.filters, "pull_number", "pr_files")
                r = await self._call_api(
                    "GET",
                    f"/repos/{owner_repo}/pulls/{pull_number}/files",
                    params={"per_page": q.limit},
                )
                files: list[dict[str, Any]] = await self._parse_json(r)
                return self._result(files, r, total=len(files))
            case "issues":
                owner_repo = self._require_filter(q.filters, "repo", "issues")
                params = {"per_page": q.limit}
                for key in ("state", "labels", "sort", "direction", "milestone", "assignee", "since"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                r = await self._call_api("GET", f"/repos/{owner_repo}/issues", params=params)
                issues: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return self._result(issues, r, total=len(issues), next_cursor=links.get("next"))
            case "issue":
                owner_repo = self._require_filter(q.filters, "repo", "issue")
                issue_number = self._require_filter(q.filters, "issue_number", "issue")
                r = await self._call_api("GET", f"/repos/{owner_repo}/issues/{issue_number}")
                return self._result([await self._parse_json(r)], r)
            case "labels":
                owner_repo = self._require_filter(q.filters, "repo", "labels")
                r = await self._call_api("GET", f"/repos/{owner_repo}/labels", params={"per_page": q.limit})
                labels: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return self._result(labels, r, total=len(labels), next_cursor=links.get("next"))
            case "milestones":
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
                links = _parse_link_header(r)
                return self._result(milestones, r, total=len(milestones), next_cursor=links.get("next"))
            case "issue_comments":
                owner_repo = self._require_filter(q.filters, "repo", "issue_comments")
                issue_number = self._require_filter(q.filters, "issue_number", "issue_comments")
                r = await self._call_api(
                    "GET",
                    f"/repos/{owner_repo}/issues/{issue_number}/comments",
                    params={"per_page": q.limit},
                )
                comments: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return self._result(comments, r, total=len(comments), next_cursor=links.get("next"))
            case "issue_events":
                owner_repo = self._require_filter(q.filters, "repo", "issue_events")
                issue_number = self._require_filter(q.filters, "issue_number", "issue_events")
                r = await self._call_api(
                    "GET",
                    f"/repos/{owner_repo}/issues/{issue_number}/events",
                    params={"per_page": q.limit},
                )
                events: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return self._result(events, r, total=len(events), next_cursor=links.get("next"))
            case "assignees":
                owner_repo = self._require_filter(q.filters, "repo", "assignees")
                r = await self._call_api("GET", f"/repos/{owner_repo}/assignees", params={"per_page": q.limit})
                assignees: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return self._result(assignees, r, total=len(assignees), next_cursor=links.get("next"))
            case "timeline":
                owner_repo = self._require_filter(q.filters, "repo", "timeline")
                issue_number = self._require_filter(q.filters, "issue_number", "timeline")
                r = await self._call_api(
                    "GET",
                    f"/repos/{owner_repo}/issues/{issue_number}/timeline",
                    params={"per_page": q.limit},
                )
                timeline: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return self._result(timeline, r, total=len(timeline), next_cursor=links.get("next"))
            case "pr_diff":
                owner_repo = self._require_filter(q.filters, "repo", "pr_diff")
                pull_number = self._require_filter(q.filters, "pull_number", "pr_diff")
                r = await self._call_api(
                    "GET",
                    f"/repos/{owner_repo}/pulls/{pull_number}",
                    headers={"Accept": "application/vnd.github.v3.diff"},
                )
                return self._result([{"diff": r.text}], r, total=1)
            case "search_issues":
                search_query = self._require_filter(q.filters, "q", "search_issues")
                params = {"q": search_query, "per_page": q.limit}
                for key in ("sort", "order", "state", "labels", "assignee", "created", "updated"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                r = await self._call_api("GET", "/search/issues", params=params)
                body = await self._parse_json_object(r)
                items = cast("list[dict[str, Any]]", body.get("items", []))
                links = _parse_link_header(r)
                return self._result(items, r, total=body.get("total_count"), next_cursor=links.get("next"))
            case "rate_limit":
                r = await self._call_api("GET", "/rate_limit")
                body = await self._parse_json_object(r)
                resources = cast("dict[str, Any]", body.get("resources", {}))
                return self._result([resources], r, total=1)
            case _:
                raise ValueError(f"Unsupported GitHub resource: {q.resource!r}")

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

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "commit" | "files":
                owner_repo = self._require_write_filter(payload.data, "repo", payload.resource)
                actions = payload.data.get("actions")
                if not isinstance(actions, list) or not actions:
                    raise ValueError(f"GitHub resource {payload.resource!r} requires a non-empty 'actions' list")
                ref = payload.data.get("ref", payload.data.get("branch", "main"))
                if not isinstance(ref, str) or not ref:
                    raise ValueError(f"GitHub resource {payload.resource!r} requires a non-empty 'ref' or 'branch'")
                message = payload.data.get("message", "Update via Modulo")
                if not isinstance(message, str) or not message:
                    raise ValueError(f"GitHub resource {payload.resource!r} requires a non-empty 'message'")
                targeted_paths: set[str] = set()
                for action in actions:
                    if not isinstance(action, dict):
                        raise ValueError(f"GitHub resource {payload.resource!r}: each action must be an object")
                    action_type = action.get("action")
                    if action_type not in _COMMIT_ACTIONS:
                        raise ValueError(
                            f"GitHub resource {payload.resource!r}: action {action_type!r} must be one of "
                            f"{sorted(_COMMIT_ACTIONS)}",
                        )
                    path = action.get("path")
                    if not isinstance(path, str) or not path:
                        raise ValueError(f"GitHub resource {payload.resource!r}: each action requires 'path'")
                    _validate_path(path, payload.resource)
                    targets: tuple[str, ...]
                    if action_type == "move":
                        previous_path = action.get("previous_path")
                        if not isinstance(previous_path, str) or not previous_path:
                            raise ValueError(
                                f"GitHub resource {payload.resource!r}: move action requires 'previous_path'"
                            )
                        _validate_path(previous_path, payload.resource)
                        targets = (previous_path, path)
                    else:
                        targets = (path,)
                    for targeted in targets:
                        if targeted in targeted_paths:
                            raise ValueError(
                                f"GitHub resource {payload.resource!r}: path {targeted!r} is targeted "
                                "more than once by the batch",
                            )
                        targeted_paths.add(targeted)
                    if action_type not in {"move", "delete"} and not isinstance(action.get("content"), str):
                        raise ValueError(
                            f"GitHub resource {payload.resource!r}: action {action_type!r} requires string 'content'",
                        )
                base_sha = await self._resolve_commit_sha(owner_repo, ref, payload.resource)
                tree_entries: list[dict[str, Any]] = []
                for action in actions:
                    action_type = action["action"]
                    path = action["path"]
                    if action_type == "delete":
                        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                        continue
                    if action_type == "move":
                        previous_path = action["previous_path"]
                        tree_entries.append({"path": previous_path, "mode": "100644", "type": "blob", "sha": None})
                        content = await self._read_file_text(owner_repo, previous_path, ref)
                    else:
                        content = action["content"]
                    blob_sha = await self._create_blob(owner_repo, content)
                    tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
                tree_sha = await self._create_tree(owner_repo, base_sha, tree_entries)
                commit_sha = await self._create_commit(owner_repo, message, tree_sha, base_sha)
                ref_response = await self._update_ref(owner_repo, ref, commit_sha)
                return await self._parse_json_object(ref_response)
            case "file":
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
            case "issue":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue")
                issue_body: dict[str, Any] = {
                    "title": self._require_write_filter(payload.data, "title", "issue"),
                }
                if "body" in payload.data:
                    issue_body["body"] = payload.data["body"]
                if "labels" in payload.data:
                    issue_body["labels"] = payload.data["labels"]
                if "assignees" in payload.data:
                    issue_body["assignees"] = payload.data["assignees"]
                if "milestone" in payload.data:
                    issue_body["milestone"] = payload.data["milestone"]
                r = await self._call_api("POST", f"/repos/{owner_repo}/issues", json=issue_body)
                return await self._parse_json_object(r)
            case "issue_update":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_update")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_update")
                update_body: dict[str, Any] = {}
                for key in ("state", "title", "body", "labels", "milestone"):
                    if key in payload.data:
                        update_body[key] = payload.data[key]
                r = await self._call_api("PATCH", f"/repos/{owner_repo}/issues/{issue_number}", json=update_body)
                return await self._parse_json_object(r)
            case "issue_comment":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_comment")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_comment")
                r = await self._call_api(
                    "POST",
                    f"/repos/{owner_repo}/issues/{issue_number}/comments",
                    json={"body": self._require_write_filter(payload.data, "body", "issue_comment")},
                )
                return await self._parse_json_object(r)
            case "issue_label":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_label")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_label")
                r = await self._call_api(
                    "POST",
                    f"/repos/{owner_repo}/issues/{issue_number}/labels",
                    json={"labels": self._require_string_list(payload.data, "labels", "issue_label")},
                )
                return await self._parse_json_object(r)
            case "issue_reaction":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_reaction")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_reaction")
                r = await self._call_api(
                    "POST",
                    f"/repos/{owner_repo}/issues/{issue_number}/reactions",
                    json={"content": self._require_write_filter(payload.data, "content", "issue_reaction")},
                    headers={"Accept": "application/vnd.github.squirrel-girl-preview+json"},
                )
                return await self._parse_json_object(r)
            case "label":
                owner_repo = self._require_write_filter(payload.data, "repo", "label")
                label_body: dict[str, Any] = {
                    "name": self._require_write_filter(payload.data, "name", "label"),
                    "color": self._require_write_filter(payload.data, "color", "label"),
                }
                if "description" in payload.data:
                    label_body["description"] = payload.data["description"]
                r = await self._call_api("POST", f"/repos/{owner_repo}/labels", json=label_body)
                return await self._parse_json_object(r)
            case "milestone":
                owner_repo = self._require_write_filter(payload.data, "repo", "milestone")
                milestone_body: dict[str, Any] = {
                    "title": self._require_write_filter(payload.data, "title", "milestone"),
                }
                if "description" in payload.data:
                    milestone_body["description"] = payload.data["description"]
                if "due_on" in payload.data:
                    milestone_body["due_on"] = payload.data["due_on"]
                r = await self._call_api("POST", f"/repos/{owner_repo}/milestones", json=milestone_body)
                return await self._parse_json_object(r)
            case "pr":
                owner_repo = self._require_write_filter(payload.data, "repo", "pr")
                pr_body: dict[str, Any] = {
                    "title": self._require_write_filter(payload.data, "title", "pr"),
                    "head": self._require_write_filter(payload.data, "head", "pr"),
                    "base": self._require_write_filter(payload.data, "base", "pr"),
                }
                if "body" in payload.data:
                    pr_body["body"] = payload.data["body"]
                if "draft" in payload.data:
                    pr_body["draft"] = payload.data["draft"]
                if "maintainer_can_modify" in payload.data:
                    pr_body["maintainer_can_modify"] = payload.data["maintainer_can_modify"]
                r = await self._call_api("POST", f"/repos/{owner_repo}/pulls", json=pr_body)
                return await self._parse_json_object(r)
            case "pr_review":
                owner_repo = self._require_write_filter(payload.data, "repo", "pr_review")
                pull_number = self._require_write_filter(payload.data, "pull_number", "pr_review")
                event = self._require_write_filter(payload.data, "event", "pr_review")
                if event not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
                    raise ValueError(
                        f"GitHub pr_review 'event' must be one of APPROVE, REQUEST_CHANGES, COMMENT; got {event!r}"
                    )
                review_body: dict[str, Any] = {
                    "event": event,
                    "body": payload.data.get("body", ""),
                }
                if "comments" in payload.data:
                    review_body["comments"] = payload.data["comments"]
                r = await self._call_api(
                    "POST",
                    f"/repos/{owner_repo}/pulls/{pull_number}/reviews",
                    json=review_body,
                )
                return await self._parse_json_object(r)
            case "pr_comment":
                owner_repo = self._require_write_filter(payload.data, "repo", "pr_comment")
                pull_number = self._require_write_filter(payload.data, "pull_number", "pr_comment")
                body_value = self._require_write_filter(payload.data, "body", "pr_comment")
                r = await self._call_api(
                    "POST",
                    f"/repos/{owner_repo}/pulls/{pull_number}/comments",
                    json={"body": body_value},
                )
                return await self._parse_json_object(r)
            case "pr_update":
                owner_repo = self._require_write_filter(payload.data, "repo", "pr_update")
                pull_number = self._require_write_filter(payload.data, "pull_number", "pr_update")
                update: dict[str, Any] = {}
                for key in ("title", "body", "state", "base"):
                    if key in payload.data:
                        update[key] = payload.data[key]
                r = await self._call_api("PATCH", f"/repos/{owner_repo}/pulls/{pull_number}", json=update)
                return await self._parse_json_object(r)
            case "pr_merge":
                owner_repo = self._require_write_filter(payload.data, "repo", "pr_merge")
                pull_number = self._require_write_filter(payload.data, "pull_number", "pr_merge")
                merge_body: dict[str, Any] = {}
                for key in ("commit_title", "commit_message", "merge_method", "sha"):
                    if key in payload.data:
                        merge_body[key] = payload.data[key]
                r = await self._call_api("PUT", f"/repos/{owner_repo}/pulls/{pull_number}/merge", json=merge_body)
                return await self._parse_json_object(r)
            case "pr_review_request":
                owner_repo = self._require_write_filter(payload.data, "repo", "pr_review_request")
                pull_number = self._require_write_filter(payload.data, "pull_number", "pr_review_request")
                reviewers = payload.data.get("reviewers")
                team_reviewers = payload.data.get("team_reviewers")
                if not reviewers and not team_reviewers:
                    raise ValueError("GitHub pr_review_request write requires 'reviewers' or 'team_reviewers' in data")
                request_body: dict[str, Any] = {}
                if reviewers:
                    request_body["reviewers"] = self._require_string_list(
                        payload.data, "reviewers", "pr_review_request"
                    )
                if team_reviewers:
                    request_body["team_reviewers"] = self._require_string_list(
                        payload.data, "team_reviewers", "pr_review_request"
                    )
                r = await self._call_api(
                    "POST",
                    f"/repos/{owner_repo}/pulls/{pull_number}/requested_reviewers",
                    json=request_body,
                )
                return await self._parse_json_object(r)
            case "pr_label":
                owner_repo = self._require_write_filter(payload.data, "repo", "pr_label")
                pull_number = self._require_write_filter(payload.data, "pull_number", "pr_label")
                r = await self._call_api(
                    "POST",
                    f"/repos/{owner_repo}/issues/{pull_number}/labels",
                    json={"labels": self._require_string_list(payload.data, "labels", "pr_label")},
                )
                return await self._parse_json_object(r)
            case "issue_assign":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_assign")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_assign")
                r = await self._call_api(
                    "POST",
                    f"/repos/{owner_repo}/issues/{issue_number}/assignees",
                    json={"assignees": self._require_string_list(payload.data, "assignees", "issue_assign")},
                )
                return await self._parse_json_object(r)
            case _:
                raise ValueError(f"Unsupported GitHub write resource: {payload.resource!r}")

"""GitHubConnector — async GitHub API connector."""

import asyncio
import json
import random
import re
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

# Retry/backoff configuration
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0

# Link header regex for pagination
_LINK_HEADER_RE = re.compile(r'<([^>]+)>\s*;\s*rel="(\w+)"')


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from GitHub API response."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return None


def _parse_link_header(response: httpx.Response) -> dict[str, str]:
    """Parse GitHub Link header into {rel: url} dict."""
    link_value = response.headers.get("Link", "")
    if not link_value:
        return {}
    return {rel: url for url, rel in _LINK_HEADER_RE.findall(link_value)}


class GitHubConnector(ConnectorBase):
    """Read/write GitHub via the REST API.

    Supports configurable ``base_url`` for GHES (default: ``https://api.github.com``).
    Retries 429/502/503/504 with exponential backoff + jitter (max 3 retries).
    Includes random jitter in retry delays to avoid thundering herd.
    Parses Link header for pagination cursor on list endpoints.

    Supported query resources:
      "repos"           — list repositories accessible to the token
      "file"            — read a file; filters: {"repo": "owner/repo", "path": "...", "ref": "main"}
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

    Supported write resources:
      "file"            — create/update a file; data: {"repo": ..., "path": ..., "content": <base64>,
                           "message": ..., "sha": <required for update>}
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
    """

    def __init__(self, token: str, base_url: str = _GITHUB_API) -> None:
        self._token = token
        self._base_url = base_url

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITHUB

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "Accept": "application/vnd.github+json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers(), timeout=30)

    @staticmethod
    def _jitter(delay: float) -> float:
        """Add random jitter: [0, delay) to avoid thundering herd."""
        return random.uniform(0, delay)  # noqa: S311 — non-cryptographic jitter for retry delays

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Call GitHub API with retry/backoff for retryable statuses.

        Retries on 429, 502, 503, 504 with exponential backoff + jitter.
        Adds random jitter to retry delays to avoid thundering herd.
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
                raise ValueError(f"GitHub API HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
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
        return cast(dict[str, Any], await self._parse_json(response))

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

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "repos":
                r = await self._call_api("GET", "/user/repos", params={"per_page": q.limit})
                data: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return ConnectorResult(records=data, total=len(data), next_cursor=links.get("next"))
            case "file":
                owner_repo = self._require_filter(q.filters, "repo", "file")
                path = self._require_filter(q.filters, "path", "file")
                ref = q.filters.get("ref", "main")
                r = await self._call_api("GET", f"/repos/{owner_repo}/contents/{path}", params={"ref": ref})
                return ConnectorResult(records=[await self._parse_json(r)])
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
                return ConnectorResult(records=prs, total=len(prs), next_cursor=links.get("next"))
            case "pr_commits":
                owner_repo = self._require_filter(q.filters, "repo", "pr_commits")
                pull_number = self._require_filter(q.filters, "pull_number", "pr_commits")
                r = await self._call_api(
                    "GET",
                    f"/repos/{owner_repo}/pulls/{pull_number}/commits",
                    params={"per_page": q.limit},
                )
                commits: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=commits, total=len(commits))
            case "pr_files":
                owner_repo = self._require_filter(q.filters, "repo", "pr_files")
                pull_number = self._require_filter(q.filters, "pull_number", "pr_files")
                r = await self._call_api(
                    "GET",
                    f"/repos/{owner_repo}/pulls/{pull_number}/files",
                    params={"per_page": q.limit},
                )
                files: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=files, total=len(files))
            case "issues":
                owner_repo = self._require_filter(q.filters, "repo", "issues")
                params = {"per_page": q.limit}
                for key in ("state", "labels", "sort", "direction", "milestone", "assignee", "since"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                r = await self._call_api("GET", f"/repos/{owner_repo}/issues", params=params)
                issues: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return ConnectorResult(records=issues, total=len(issues), next_cursor=links.get("next"))
            case "issue":
                owner_repo = self._require_filter(q.filters, "repo", "issue")
                issue_number = self._require_filter(q.filters, "issue_number", "issue")
                r = await self._call_api("GET", f"/repos/{owner_repo}/issues/{issue_number}")
                return ConnectorResult(records=[await self._parse_json(r)])
            case "labels":
                owner_repo = self._require_filter(q.filters, "repo", "labels")
                r = await self._call_api("GET", f"/repos/{owner_repo}/labels", params={"per_page": q.limit})
                labels: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return ConnectorResult(records=labels, total=len(labels), next_cursor=links.get("next"))
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
                return ConnectorResult(records=milestones, total=len(milestones), next_cursor=links.get("next"))
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
                return ConnectorResult(records=comments, total=len(comments), next_cursor=links.get("next"))
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
                return ConnectorResult(records=events, total=len(events), next_cursor=links.get("next"))
            case "assignees":
                owner_repo = self._require_filter(q.filters, "repo", "assignees")
                r = await self._call_api("GET", f"/repos/{owner_repo}/assignees", params={"per_page": q.limit})
                assignees: list[dict[str, Any]] = await self._parse_json(r)
                links = _parse_link_header(r)
                return ConnectorResult(records=assignees, total=len(assignees), next_cursor=links.get("next"))
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
                return ConnectorResult(records=timeline, total=len(timeline), next_cursor=links.get("next"))
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
            case "file":
                owner_repo = self._require_write_filter(payload.data, "repo", "file")
                path = self._require_write_filter(payload.data, "path", "file")
                body: dict[str, Any] = {
                    "message": payload.data.get("message", "Update via Modulo"),
                    "content": self._require_write_filter(payload.data, "content", "file"),
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
            case _:
                raise ValueError(f"Unsupported GitHub write resource: {payload.resource!r}")

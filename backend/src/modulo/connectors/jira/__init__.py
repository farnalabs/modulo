"""JiraConnector — async Jira Cloud REST API v3 connector."""

import asyncio
import json
import random
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

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from API response."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return None


def _compute_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Compute retry delay with exponential backoff, jitter, and optional Retry-After."""
    if response:
        retry_after = _parse_retry_after(response)
        if retry_after is not None:
            return float(min(retry_after, _MAX_DELAY))
    jitter = random.uniform(0, 1)  # noqa: S311 — non-cryptographic jitter for retry delays
    return float(min(_BASE_DELAY * (2**attempt) + jitter, _MAX_DELAY))


class JiraConnector(ConnectorBase):
    """Read/write Jira issues via the REST API v3.

    Config (from config_json):
      "instance" — your-domain.atlassian.net (without https://)

    Credentials (from credentials_ciphertext):
      "email"    — Atlassian account email (for Basic auth)
      "api_token" — Atlassian API token (for Basic auth)
    Or:
      "token"    — OAuth/Personal Access Token

    Supported query resources:
      "issue"           — get a single issue; filters: {"issue_key": "PROJ-123"}
      "search"          — JQL search; filters: {"jql": "project = PROJ", "max_results": 50}
      "issue_comments"  — list comments on an issue; filters: {"issue_key": "PROJ-123"}
      "transitions"     — get available transitions for an issue; filters: {"issue_key": "PROJ-123"}
      "projects"        — list accessible projects

    Supported write resources:
      "issue"           — create an issue; data: {"project": {"key": "PROJ"}, "summary": "...",
                           "issuetype": {"name": "Task"}, ...}
      "issue_update"    — update an issue; data: {"issue_key": "PROJ-123", "fields": {...}}
      "issue_comment"   — add a comment to an issue; data: {"issue_key": "PROJ-123", "body": "..."}
      "transition"      — transition an issue; data: {"issue_key": "PROJ-123", "transition_id": "..."}
    """

    def __init__(self, instance: str, creds: dict[str, str]) -> None:
        self._instance = instance.rstrip("/")
        self._base_url = f"https://{self._instance}/rest/api/3"
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
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            auth=self._auth,
            timeout=30,
        )

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Call Jira API with retry/backoff for retryable statuses.

        Retries on 429, 502, 503, 504 with exponential backoff + jitter.
        Wraps HTTP/network/parse errors as ValueError.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    r = await client.request(method, path, **kwargs)
                    if r.status_code == 304:
                        raise ValueError("Jira API returned 304 Not Modified — resource unchanged")
                    if r.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                        delay = _compute_delay(attempt, r)
                        await asyncio.sleep(delay)
                        continue
                    r.raise_for_status()
                    return r
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    delay = _compute_delay(attempt, exc.response)
                    await asyncio.sleep(delay)
                    continue
                raise ValueError(f"Jira API HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = _compute_delay(attempt)
                    await asyncio.sleep(delay)
                    continue
                raise ValueError("Jira API timeout") from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = _compute_delay(attempt)
                    await asyncio.sleep(delay)
                    continue
                raise ValueError("Jira API connection error") from exc
        raise ValueError("Jira API request failed after retries") from last_exc

    async def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        """Safely parse JSON response, wrapping decode errors."""
        try:
            return cast(dict[str, Any], response.json())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Jira API invalid response: {response.text[:200]}") from exc

    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the current user's profile."""
        try:
            r = await self._call_api("GET", "/myself")
            user_info = await self._parse_json(r)
            display_name = user_info.get("displayName", "")
            return HealthResult(ok=True, detail=display_name)
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "issue":
                if "issue_key" not in q.filters:
                    raise ValueError("Jira issue query requires 'issue_key' filter")
                issue_key = q.filters["issue_key"]
                r = await self._call_api("GET", f"/issue/{issue_key}")
                data: dict[str, Any] = await self._parse_json(r)
                return ConnectorResult(records=[data])
            case "search":
                jql = q.filters.get("jql", "")
                max_results = q.filters.get("max_results", q.limit)
                params: dict[str, Any] = {"jql": jql, "maxResults": max_results}
                if q.cursor:
                    params["startAt"] = int(q.cursor)
                r = await self._call_api("POST", "/search", json=params)
                body: dict[str, Any] = await self._parse_json(r)
                issues: list[dict[str, Any]] = body.get("issues", [])
                total = body.get("total", len(issues))
                start_at = body.get("startAt", 0)
                max_results = body.get("maxResults", max_results)
                next_cursor: str | None = None
                if start_at + max_results < total:
                    next_cursor = str(start_at + max_results)
                return ConnectorResult(records=issues, total=total, next_cursor=next_cursor)
            case "issue_comments":
                if "issue_key" not in q.filters:
                    raise ValueError("Jira issue_comments query requires 'issue_key' filter")
                issue_key = q.filters["issue_key"]
                comment_params: dict[str, Any] = {}
                if q.cursor:
                    comment_params["startAt"] = int(q.cursor)
                r = await self._call_api("GET", f"/issue/{issue_key}/comment", params=comment_params)
                body = await self._parse_json(r)
                comments = body.get("comments", [])
                total = body.get("total", len(comments))
                start_at = body.get("startAt", 0)
                max_results = body.get("maxResults", 50)
                comment_next_cursor: str | None = None
                if start_at + max_results < total:
                    comment_next_cursor = str(start_at + max_results)
                return ConnectorResult(records=comments, total=total, next_cursor=comment_next_cursor)
            case "transitions":
                if "issue_key" not in q.filters:
                    raise ValueError("Jira transitions query requires 'issue_key' filter")
                issue_key = q.filters["issue_key"]
                r = await self._call_api("GET", f"/issue/{issue_key}/transitions")
                body = await self._parse_json(r)
                transitions = body.get("transitions", [])
                return ConnectorResult(records=transitions, total=len(transitions))
            case "projects":
                r = await self._call_api("GET", "/project")
                data = await self._parse_json(r)
                projects = data if isinstance(data, list) else data.get("values", [])
                return ConnectorResult(records=projects, total=len(projects))
            case _:
                raise ValueError(f"Unsupported Jira resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "issue":
                r = await self._call_api("POST", "/issue", json=payload.data)
                created: dict[str, Any] = await self._parse_json(r)
                return created
            case "issue_update":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira issue update requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
                fields: dict[str, Any] = payload.data.get("fields", {})
                r = await self._call_api("PUT", f"/issue/{issue_key}", json={"fields": fields})
                return {"issue_key": issue_key, "updated": True}
            case "issue_comment":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira issue comment requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
                if "body" not in payload.data:
                    raise ValueError("Jira issue comment requires 'body' in data")
                body = payload.data["body"]
                r = await self._call_api("POST", f"/issue/{issue_key}/comment", json={"body": body})
                return await self._parse_json(r)
            case "transition":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira transition requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
                if "transition_id" not in payload.data:
                    raise ValueError("Jira transition requires 'transition_id' in data")
                transition_id = payload.data["transition_id"]
                r = await self._call_api(
                    "POST",
                    f"/issue/{issue_key}/transitions",
                    json={"transition": {"id": transition_id}},
                )
                return {"issue_key": issue_key, "transitioned": True}
            case _:
                raise ValueError(f"Unsupported Jira write resource: {payload.resource!r}")

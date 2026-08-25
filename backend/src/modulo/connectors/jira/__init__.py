"""JiraConnector — async Jira Cloud / Data Center REST API connector."""

import asyncio
import base64
import json
import random
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
)

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
        On 429 responses, prefers ``Retry-After`` then Jira Cloud's
        ``X-RateLimit-Reset`` (quota window) to compute the wait instead of
        blind backoff. Wraps HTTP/network/parse errors as ValueError.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    r = await client.request(method, path, **kwargs)
                    if r.status_code == 304:
                        raise ValueError("Jira API returned 304 Not Modified — resource unchanged")
                    if should_retry_status(r.status_code, attempt):
                        await asyncio.sleep(self._sleep_delay(r, attempt))
                        continue
                    r.raise_for_status()
                    return r
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if should_retry_status(exc.response.status_code, attempt):
                    await asyncio.sleep(self._sleep_delay(exc.response, attempt))
                    continue
                detail = exc.response.text[:200]
                if exc.response.status_code == 429:
                    quota = _rate_limit_detail(exc.response)
                    if quota:
                        detail = f"{detail} (quota: {quota})"
                raise ValueError(f"Jira API HTTP {exc.response.status_code}: {detail}") from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if should_retry_network(attempt):
                    await asyncio.sleep(_jitter(backoff_delay(attempt)))
                    continue
                raise ValueError("Jira API timeout") from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if should_retry_network(attempt):
                    await asyncio.sleep(_jitter(backoff_delay(attempt)))
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
                return ConnectorResult(
                    records=[data],
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
            case "search":
                jql = q.filters.get("jql", "")
                max_results = q.filters.get("max_results", q.limit)
                params: dict[str, Any] = {"jql": jql, "maxResults": max_results}
                if q.cursor:
                    params["startAt"] = int(q.cursor)
                r = await self._call_api("POST", "/search", json=params)
                body: dict[str, Any] = await self._parse_json(r)
                issues = body.get("issues", [])
                if not isinstance(issues, list):
                    issues = []
                total = _safe_int(body.get("total"), len(issues))
                start_at = _safe_int(body.get("startAt"), 0)
                max_results = _safe_int(body.get("maxResults"), max_results)
                next_cursor: str | None = None
                if start_at + max_results < total:
                    next_cursor = str(start_at + max_results)
                return ConnectorResult(
                    records=issues,
                    total=total,
                    next_cursor=next_cursor,
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
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
                if not isinstance(comments, list):
                    comments = []
                total = _safe_int(body.get("total"), len(comments))
                start_at = _safe_int(body.get("startAt"), 0)
                max_results = _safe_int(body.get("maxResults"), 50)
                comment_next_cursor: str | None = None
                if start_at + max_results < total:
                    comment_next_cursor = str(start_at + max_results)
                return ConnectorResult(
                    records=comments,
                    total=total,
                    next_cursor=comment_next_cursor,
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
            case "transitions":
                if "issue_key" not in q.filters:
                    raise ValueError("Jira transitions query requires 'issue_key' filter")
                issue_key = q.filters["issue_key"]
                r = await self._call_api("GET", f"/issue/{issue_key}/transitions")
                body = await self._parse_json(r)
                transitions = body.get("transitions", [])
                return ConnectorResult(
                    records=transitions,
                    total=len(transitions),
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
            case "issue_attachments":
                if "issue_key" not in q.filters:
                    raise ValueError("Jira issue_attachments query requires 'issue_key' filter")
                issue_key = q.filters["issue_key"]
                r = await self._call_api("GET", f"/issue/{issue_key}")
                body = await self._parse_json(r)
                attachments = body.get("fields", {}).get("attachment") or []
                return ConnectorResult(
                    records=attachments,
                    total=len(attachments),
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
            case "issue_remote_links":
                if "issue_key" not in q.filters:
                    raise ValueError("Jira issue_remote_links query requires 'issue_key' filter")
                issue_key = q.filters["issue_key"]
                r = await self._call_api("GET", f"/issue/{issue_key}/remotelink")
                body = await self._parse_json(r)
                remote_links: list[Any] = body if isinstance(body, list) else body.get("links", [])
                return ConnectorResult(
                    records=remote_links,
                    total=len(remote_links),
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
            case "project_components":
                if "project" not in q.filters:
                    raise ValueError("Jira project_components query requires 'project' filter")
                project = q.filters["project"]
                r = await self._call_api("GET", f"/project/{project}/components")
                data = await self._parse_json(r)
                components: list[Any] = data if isinstance(data, list) else []
                return ConnectorResult(
                    records=components,
                    total=len(components),
                    metadata={
                        "rate_limit": _rate_limit_metadata(r),
                        "project": project,
                    },
                )
            case "project_versions":
                if "project" not in q.filters:
                    raise ValueError("Jira project_versions query requires 'project' filter")
                project = q.filters["project"]
                r = await self._call_api("GET", f"/project/{project}/versions")
                data = await self._parse_json(r)
                versions: list[Any] = data if isinstance(data, list) else []
                return ConnectorResult(
                    records=versions,
                    total=len(versions),
                    metadata={
                        "rate_limit": _rate_limit_metadata(r),
                        "project": project,
                    },
                )
            case "projects":
                r = await self._call_api("GET", "/project")
                data = await self._parse_json(r)
                projects = data if isinstance(data, list) else data.get("values", [])
                return ConnectorResult(
                    records=projects,
                    total=len(projects),
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
            case "field_metadata":
                if "project" not in q.filters:
                    raise ValueError("Jira field_metadata query requires 'project' filter")
                project = q.filters["project"]
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
            case "fields":
                r = await self._call_api("GET", "/field")
                data = await self._parse_json(r)
                fields: list[Any] = data if isinstance(data, list) else []
                return ConnectorResult(
                    records=fields,
                    total=len(fields),
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
            case "statuses":
                if "project" not in q.filters:
                    raise ValueError("Jira statuses query requires 'project' filter")
                project = q.filters["project"]
                r = await self._call_api("GET", f"/project/{project}/statuses")
                data = await self._parse_json(r)
                statuses: list[Any] = data if isinstance(data, list) else []
                return ConnectorResult(
                    records=statuses,
                    total=len(statuses),
                    metadata={
                        "rate_limit": _rate_limit_metadata(r),
                        "project": project,
                    },
                )
            case "attachments":
                if "issue_key" not in q.filters:
                    raise ValueError("Jira attachments query requires 'issue_key' filter")
                issue_key = q.filters["issue_key"]
                r = await self._call_api("GET", f"/issue/{issue_key}", params={"fields": "attachment"})
                body = await self._parse_json(r)
                attachments = body.get("fields", {}).get("attachment", [])
                return ConnectorResult(
                    records=attachments,
                    total=len(attachments),
                    metadata={"rate_limit": _rate_limit_metadata(r)},
                )
            case "attachment":
                if "attachment_id" not in q.filters:
                    raise ValueError("Jira attachment query requires 'attachment_id' filter")
                attachment_id = q.filters["attachment_id"]
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
            case "issue_assign":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira issue assign requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
                assignee_field = await self._resolve_assignee(payload.data)
                r = await self._call_api("PUT", f"/issue/{issue_key}", json={"fields": {"assignee": assignee_field}})
                return {"issue_key": issue_key, "assignee": assignee_field}
            case "issue_label":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira issue label requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
                add_labels = payload.data.get("add") or []
                remove_labels = payload.data.get("remove") or []
                if not add_labels and not remove_labels:
                    raise ValueError("Jira issue_label requires 'add' and/or 'remove' in data")
                target_labels = await self._compute_target_labels(issue_key, add_labels, remove_labels)
                r = await self._call_api("PUT", f"/issue/{issue_key}", json={"fields": {"labels": target_labels}})
                return {"issue_key": issue_key, "labels": target_labels}
            case "issue_delete":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira issue delete requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
                await self._call_api("DELETE", f"/issue/{issue_key}")
                return {"issue_key": issue_key, "deleted": True}
            case "issue_comment":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira issue comment requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
                if "body" not in payload.data:
                    raise ValueError("Jira issue comment requires 'body' in data")
                body = payload.data["body"]
                r = await self._call_api("POST", f"/issue/{issue_key}/comment", json={"body": body})
                comment: dict[str, Any] = await self._parse_json(r)
                return comment
            case "issue_attachment":
                return await self._upload_attachment(payload.data)
            case "issue_remote_link":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira issue_remote_link requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
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
            case "remote_link_delete":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira remote_link_delete requires 'issue_key' in data")
                if "link_id" not in payload.data:
                    raise ValueError("Jira remote_link_delete requires 'link_id' in data")
                issue_key = payload.data["issue_key"]
                link_id = payload.data["link_id"]
                await self._call_api("DELETE", f"/issue/{issue_key}/remotelink/{link_id}")
                return {"issue_key": issue_key, "link_id": link_id, "deleted": True}
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
            case "attachment":
                uploaded = await self._upload_attachment(payload.data)
                attachments = uploaded if isinstance(uploaded, list) else [uploaded]
                return {"issue_key": payload.data["issue_key"], "attachments": attachments}
            case _:
                raise ValueError(f"Unsupported Jira write resource: {payload.resource!r}")

    async def _upload_attachment(self, data: dict[str, Any]) -> dict[str, Any]:
        """Upload a file as an issue attachment via the Jira attachments API.

        Accepts ``issue_key`` + ``filename`` plus exactly one of ``content``
        (str) or ``file`` (bytes/str). Optional ``mime_type`` sets the upload
        content type (defaults to octet-stream). Optional extra keys
        (e.g. ``comment``) are passed through as multipart form fields. Sends
        the ``X-Atlassian-Token: no-check`` header Jira requires for attachment
        uploads to bypass XSRF protection.
        """
        if "issue_key" not in data:
            raise ValueError("Jira issue attachment requires 'issue_key' in data")
        issue_key = data["issue_key"]
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

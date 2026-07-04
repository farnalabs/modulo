"""GitHubConnector — async GitHub API connector."""

import json
from typing import Any

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


class GitHubConnector(ConnectorBase):
    """Read/write GitHub via the REST API.

    Supported query resources:
      "repos"           — list repositories accessible to the token
      "file"            — read a file; filters: {"repo": "owner/repo", "path": "...", "ref": "main"}
      "pulls"           — list pull requests; filters: {"repo": "owner/repo", "state": "open"}
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
    """

    def __init__(self, token: str) -> None:
        self._token = token

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
        return httpx.AsyncClient(base_url=_GITHUB_API, headers=self._headers(), timeout=30)

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Call GitHub API and wrap HTTP/network errors as ValueError."""
        try:
            async with self._client() as client:
                r = await client.request(method, path, **kwargs)
                if r.status_code == 304:
                    raise ValueError("GitHub API returned 304 Not Modified — resource unchanged")
                r.raise_for_status()
                return r
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitHub API HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.TimeoutException:
            raise ValueError("GitHub API timeout") from None
        except httpx.ConnectError:
            raise ValueError("GitHub API connection error") from None

    async def _parse_json(self, response: httpx.Response) -> Any:
        """Parse JSON response, wrapping decode errors as ValueError."""
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"GitHub API returned invalid JSON: {response.text[:200]}") from exc

    async def verify_scopes(self) -> set[str]:
        """Verify the token has required OAuth scopes via ``X-OAuth-Scopes`` header.

        Returns the set of missing scopes (empty if all present).
        Raises ``ValueError`` if the API call fails (non-200).
        """
        try:
            r = await self._call_api("GET", "/user")
        except ValueError as exc:
            raise ValueError(f"Cannot verify scopes: {exc}") from exc

        header_value = r.headers.get("X-OAuth-Scopes", "")
        token_scopes: set[str] = set()
        if header_value.strip():
            token_scopes = {s.strip() for s in header_value.split(",")}

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

        header_value = r.headers.get("X-OAuth-Scopes", "")
        token_scopes: set[str] = set()
        if header_value.strip():
            token_scopes = {s.strip() for s in header_value.split(",")}

        missing = REQUIRED_SCOPES - token_scopes
        if missing:
            return HealthResult(
                ok=False,
                detail=f"Missing scopes: {', '.join(sorted(missing))}. Required: {', '.join(sorted(REQUIRED_SCOPES))}",
            )

        return HealthResult(ok=True, detail=user_login)

    def _require_filter(self, filters: dict[str, Any], key: str, resource: str) -> str:
        """Get a required filter or raise a descriptive ValueError."""
        value = filters.get(key)
        if not value:
            raise ValueError(f"GitHub {resource} query requires '{key}' filter")
        return value

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "repos":
                r = await self._call_api("GET", "/user/repos", params={"per_page": q.limit})
                data: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=data, total=len(data))
            case "file":
                owner_repo = self._require_filter(q.filters, "repo", "file")
                path = self._require_filter(q.filters, "path", "file")
                ref = q.filters.get("ref", "main")
                r = await self._call_api("GET", f"/repos/{owner_repo}/contents/{path}", params={"ref": ref})
                return ConnectorResult(records=[await self._parse_json(r)])
            case "pulls":
                owner_repo = self._require_filter(q.filters, "repo", "pulls")
                state = q.filters.get("state", "open")
                r = await self._call_api(
                    "GET", f"/repos/{owner_repo}/pulls",
                    params={"state": state, "per_page": q.limit},
                )
                prs: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=prs, total=len(prs))
            case "issues":
                owner_repo = self._require_filter(q.filters, "repo", "issues")
                params: dict[str, Any] = {"per_page": q.limit}
                for key in ("state", "labels", "sort", "direction", "milestone", "assignee", "since"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                r = await self._call_api("GET", f"/repos/{owner_repo}/issues", params=params)
                issues: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=issues, total=len(issues))
            case "issue":
                owner_repo = self._require_filter(q.filters, "repo", "issue")
                issue_number = self._require_filter(q.filters, "issue_number", "issue")
                r = await self._call_api("GET", f"/repos/{owner_repo}/issues/{issue_number}")
                return ConnectorResult(records=[await self._parse_json(r)])
            case "labels":
                owner_repo = self._require_filter(q.filters, "repo", "labels")
                r = await self._call_api("GET", f"/repos/{owner_repo}/labels", params={"per_page": q.limit})
                labels: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=labels, total=len(labels))
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
                return ConnectorResult(records=milestones, total=len(milestones))
            case "issue_comments":
                owner_repo = self._require_filter(q.filters, "repo", "issue_comments")
                issue_number = self._require_filter(q.filters, "issue_number", "issue_comments")
                r = await self._call_api(
                    "GET", f"/repos/{owner_repo}/issues/{issue_number}/comments",
                    params={"per_page": q.limit},
                )
                comments: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=comments, total=len(comments))
            case "issue_events":
                owner_repo = self._require_filter(q.filters, "repo", "issue_events")
                issue_number = self._require_filter(q.filters, "issue_number", "issue_events")
                r = await self._call_api(
                    "GET", f"/repos/{owner_repo}/issues/{issue_number}/events",
                    params={"per_page": q.limit},
                )
                events: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=events, total=len(events))
            case "assignees":
                owner_repo = self._require_filter(q.filters, "repo", "assignees")
                r = await self._call_api("GET", f"/repos/{owner_repo}/assignees", params={"per_page": q.limit})
                assignees: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=assignees, total=len(assignees))
            case "timeline":
                owner_repo = self._require_filter(q.filters, "repo", "timeline")
                issue_number = self._require_filter(q.filters, "issue_number", "timeline")
                r = await self._call_api(
                    "GET", f"/repos/{owner_repo}/issues/{issue_number}/timeline",
                    params={"per_page": q.limit},
                )
                timeline: list[dict[str, Any]] = await self._parse_json(r)
                return ConnectorResult(records=timeline, total=len(timeline))
            case _:
                raise ValueError(f"Unsupported GitHub resource: {q.resource!r}")

    def _require_write_filter(self, data: dict[str, Any], key: str, resource: str) -> str:
        """Get a required write field or raise a descriptive ValueError."""
        value = data.get(key)
        if not value:
            raise ValueError(f"GitHub {resource} write requires '{key}' in data")
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
                result: dict[str, Any] = await self._parse_json(r)
                return result
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
                result = await self._parse_json(r)
                return result
            case "issue_update":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_update")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_update")
                update_body: dict[str, Any] = {}
                for key in ("state", "title", "body", "labels", "milestone"):
                    if key in payload.data:
                        update_body[key] = payload.data[key]
                r = await self._call_api("PATCH", f"/repos/{owner_repo}/issues/{issue_number}", json=update_body)
                result = await self._parse_json(r)
                return result
            case "issue_comment":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_comment")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_comment")
                r = await self._call_api(
                    "POST", f"/repos/{owner_repo}/issues/{issue_number}/comments",
                    json={"body": self._require_write_filter(payload.data, "body", "issue_comment")},
                )
                result = await self._parse_json(r)
                return result
            case "issue_label":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_label")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_label")
                r = await self._call_api(
                    "POST", f"/repos/{owner_repo}/issues/{issue_number}/labels",
                    json={"labels": self._require_write_filter(payload.data, "labels", "issue_label")},
                )
                result = await self._parse_json(r)
                return result
            case "issue_reaction":
                owner_repo = self._require_write_filter(payload.data, "repo", "issue_reaction")
                issue_number = self._require_write_filter(payload.data, "issue_number", "issue_reaction")
                r = await self._call_api(
                    "POST", f"/repos/{owner_repo}/issues/{issue_number}/reactions",
                    json={"content": self._require_write_filter(payload.data, "content", "issue_reaction")},
                    headers={"Accept": "application/vnd.github.squirrel-girl-preview+json"},
                )
                result = await self._parse_json(r)
                return result
            case "label":
                owner_repo = self._require_write_filter(payload.data, "repo", "label")
                label_body: dict[str, Any] = {
                    "name": self._require_write_filter(payload.data, "name", "label"),
                    "color": self._require_write_filter(payload.data, "color", "label"),
                }
                if "description" in payload.data:
                    label_body["description"] = payload.data["description"]
                r = await self._call_api("POST", f"/repos/{owner_repo}/labels", json=label_body)
                result = await self._parse_json(r)
                return result
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
                result = await self._parse_json(r)
                return result
            case _:
                raise ValueError(f"Unsupported GitHub write resource: {payload.resource!r}")

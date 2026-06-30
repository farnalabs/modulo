"""GitHubConnector — async GitHub API connector."""

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

    async def verify_scopes(self) -> set[str]:
        """Verify the token has required OAuth scopes via ``X-OAuth-Scopes`` header.

        Returns the set of missing scopes (empty if all present).
        Raises ``ValueError`` if the API call fails (non-200).
        """
        async with self._client() as client:
            r = await client.get("/user")

        if r.status_code != 200:
            raise ValueError(f"Cannot verify scopes: HTTP {r.status_code}")

        header_value = r.headers.get("X-OAuth-Scopes", "")
        token_scopes: set[str] = set()
        if header_value.strip():
            token_scopes = {s.strip() for s in header_value.split(",")}

        return set(REQUIRED_SCOPES - token_scopes)

    async def health_check(self) -> HealthResult:
        """Check API access and verify required OAuth scopes via ``X-OAuth-Scopes`` header.

        Required scopes: ``repo``, ``read:org``.
        """
        async with self._client() as client:
            r = await client.get("/user")

        if r.status_code != 200:
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

        user_login = r.json().get("login", "")

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

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "repos":
                    r = await client.get("/user/repos", params={"per_page": q.limit})
                    r.raise_for_status()
                    data: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=data, total=len(data))
                case "file":
                    owner_repo = q.filters["repo"]
                    path = q.filters["path"]
                    ref = q.filters.get("ref", "main")
                    r = await client.get(f"/repos/{owner_repo}/contents/{path}", params={"ref": ref})
                    r.raise_for_status()
                    return ConnectorResult(records=[r.json()])
                case "pulls":
                    owner_repo = q.filters["repo"]
                    state = q.filters.get("state", "open")
                    r = await client.get(
                        f"/repos/{owner_repo}/pulls",
                        params={"state": state, "per_page": q.limit},
                    )
                    r.raise_for_status()
                    prs: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=prs, total=len(prs))
                case "issues":
                    owner_repo = q.filters["repo"]
                    params: dict[str, Any] = {"per_page": q.limit}
                    for key in ("state", "labels", "sort", "direction", "milestone", "assignee", "since"):
                        if key in q.filters:
                            params[key] = q.filters[key]
                    r = await client.get(f"/repos/{owner_repo}/issues", params=params)
                    r.raise_for_status()
                    issues: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=issues, total=len(issues))
                case "issue":
                    owner_repo = q.filters["repo"]
                    issue_number = q.filters["issue_number"]
                    r = await client.get(f"/repos/{owner_repo}/issues/{issue_number}")
                    r.raise_for_status()
                    return ConnectorResult(records=[r.json()])
                case "labels":
                    owner_repo = q.filters["repo"]
                    r = await client.get(f"/repos/{owner_repo}/labels", params={"per_page": q.limit})
                    r.raise_for_status()
                    labels: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=labels, total=len(labels))
                case "milestones":
                    owner_repo = q.filters["repo"]
                    params = {"per_page": q.limit}
                    if "state" in q.filters:
                        params["state"] = q.filters["state"]
                    if "sort" in q.filters:
                        params["sort"] = q.filters["sort"]
                    if "direction" in q.filters:
                        params["direction"] = q.filters["direction"]
                    r = await client.get(f"/repos/{owner_repo}/milestones", params=params)
                    r.raise_for_status()
                    milestones: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=milestones, total=len(milestones))
                case "issue_comments":
                    owner_repo = q.filters["repo"]
                    issue_number = q.filters["issue_number"]
                    r = await client.get(
                        f"/repos/{owner_repo}/issues/{issue_number}/comments",
                        params={"per_page": q.limit},
                    )
                    r.raise_for_status()
                    comments: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=comments, total=len(comments))
                case "issue_events":
                    owner_repo = q.filters["repo"]
                    issue_number = q.filters["issue_number"]
                    r = await client.get(
                        f"/repos/{owner_repo}/issues/{issue_number}/events",
                        params={"per_page": q.limit},
                    )
                    r.raise_for_status()
                    events: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=events, total=len(events))
                case "assignees":
                    owner_repo = q.filters["repo"]
                    r = await client.get(f"/repos/{owner_repo}/assignees", params={"per_page": q.limit})
                    r.raise_for_status()
                    assignees: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=assignees, total=len(assignees))
                case "timeline":
                    owner_repo = q.filters["repo"]
                    issue_number = q.filters["issue_number"]
                    r = await client.get(
                        f"/repos/{owner_repo}/issues/{issue_number}/timeline",
                        params={"per_page": q.limit},
                    )
                    r.raise_for_status()
                    timeline: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=timeline, total=len(timeline))
                case _:
                    raise ValueError(f"Unsupported GitHub resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "file":
                    owner_repo = payload.data["repo"]
                    path = payload.data["path"]
                    body: dict[str, Any] = {
                        "message": payload.data.get("message", "Update via Modulo"),
                        "content": payload.data["content"],
                    }
                    if "sha" in payload.data:
                        body["sha"] = payload.data["sha"]
                    r = await client.put(f"/repos/{owner_repo}/contents/{path}", json=body)
                    r.raise_for_status()
                    result: dict[str, Any] = r.json()
                    return result
                case "issue":
                    owner_repo = payload.data["repo"]
                    issue_body: dict[str, Any] = {
                        "title": payload.data["title"],
                    }
                    if "body" in payload.data:
                        issue_body["body"] = payload.data["body"]
                    if "labels" in payload.data:
                        issue_body["labels"] = payload.data["labels"]
                    if "assignees" in payload.data:
                        issue_body["assignees"] = payload.data["assignees"]
                    if "milestone" in payload.data:
                        issue_body["milestone"] = payload.data["milestone"]
                    r = await client.post(f"/repos/{owner_repo}/issues", json=issue_body)
                    r.raise_for_status()
                    result = r.json()
                    return result
                case "issue_update":
                    owner_repo = payload.data["repo"]
                    issue_number = payload.data["issue_number"]
                    update_body: dict[str, Any] = {}
                    for key in ("state", "title", "body", "labels", "milestone"):
                        if key in payload.data:
                            update_body[key] = payload.data[key]
                    r = await client.patch(f"/repos/{owner_repo}/issues/{issue_number}", json=update_body)
                    r.raise_for_status()
                    result = r.json()
                    return result
                case "issue_comment":
                    owner_repo = payload.data["repo"]
                    issue_number = payload.data["issue_number"]
                    r = await client.post(
                        f"/repos/{owner_repo}/issues/{issue_number}/comments",
                        json={"body": payload.data["body"]},
                    )
                    r.raise_for_status()
                    result = r.json()
                    return result
                case "issue_label":
                    owner_repo = payload.data["repo"]
                    issue_number = payload.data["issue_number"]
                    r = await client.post(
                        f"/repos/{owner_repo}/issues/{issue_number}/labels",
                        json={"labels": payload.data["labels"]},
                    )
                    r.raise_for_status()
                    result = r.json()
                    return result
                case "issue_reaction":
                    owner_repo = payload.data["repo"]
                    issue_number = payload.data["issue_number"]
                    r = await client.post(
                        f"/repos/{owner_repo}/issues/{issue_number}/reactions",
                        json={"content": payload.data["content"]},
                        headers={
                            **self._headers(),
                            "Accept": "application/vnd.github.squirrel-girl-preview+json",
                        },
                    )
                    r.raise_for_status()
                    result = r.json()
                    return result
                case "label":
                    owner_repo = payload.data["repo"]
                    label_body: dict[str, Any] = {
                        "name": payload.data["name"],
                        "color": payload.data["color"],
                    }
                    if "description" in payload.data:
                        label_body["description"] = payload.data["description"]
                    r = await client.post(f"/repos/{owner_repo}/labels", json=label_body)
                    r.raise_for_status()
                    result = r.json()
                    return result
                case "milestone":
                    owner_repo = payload.data["repo"]
                    milestone_body: dict[str, Any] = {
                        "title": payload.data["title"],
                    }
                    if "description" in payload.data:
                        milestone_body["description"] = payload.data["description"]
                    if "due_on" in payload.data:
                        milestone_body["due_on"] = payload.data["due_on"]
                    r = await client.post(f"/repos/{owner_repo}/milestones", json=milestone_body)
                    r.raise_for_status()
                    result = r.json()
                    return result
                case _:
                    raise ValueError(f"Unsupported GitHub write resource: {payload.resource!r}")

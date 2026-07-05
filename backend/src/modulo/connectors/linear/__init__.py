"""LinearConnector — async Linear GraphQL API connector."""

import asyncio
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

_LINEAR_API = "https://api.linear.app"

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0

_ISSUE_FIELDS = """
  id
  identifier
  title
  description
  priority
  state { id name }
  assignee { id name email }
  team { id name key }
  createdAt
  updatedAt
  url
"""

_GET_ISSUE_QUERY = f"""
query($id: String!) {{
  issue(id: $id) {{
    {_ISSUE_FIELDS}
  }}
}}
"""

_CREATE_ISSUE_MUTATION = f"""
mutation($input: IssueCreateInput!) {{
  issueCreate(input: $input) {{
    success
    issue {{
      {_ISSUE_FIELDS}
    }}
  }}
}}
"""

_UPDATE_ISSUE_MUTATION = f"""
mutation($id: String!, $input: IssueUpdateInput!) {{
  issueUpdate(id: $id, input: $input) {{
    success
    issue {{
      {_ISSUE_FIELDS}
    }}
  }}
}}
"""

_SEARCH_ISSUES_QUERY = f"""
query($query: String!, $limit: Int, $cursor: String) {{
  searchIssues(query: $query, first: $limit, after: $cursor) {{
    nodes {{
      {_ISSUE_FIELDS}
    }}
    pageInfo {{
      hasNextPage
      endCursor
    }}
  }}
}}
"""

_VIEWER_QUERY = """
query {
  viewer {
    id
    name
    email
  }
}
"""

_ISSUE_COMMENTS_QUERY = """
query($issueId: String!) {
  issue(id: $issueId) {
    comments {
      nodes {
        id
        body
        user { id name email }
        createdAt
        updatedAt
      }
    }
  }
}
"""

_CREATE_COMMENT_MUTATION = """
mutation($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment {
      id
      body
      user { id name email }
      createdAt
      updatedAt
    }
  }
}
"""

_TEAMS_QUERY = """
query {
  teams {
    nodes {
      id
      name
      key
      description
    }
  }
}
"""

_TEAM_PROJECTS_QUERY = """
query($teamId: String!) {
  team(id: $teamId) {
    projects {
      nodes {
        id
        name
        description
        state
        startDate
        targetDate
      }
    }
  }
}
"""

_TEAM_STATES_QUERY = """
query($teamId: String!) {
  team(id: $teamId) {
    states {
      nodes {
        id
        name
        type
        position
      }
    }
  }
}
"""

_TEAM_LABELS_QUERY = """
query($teamId: String!) {
  team(id: $teamId) {
    labels {
      nodes {
        id
        name
        color
      }
    }
  }
}
"""

_TEAM_CYCLES_QUERY = """
query($teamId: String!) {
  team(id: $teamId) {
    cycles(first: 10) {
      nodes {
        id
        name
        startsAt
        endsAt
        completedAt
      }
    }
  }
}
"""


def _parse_retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if value:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return None


class LinearConnector(ConnectorBase):
    """Read/write Linear issues via the GraphQL API.

    Credentials (from credentials_ciphertext):
      "api_key"  — Linear personal API key

    Supported query resources:
      "issue"           — get a single issue by ID; filters: {"id": "uuid"}
      "search"          — search issues by text; filters: {"query": "..."}, supports cursor
      "issue_comments"  — get comments on an issue; filters: {"issueId": "uuid"}
      "teams"           — list all teams
      "team_projects"   — list projects for a team; filters: {"teamId": "uuid"}
      "team_states"     — list workflow states for a team; filters: {"teamId": "uuid"}
      "team_labels"     — list issue labels for a team; filters: {"teamId": "uuid"}
      "team_cycles"     — list active/upcoming cycles for a team; filters: {"teamId": "uuid"}

    Supported write resources:
      "issue"           — create an issue; data: {"title": "...", "teamId": "...", ...}
      "issue_update"    — update an issue; data: {"id": "...", "title": "...", ...}
      "issue_comment"   — add a comment to an issue; data: {"issueId": "...", "body": "..."}
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.LINEAR

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_LINEAR_API, headers=self._headers(), timeout=30)

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    r = await client.post(
                        "/graphql",
                        json={"query": query, "variables": variables or {}},
                    )
                    if r.status_code == 304:
                        raise ValueError("Linear API returned 304 Not Modified — resource unchanged")
                    if r.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                        retry_after = _parse_retry_after(r)
                        delay = (
                            min(retry_after, _MAX_DELAY)
                            if retry_after
                            else min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                        )
                        await asyncio.sleep(delay)
                        continue
                    r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    retry_after = _parse_retry_after(exc.response)
                    delay = (
                        min(retry_after, _MAX_DELAY)
                        if retry_after
                        else min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    )
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
                raise ValueError(
                    f"Linear API HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
            except httpx.TimeoutException as exc:
                if attempt < _MAX_RETRIES:
                    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
                raise ValueError("Linear API timeout") from exc
            except httpx.ConnectError as exc:
                if attempt < _MAX_RETRIES:
                    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
                raise ValueError("Linear API connection error") from exc
            try:
                body: dict[str, Any] = r.json()
            except json.JSONDecodeError as exc:
                raise ValueError(f"Linear API invalid response: {exc}") from exc
            if "errors" in body:
                raise ValueError(f"Linear API error: {body['errors']}")
            data: dict[str, Any] = body.get("data")
            if data is None:
                data = {}
            return data
        raise ValueError("Linear API request failed after retries") from last_exc

    async def health_check(self) -> HealthResult:
        try:
            data = await self._graphql(_VIEWER_QUERY)
            viewer = data.get("viewer", {})
            if not viewer:
                return HealthResult(ok=False, detail="No viewer returned — invalid API key?")
            name = viewer.get("name") or viewer.get("email") or viewer.get("id", "")
            return HealthResult(ok=True, detail=name)
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "issue":
                issue_id = q.filters.get("id")
                if not issue_id:
                    raise ValueError("Linear issue query requires 'id' filter")
                data = await self._graphql(_GET_ISSUE_QUERY, {"id": issue_id})
                issue = data.get("issue")
                if issue is None:
                    return ConnectorResult(records=[])
                return ConnectorResult(records=[issue])
            case "search":
                query_text = q.filters.get("query", "")
                data = await self._graphql(
                    _SEARCH_ISSUES_QUERY,
                    {"query": query_text, "limit": q.limit, "cursor": q.cursor},
                )
                search_issues = data.get("searchIssues", {})
                nodes = search_issues.get("nodes", [])
                page_info = search_issues.get("pageInfo", {})
                next_cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
                return ConnectorResult(records=nodes, next_cursor=next_cursor, total=len(nodes))
            case "issue_comments":
                issue_id = q.filters.get("issueId")
                if not issue_id:
                    raise ValueError("Linear issue_comments query requires 'issueId' filter")
                data = await self._graphql(_ISSUE_COMMENTS_QUERY, {"issueId": issue_id})
                issue = data.get("issue", {})
                comments = issue.get("comments", {}).get("nodes", [])
                return ConnectorResult(records=comments, total=len(comments))
            case "teams":
                data = await self._graphql(_TEAMS_QUERY)
                teams = data.get("teams", {}).get("nodes", [])
                return ConnectorResult(records=teams, total=len(teams))
            case "team_projects":
                team_id = q.filters.get("teamId")
                if not team_id:
                    raise ValueError("Linear team_projects query requires 'teamId' filter")
                data = await self._graphql(_TEAM_PROJECTS_QUERY, {"teamId": team_id})
                team = data.get("team", {})
                projects = team.get("projects", {}).get("nodes", [])
                return ConnectorResult(records=projects, total=len(projects))
            case "team_states":
                team_id = q.filters.get("teamId")
                if not team_id:
                    raise ValueError("Linear team_states query requires 'teamId' filter")
                data = await self._graphql(_TEAM_STATES_QUERY, {"teamId": team_id})
                team = data.get("team", {})
                states = team.get("states", {}).get("nodes", [])
                return ConnectorResult(records=states, total=len(states))
            case "team_labels":
                team_id = q.filters.get("teamId")
                if not team_id:
                    raise ValueError("Linear team_labels query requires 'teamId' filter")
                data = await self._graphql(_TEAM_LABELS_QUERY, {"teamId": team_id})
                team = data.get("team", {})
                labels = team.get("labels", {}).get("nodes", [])
                return ConnectorResult(records=labels, total=len(labels))
            case "team_cycles":
                team_id = q.filters.get("teamId")
                if not team_id:
                    raise ValueError("Linear team_cycles query requires 'teamId' filter")
                data = await self._graphql(_TEAM_CYCLES_QUERY, {"teamId": team_id})
                team = data.get("team", {})
                cycles = team.get("cycles", {}).get("nodes", [])
                return ConnectorResult(records=cycles, total=len(cycles))
            case _:
                raise ValueError(f"Unsupported Linear resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "issue":
                data = await self._graphql(
                    _CREATE_ISSUE_MUTATION,
                    {"input": payload.data},
                )
                result = data.get("issueCreate", {})
                if not result.get("success"):
                    title = payload.data.get("title", "")
                    raise ValueError(f"Failed to create Linear issue: {title}")
                issue: dict[str, Any] = result.get("issue", {})
                return issue
            case "issue_update":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in update payload")
                input_data = {k: v for k, v in payload.data.items() if k != "id"}
                data = await self._graphql(
                    _UPDATE_ISSUE_MUTATION,
                    {"id": issue_id, "input": input_data},
                )
                result = data.get("issueUpdate", {})
                if not result.get("success"):
                    raise ValueError(f"Failed to update Linear issue: {issue_id}")
                updated: dict[str, Any] = result.get("issue", {})
                return updated
            case "issue_comment":
                data = await self._graphql(
                    _CREATE_COMMENT_MUTATION,
                    {"input": payload.data},
                )
                result = data.get("commentCreate", {})
                if not result.get("success"):
                    raise ValueError("Failed to create Linear issue comment")
                comment: dict[str, Any] = result.get("comment", {})
                return comment
            case _:
                raise ValueError(f"Unsupported Linear write resource: {payload.resource!r}")

"""LinearConnector — async Linear GraphQL API connector."""

import asyncio
import json
import random
from typing import Any, cast

import httpx

from modulo.connectors._retry_headers import parse_retry_after as _parse_retry_after
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
  labels { nodes { id name color } }
  cycle { id name }
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

_LABEL_FIELDS = """
  id
  name
  color
  description
"""

_CREATE_LABEL_MUTATION = f"""
mutation($input: LabelCreateInput!) {{
  labelCreate(input: $input) {{
    success
    label {{
      {_LABEL_FIELDS}
    }}
  }}
}}
"""

_UPDATE_LABEL_MUTATION = f"""
mutation($id: String!, $input: LabelUpdateInput!) {{
  labelUpdate(id: $id, input: $input) {{
    success
    label {{
      {_LABEL_FIELDS}
    }}
  }}
}}
"""

_DELETE_LABEL_MUTATION = """
mutation($id: String!) {
  labelDelete(id: $id) {
    success
  }
}
"""

_CYCLE_LOOKUP_QUERY = """
query($teamId: String!, $cursor: String) {
  team(id: $teamId) {
    cycles(first: 100, after: $cursor) {
      nodes {
        id
        name
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

_STATE_LOOKUP_QUERY = """
query($teamId: String!, $cursor: String) {
  team(id: $teamId) {
    states(first: 100, after: $cursor) {
      nodes {
        id
        name
        type
        position
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

_USERS_QUERY = """
query($filter: UserFilter) {
  users(first: 1, filter: $filter) {
    nodes {
      id
      name
      email
    }
  }
}
"""

_ARCHIVE_ISSUE_MUTATION = """
mutation($id: String!, $trash: Boolean) {
  issueArchive(id: $id, trash: $trash) {
    success
  }
}
"""

_DELETE_ISSUE_MUTATION = """
mutation($id: String!) {
  issueDelete(id: $id) {
    success
  }
}
"""

_ISSUE_LABEL_IDS_QUERY = """
query($id: String!) {
  issue(id: $id) {
    id
    labels {
      nodes {
        id
      }
    }
  }
}
"""

_LABEL_CREATE_INPUT_KEYS = ("name", "teamId", "color", "description", "parentId")


class LinearError(ValueError):
    """Base class for all Linear connector errors.

    Carries a machine-parseable ``error_code`` so callers can branch on
    failure modes (invalid API key, missing permission, rate limit, network)
    without parsing human-readable messages. ``status_code`` holds the Linear
    HTTP status when the error originated from an HTTP response (``None`` for
    transport-level failures, GraphQL body errors, and local validation).
    """

    error_code = "linear_error"

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


class LinearAPIError(LinearError):
    """Raised when Linear returns a non-retryable business-level error."""

    error_code = "api_error"


class LinearRateLimitError(LinearAPIError):
    """Raised when Linear rate-limits the request and automatic retries are exhausted."""

    error_code = "rate_limited"


class LinearAuthError(LinearAPIError):
    """Raised when the API key is invalid/expired (HTTP 401 or GraphQL
    ``AUTHENTICATION_REQUIRED``) or lacks the required permission (HTTP 403 or
    GraphQL ``FORBIDDEN``).

    The ``error_code`` distinguishes the two failure modes:
    ``invalid_token`` (bad credentials) vs ``forbidden`` (the key is valid but
    lacks the required permission).
    """

    error_code = "authentication_failed"


class LinearNotFoundError(LinearAPIError):
    """Raised when Linear reports a resource does not exist (HTTP 404)."""

    error_code = "not_found"


class LinearNetworkError(LinearError):
    """Raised on transport-level failures (timeout, connection, protocol)."""

    error_code = "network_error"


def _error_for_status(status_code: int, detail: str) -> LinearError:
    """Map a Linear HTTP status to the matching structured error type."""
    if status_code == 429:
        return LinearRateLimitError(detail, status_code=429)
    if status_code == 401:
        return LinearAuthError(detail, status_code=401, error_code="invalid_token")
    if status_code == 403:
        return LinearAuthError(detail, status_code=403, error_code="forbidden")
    if status_code == 404:
        return LinearNotFoundError(detail, status_code=404)
    return LinearAPIError(detail, status_code=status_code)


def _graphql_error_types(errors: Any) -> list[str]:
    """Extract Linear GraphQL ``extensions.type`` values from an ``errors`` array."""
    if not isinstance(errors, list):
        return []
    types: list[str] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        error_type = extensions.get("type") if isinstance(extensions, dict) else None
        if isinstance(error_type, str):
            types.append(error_type)
    return types


def _classify_graphql_error(errors: Any) -> LinearError:
    """Classify a Linear GraphQL ``errors`` array into a typed error.

    Linear reports auth failures via HTTP status and GraphQL body errors such
    as ``AUTHENTICATION_REQUIRED`` (invalid/missing key) and ``FORBIDDEN``
    (valid key without the required permission). Unknown error types fall back
    to a generic ``LinearAPIError`` so callers can keep branching on the code.
    """
    types = _graphql_error_types(errors)
    detail = f"Linear API error: {errors}"
    if "AUTHENTICATION_REQUIRED" in types:
        return LinearAuthError(detail, error_code="invalid_token")
    if "FORBIDDEN" in types:
        return LinearAuthError(detail, error_code="forbidden")
    return LinearAPIError(detail)


def _compute_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Compute retry delay with exponential backoff, jitter, and optional Retry-After."""
    if response:
        retry_after = _parse_retry_after(response)
        if retry_after is not None:
            return float(min(retry_after, _MAX_DELAY))
    jitter = random.uniform(0, 1)  # noqa: S311 — non-crypto jitter for retry backoff
    return float(min(_BASE_DELAY * (2**attempt) + jitter, _MAX_DELAY))


def _normalize_name(name: str) -> str:
    """Normalise a name for case-insensitive, punctuation-insensitive comparison."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in name.lower()).split())


def _fuzzy_matches(name: str, query: str) -> bool:
    """True when every normalised query token appears inside the candidate name.

    Used as the fuzzy fallback after an exact (normalised) match fails, so
    e.g. "progress" resolves to "In Progress" but never overrides an exact hit.
    """
    query_tokens = set(_normalize_name(query).split())
    if not query_tokens:
        return False
    name_tokens = set(_normalize_name(name).split())
    return query_tokens.issubset(name_tokens)


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
      "issue_state"     — transition an issue's workflow state; data: {"id", "state": "<name>"} + required
                          {"teamId"} when using a state name (resolved via team_states), or
                          {"id", "stateId": "<id>"} to set a raw workflow state ID directly; name
                          resolution is exact-first then fuzzy, raising on ambiguous/duplicate names
      "issue_cycle"     — assign an issue to a cycle; data: {"id", "cycleId": "<id>"} to assign,
                          {"id", "cycle": "<name>", "teamId"} to resolve a cycle name via
                          team_cycles, or {"id", "cycleId": null} to remove the cycle; name
                          resolution is exact-first then fuzzy, raising on ambiguous/duplicate names
      "issue_assign"    — assign/reassign an issue; data: {"id", "assigneeId": "<id>"} (direct),
                          {"id", "email": "..."} or {"id", "name": "..."} (resolved via Linear user
                          search), or {"id", "assigneeId": null} / {"id", "unassign": true} to clear
                          the assignee; when both "assigneeId" and "unassign": true are supplied,
                          "assigneeId" wins (checked first, including an explicit null)
      "issue_unassign"  — clear the assignee on an issue; data: {"id": "..."}
      "issue_label"     — add/remove labels on an issue; data: {"id": "...",
                          "addLabelIds": ["<label-id>", ...]} and/or {"id": "...",
                          "removeLabelIds": ["<label-id>", ...]}; at least one of the two is
                          required; applied atomically via a single issueUpdate (current labels
                          are fetched first so the result is a true add/remove, not a replace)
      "issue_archive"   — archive an issue; data: {"id": "..."} with optional {"trash": bool}
      "issue_delete"    — permanently delete an issue; data: {"id": "..."}
      "label"           — create a label; data: {"name": "...", "teamId": "...", ...}
      "label_update"    — update a label; data: {"id": "...", "name": "...", ...}
      "label_delete"    — delete a label; data: {"id": "..."}
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
                        raise LinearAPIError(
                            "Linear API returned 304 Not Modified — resource unchanged",
                            error_code="not_modified",
                        )
                    if r.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                        delay = _compute_delay(attempt, r)
                        await asyncio.sleep(delay)
                        continue
                    r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                    delay = _compute_delay(attempt, exc.response)
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
                detail = f"Linear API HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                raise _error_for_status(exc.response.status_code, detail) from exc
            except httpx.TimeoutException as exc:
                if attempt < _MAX_RETRIES:
                    delay = _compute_delay(attempt)
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
                raise LinearNetworkError("Linear API timeout", error_code="network_timeout") from exc
            except httpx.ConnectError as exc:
                if attempt < _MAX_RETRIES:
                    delay = _compute_delay(attempt)
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
                raise LinearNetworkError("Linear API connection error", error_code="network_connection") from exc
            except httpx.ProtocolError as exc:
                if attempt < _MAX_RETRIES:
                    delay = _compute_delay(attempt)
                    await asyncio.sleep(delay)
                    last_exc = exc
                    continue
                raise LinearNetworkError("Linear API protocol error", error_code="network_protocol") from exc
            try:
                body: dict[str, Any] = r.json()
            except json.JSONDecodeError as exc:
                raise LinearAPIError(f"Linear API invalid response: {exc}", error_code="invalid_response") from exc
            if "errors" in body:
                raise _classify_graphql_error(body["errors"])
            raw_data = body.get("data")
            if raw_data is None:
                return {}
            if not isinstance(raw_data, dict):
                raise LinearAPIError(
                    "Linear API response 'data' must be an object",
                    error_code="invalid_response",
                )
            return cast("dict[str, Any]", raw_data)
        raise LinearError("Linear API request failed after retries", error_code="retries_exhausted") from last_exc

    async def _team_states(self, team_id: str) -> list[dict[str, Any]]:
        """Fetch all workflow states for a team, paginating through every page."""
        return await self._team_named_entities(team_id, _STATE_LOOKUP_QUERY, "states")

    async def _team_cycles(self, team_id: str) -> list[dict[str, Any]]:
        """Fetch all cycles for a team, paginating through every page."""
        return await self._team_named_entities(team_id, _CYCLE_LOOKUP_QUERY, "cycles")

    async def _team_named_entities(
        self,
        team_id: str,
        query: str,
        field: str,
    ) -> list[dict[str, Any]]:
        """Paginate a team's ``field`` connection (``states``/``cycles``) into a flat list."""
        cursor: str | None = None
        entities: list[dict[str, Any]] = []
        while True:
            data = await self._graphql(query, {"teamId": team_id, "cursor": cursor})
            team = data.get("team")
            if team is None:
                raise ValueError(f"Linear team {team_id!r} not found")
            connection = team.get(field, {})
            entities.extend(node for node in connection.get("nodes", []) if isinstance(node, dict))
            page_info = connection.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        return entities

    def _resolve_entity_by_name(
        self,
        entities: list[dict[str, Any]],
        name: str,
        entity: str,
        team_id: str,
    ) -> str:
        """Resolve a state/cycle name to an ID — exact match first, then fuzzy.

        Raises a clear ``ValueError`` when the name is ambiguous (two or more
        entities match) so the caller can pass the raw ID instead, and when no
        entity matches at all.
        """
        named = [e for e in entities if e.get("id") and e.get("name")]
        norm = _normalize_name(name)
        exact = [e for e in named if _normalize_name(e["name"]) == norm]
        if len(exact) > 1:
            raise ValueError(
                f"Multiple Linear {entity}s named {name!r} for team {team_id}; pass the {entity}Id directly",
            )
        if exact:
            return cast("str", exact[0]["id"])
        fuzzy = [e for e in named if _fuzzy_matches(e["name"], name)]
        if len(fuzzy) > 1:
            raise ValueError(
                f"Multiple Linear {entity}s match {name!r} for team {team_id}; pass the {entity}Id directly",
            )
        if fuzzy:
            return cast("str", fuzzy[0]["id"])
        raise ValueError(f"Linear {entity} {name!r} not found for team {team_id}")

    async def _resolve_state_id(self, team_id: str, state_name: str) -> str:
        """Resolve a workflow state name to its ID via the team's states."""
        states = await self._team_states(team_id)
        return self._resolve_entity_by_name(states, state_name, "workflow state", team_id)

    async def _resolve_cycle_id(self, team_id: str, cycle_name: str) -> str:
        """Resolve a cycle name to its ID via the team's cycles, paginating all pages."""
        cycles = await self._team_cycles(team_id)
        return self._resolve_entity_by_name(cycles, cycle_name, "cycle", team_id)

    async def _resolve_user_id(self, *, email: str | None = None, name: str | None = None) -> str:
        """Resolve a Linear user to an ID by email or display name.

        Uses Linear's server-side exact ``eq`` filter (``users(first: 1)``) rather than the
        client-side exact-then-fuzzy/ambiguity resolution used for states and cycles; a name
        matching multiple users returns the first hit without raising (asymmetry with
        ``_resolve_entity_by_name``, acceptable because the server limits to one result).
        """
        if email:
            user_filter: dict[str, Any] = {"email": {"eq": email}}
            label = f"user with email {email!r}"
        elif name:
            user_filter = {"name": {"eq": name}}
            label = f"user named {name!r}"
        else:
            raise ValueError("issue_assign requires 'assigneeId', 'email', 'name', or 'unassign': true")
        data = await self._graphql(_USERS_QUERY, {"filter": user_filter})
        users = data.get("users", {}).get("nodes", [])
        for user in users:
            user_id = user.get("id")
            if user_id:
                return cast("str", user_id)
        raise ValueError(f"Linear {label} not found")

    async def health_check(self) -> HealthResult:
        try:
            data = await self._graphql(_VIEWER_QUERY)
            viewer = data.get("viewer", {})
            if not viewer:
                return HealthResult(ok=False, detail="No viewer returned — invalid API key?")
            name = viewer.get("name") or viewer.get("email") or viewer.get("id", "")
            return HealthResult(ok=True, detail=name)
        except asyncio.CancelledError:
            raise
        except LinearAuthError as exc:
            if exc.error_code == "invalid_token":
                status = f" (HTTP {exc.status_code})" if exc.status_code else ""
                return HealthResult(
                    ok=False,
                    detail=f"Linear authentication failed — invalid or expired API key{status} (code: invalid_token)",
                )
            return HealthResult(
                ok=False,
                detail="Linear permission denied — API key lacks the required permission (code: forbidden)",
            )
        except LinearRateLimitError as exc:
            status = f" (HTTP {exc.status_code})" if exc.status_code else ""
            return HealthResult(ok=False, detail=f"Linear API rate limit exhausted{status} (code: rate_limited)")
        except LinearNetworkError as exc:
            return HealthResult(ok=False, detail=f"Linear API network error (code: {exc.error_code}): {exc}")
        except LinearError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "issue":
                if "id" not in q.filters:
                    raise ValueError("Linear issue query requires 'id' filter")
                issue_id = q.filters["id"]
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
            case "issue_state":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in issue_state payload")
                state_id = payload.data.get("stateId")
                if not state_id:
                    state_name = payload.data.get("state")
                    team_id = payload.data.get("teamId")
                    if not state_name or not team_id:
                        raise ValueError(
                            "issue_state requires 'stateId' or both 'state' (name) and 'teamId'",
                        )
                    state_id = await self._resolve_state_id(team_id, state_name)
                return await self._update_issue(issue_id, {"stateId": state_id})
            case "issue_cycle":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in issue_cycle payload")
                if "cycleId" in payload.data:
                    cycle_id = payload.data.get("cycleId")
                else:
                    cycle_name = payload.data.get("cycle")
                    team_id = payload.data.get("teamId")
                    if not cycle_name or not team_id:
                        raise ValueError(
                            "issue_cycle requires 'cycleId' or both 'cycle' (name) and 'teamId'",
                        )
                    cycle_id = await self._resolve_cycle_id(team_id, cycle_name)
                return await self._update_issue(issue_id, {"cycleId": cycle_id})
            case "issue_label":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in issue_label payload")
                add_ids = payload.data.get("addLabelIds") or []
                remove_ids = payload.data.get("removeLabelIds") or []
                if not add_ids and not remove_ids:
                    raise ValueError("issue_label requires 'addLabelIds' and/or 'removeLabelIds'")
                return await self._update_issue_labels(issue_id, add_ids, remove_ids)
            case "issue_assign":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in issue_assign payload")
                if "assigneeId" in payload.data:
                    assignee_id = payload.data.get("assigneeId")
                elif payload.data.get("unassign") is True:
                    assignee_id = None
                else:
                    email = payload.data.get("email")
                    name = payload.data.get("name")
                    assignee_id = await self._resolve_user_id(email=email, name=name)
                return await self._update_issue(issue_id, {"assigneeId": assignee_id})
            case "issue_unassign":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in issue_unassign payload")
                return await self._update_issue(issue_id, {"assigneeId": None})
            case "issue_archive":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in issue_archive payload")
                trash = payload.data.get("trash", False)
                data = await self._graphql(
                    _ARCHIVE_ISSUE_MUTATION,
                    {"id": issue_id, "trash": trash},
                )
                result = data.get("issueArchive", {})
                if not result.get("success"):
                    raise ValueError(f"Failed to archive Linear issue: {issue_id}")
                return {"id": issue_id, "archived": True, "trash": bool(trash)}
            case "issue_delete":
                issue_id = payload.data.get("id")
                if not issue_id:
                    raise ValueError("Missing 'id' in issue_delete payload")
                data = await self._graphql(_DELETE_ISSUE_MUTATION, {"id": issue_id})
                result = data.get("issueDelete", {})
                if not result.get("success"):
                    raise ValueError(f"Failed to delete Linear issue: {issue_id}")
                return {"id": issue_id, "deleted": True}
            case "label":
                name = payload.data.get("name")
                team_id = payload.data.get("teamId")
                if not name or not team_id:
                    raise ValueError("label write requires 'name' and 'teamId'")
                input_data = {k: v for k, v in payload.data.items() if k in _LABEL_CREATE_INPUT_KEYS}
                data = await self._graphql(
                    _CREATE_LABEL_MUTATION,
                    {"input": input_data},
                )
                result = data.get("labelCreate", {})
                if not result.get("success"):
                    raise ValueError(f"Failed to create Linear label: {name}")
                created: dict[str, Any] = result.get("label", {})
                return created
            case "label_update":
                label_id = payload.data.get("id")
                if not label_id:
                    raise ValueError("Missing 'id' in label_update payload")
                input_data = {k: v for k, v in payload.data.items() if k != "id"}
                data = await self._graphql(
                    _UPDATE_LABEL_MUTATION,
                    {"id": label_id, "input": input_data},
                )
                result = data.get("labelUpdate", {})
                if not result.get("success"):
                    raise ValueError(f"Failed to update Linear label: {label_id}")
                updated_label: dict[str, Any] = result.get("label", {})
                return updated_label
            case "label_delete":
                label_id = payload.data.get("id")
                if not label_id:
                    raise ValueError("Missing 'id' in label_delete payload")
                data = await self._graphql(_DELETE_LABEL_MUTATION, {"id": label_id})
                result = data.get("labelDelete", {})
                if not result.get("success"):
                    raise ValueError(f"Failed to delete Linear label: {label_id}")
                return {"id": label_id, "deleted": True}
            case _:
                raise ValueError(f"Unsupported Linear write resource: {payload.resource!r}")

    async def _update_issue(self, issue_id: str, update: dict[str, Any]) -> dict[str, Any]:
        """Apply an issue update mutation and return the updated issue."""
        data = await self._graphql(_UPDATE_ISSUE_MUTATION, {"id": issue_id, "input": update})
        result = data.get("issueUpdate", {})
        if not result.get("success"):
            raise ValueError(f"Failed to update Linear issue: {issue_id}")
        return cast("dict[str, Any]", result.get("issue", {}))

    async def _current_label_ids(self, issue_id: str) -> list[str]:
        """Fetch the IDs of the labels currently applied to an issue."""
        data = await self._graphql(_ISSUE_LABEL_IDS_QUERY, {"id": issue_id})
        issue = data.get("issue")
        if issue is None:
            raise ValueError(f"Linear issue {issue_id!r} not found")
        labels = issue.get("labels", {}).get("nodes", [])
        return [label.get("id") for label in labels if label.get("id")]

    async def _update_issue_labels(
        self,
        issue_id: str,
        add_ids: list[str],
        remove_ids: list[str],
    ) -> dict[str, Any]:
        """Add/remove labels on an issue atomically via a single issueUpdate.

        The Linear issueUpdate ``labelIds`` field is a *set* — it replaces the full
        label list. To make ``issue_label`` a true add/remove (not a replace), the
        current label IDs are fetched first and the target set computed from them.
        """
        current = await self._current_label_ids(issue_id)
        remove_set = frozenset(remove_ids)
        target = [label_id for label_id in current if label_id not in remove_set]
        for label_id in add_ids:
            if label_id not in target:
                target.append(label_id)
        return await self._update_issue(issue_id, {"labelIds": target})

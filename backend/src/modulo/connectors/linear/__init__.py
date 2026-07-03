"""LinearConnector — async Linear GraphQL API connector."""

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
query($query: String!, $limit: Int) {{
  searchIssues(query: $query, first: $limit) {{
    nodes {{
      {_ISSUE_FIELDS}
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


class LinearConnector(ConnectorBase):
    """Read/write Linear issues via the GraphQL API.

    Credentials (from credentials_ciphertext):
      "api_key"  — Linear personal API key

    Supported query resources:
      "issue"     — get a single issue by ID; filters: {"id": "uuid"}
      "search"    — search issues by text; filters: {"query": "..."}

    Supported write resources:
      "issue"     — create an issue; data: {"title": "...", "teamId": "...", ...}
      "issue_update" — update an issue; data: {"id": "...", "title": "...", ...}
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
        async with self._client() as client:
            try:
                r = await client.post(
                    "/graphql",
                    json={"query": query, "variables": variables or {}},
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"Linear API HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                raise ValueError(f"Linear API connection error: {exc}") from exc
            try:
                body: dict[str, Any] = r.json()
            except Exception as exc:
                raise ValueError(f"Linear API invalid response: {exc}") from exc
            if "errors" in body:
                raise ValueError(f"Linear API error: {body['errors']}")
            data: dict[str, Any] = body.get("data") or {}
            return data

    async def health_check(self) -> HealthResult:
        """Verify API connectivity by fetching the authenticated user."""
        try:
            data = await self._graphql(_VIEWER_QUERY)
            viewer = data.get("viewer", {})
            if not viewer:
                return HealthResult(ok=False, detail="No viewer returned — invalid API key?")
            name = viewer.get("name") or viewer.get("email") or viewer.get("id", "")
            return HealthResult(ok=True, detail=name)
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
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
                data = await self._graphql(_SEARCH_ISSUES_QUERY, {"query": query_text, "limit": q.limit})
                nodes = data.get("searchIssues", {}).get("nodes", [])
                return ConnectorResult(records=nodes, total=len(nodes))
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
            case _:
                raise ValueError(f"Unsupported Linear write resource: {payload.resource!r}")

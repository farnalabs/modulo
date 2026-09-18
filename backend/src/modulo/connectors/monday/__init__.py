"""MondayConnector — async Monday.com GraphQL API v2 connector."""

import asyncio
from typing import Any

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)
from modulo.core.ssrf import pinned_async_client_sync

_MONDAY_API = "https://api.monday.com/v2"

_ME_QUERY = """
query {
  me {
    name
  }
}
"""

_BOARDS_QUERY = """
query($limit: Int) {
  boards(limit: $limit) {
    id
    name
  }
}
"""

_BOARD_QUERY = """
query($board_id: [Int]) {
  boards(ids: $board_id) {
    id
    name
    columns {
      id
      title
      type
    }
    groups {
      id
      title
    }
  }
}
"""

_ITEMS_QUERY = """
query($board_id: [Int]) {
  boards(ids: $board_id) {
    items {
      id
      name
      column_values {
        id
        text
      }
    }
  }
}
"""

_ITEM_QUERY = """
query($item_id: [Int]) {
  items(ids: $item_id) {
    id
    name
    column_values {
      id
      text
    }
  }
}
"""

_USERS_QUERY = """
query {
  users {
    id
    name
    email
  }
}
"""

_WORKSPACES_QUERY = """
query {
  workspaces {
    id
    name
  }
}
"""

_CREATE_ITEM_MUTATION = """
mutation($board_id: Int!, $item_name: String!, $column_values: JSON) {
  create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) {
    id
    name
  }
}
"""

_CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION = """
mutation($item_id: Int!, $column_values: JSON!) {
  change_multiple_column_values(item_id: $item_id, column_values: $column_values) {
    id
    name
  }
}
"""

_CHANGE_SIMPLE_COLUMN_VALUE_MUTATION = """
mutation($item_id: Int!, $column_id: String!, $value: JSON!) {
  change_simple_column_value(item_id: $item_id, column_id: $column_id, value: $value) {
    id
    name
  }
}
"""

_CREATE_UPDATE_MUTATION = """
mutation($item_id: Int!, $body: String!) {
  create_update(item_id: $item_id, body: $body) {
    id
    text
  }
}
"""


class MondayConnector(ConnectorBase):
    """Read/write Monday.com boards, items, and users via the GraphQL API v2.

    Credentials (from credentials_ciphertext):
      "api_key"  — Monday.com API key (used directly as Authorization header)

    Supported query resources:
      "boards"      — list boards; optional filter: {"limit": int}
      "board"       — get a board with columns and groups; filter: {"board_id": int}
      "items"       — list items on a board; filter: {"board_id": int}
      "item"        — get a single item; filter: {"item_id": int}
      "users"       — list all users
      "workspaces"  — list all workspaces

    Supported write resources:
      "item"            — create an item; data: {"board_id": int, "item_name": str, ...}
      "item_update"     — update item column values; data: {"item_id": int, "column_values": JSON}
      "column_value"    — change a single column value; data: {"item_id": int, "column_id": str, "value": JSON}
      "update"          — add an update to an item; data: {"item_id": int, "body": str}
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.MONDAY

    def _client(self) -> httpx.AsyncClient:
        return pinned_async_client_sync(
            _MONDAY_API,
            base_url=_MONDAY_API,
            headers={"Authorization": self._api_key},
            timeout=30,
        )

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._client() as client:
            r = await client.post(
                "/",
                json={"query": query, "variables": variables or {}},
            )
            r.raise_for_status()
            body: dict[str, Any] = r.json()
            if "errors" in body:
                raise ValueError(f"Monday.com API error: {body['errors']}")
            data: dict[str, Any] = body.get("data", {})
            return data

    async def health_check(self) -> HealthResult:
        """Verify API connectivity by fetching the authenticated user."""
        try:
            data = await self._graphql(_ME_QUERY)
            me = data.get("me", {})
            if not me:
                return HealthResult(ok=False, detail="No user returned — invalid API key?")
            name = me.get("name") or ""
            return HealthResult(ok=True, detail=name)
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "boards":
                data = await self._graphql(_BOARDS_QUERY, {"limit": q.limit})
                boards: list[dict[str, Any]] = data.get("boards", [])
                return ConnectorResult(records=boards, total=len(boards))

            case "board":
                board_id = q.filters.get("board_id")
                if board_id is None:
                    raise ValueError("Monday board query requires 'board_id' filter")
                data = await self._graphql(_BOARD_QUERY, {"board_id": [board_id]})
                boards_list = data.get("boards", [])
                return ConnectorResult(records=boards_list, total=len(boards_list))

            case "items":
                board_id = q.filters.get("board_id")
                if board_id is None:
                    raise ValueError("Monday items query requires 'board_id' filter")
                data = await self._graphql(_ITEMS_QUERY, {"board_id": [board_id]})
                boards_list = data.get("boards", [])
                items: list[dict[str, Any]] = [item for b in boards_list for item in b.get("items", [])]
                return ConnectorResult(records=items, total=len(items))

            case "item":
                item_id = q.filters.get("item_id")
                if item_id is None:
                    raise ValueError("Monday item query requires 'item_id' filter")
                data = await self._graphql(_ITEM_QUERY, {"item_id": [item_id]})
                items_list: list[dict[str, Any]] = data.get("items", [])
                return ConnectorResult(records=items_list, total=len(items_list))

            case "users":
                data = await self._graphql(_USERS_QUERY)
                users: list[dict[str, Any]] = data.get("users", [])
                return ConnectorResult(records=users, total=len(users))

            case "workspaces":
                data = await self._graphql(_WORKSPACES_QUERY)
                workspaces: list[dict[str, Any]] = data.get("workspaces", [])
                return ConnectorResult(records=workspaces, total=len(workspaces))

            case _:
                raise ValueError(f"Unsupported Monday.com resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "item":
                board_id = payload.data.get("board_id")
                item_name = payload.data.get("item_name")
                if not board_id or not item_name:
                    raise ValueError("Monday item write requires 'board_id' and 'item_name' in data")
                column_values = payload.data.get("column_values")
                kwargs: dict[str, Any] = {
                    "board_id": board_id,
                    "item_name": item_name,
                }
                if column_values is not None:
                    kwargs["column_values"] = column_values
                data = await self._graphql(_CREATE_ITEM_MUTATION, kwargs)
                item: dict[str, Any] = data.get("create_item", {})
                return item

            case "item_update":
                item_id = payload.data.get("item_id")
                column_values = payload.data.get("column_values")
                if item_id is None or column_values is None:
                    raise ValueError("Monday item_update requires 'item_id' and 'column_values' in data")
                data = await self._graphql(
                    _CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
                    {"item_id": item_id, "column_values": column_values},
                )
                updated: dict[str, Any] = data.get("change_multiple_column_values", {})
                return updated

            case "column_value":
                item_id = payload.data.get("item_id")
                column_id = payload.data.get("column_id")
                value = payload.data.get("value")
                if item_id is None or column_id is None or value is None:
                    raise ValueError("Monday column_value write requires 'item_id', 'column_id', and 'value' in data")
                data = await self._graphql(
                    _CHANGE_SIMPLE_COLUMN_VALUE_MUTATION,
                    {"item_id": item_id, "column_id": column_id, "value": value},
                )
                simple_updated: dict[str, Any] = data.get("change_simple_column_value", {})
                return simple_updated

            case "update":
                item_id = payload.data.get("item_id")
                body = payload.data.get("body")
                if item_id is None or not body:
                    raise ValueError("Monday update requires 'item_id' and 'body' in data")
                data = await self._graphql(
                    _CREATE_UPDATE_MUTATION,
                    {"item_id": item_id, "body": body},
                )
                update: dict[str, Any] = data.get("create_update", {})
                return update

            case _:
                raise ValueError(f"Unsupported Monday.com write resource: {payload.resource!r}")

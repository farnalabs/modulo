"""TrelloConnector — async Trello REST API v1 connector."""

from collections.abc import Awaitable, Callable
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

_TRELLO_API = "https://api.trello.com/1"


class TrelloConnector(ConnectorBase):
    """Read/write Trello boards, lists, and cards via the REST API v1.

    Credentials (from credentials_ciphertext):
      "api_key"  — Trello API key
      "token"    — Trello API token

    Supported query resources:
      "boards"   — list boards for the authenticated user
      "lists"    — list lists on a board; filters: {"board_id": "..."}
      "cards"    — list cards on a board or list; filters: {"board_id": "..."} or {"list_id": "..."}
      "card"     — get a single card; filters: {"card_id": "..."}
      "members"  — list members on a board; filters: {"board_id": "..."}

    Supported write resources:
      "card"          — create a card; data: {"name": "...", "idList": "...", ...}
      "card_update"   — update a card; data: {"id": "...", ...}
      "comment"       — add a comment to a card; data: {"card_id": "...", "text": "..."}
    """

    def __init__(self, api_key: str, token: str) -> None:
        self._api_key = api_key
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.TRELLO

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_TRELLO_API,
            params={"key": self._api_key, "token": self._token},
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the authenticated user's profile."""
        async with self._client() as client:
            r = await client.get("/members/me")

        if r.status_code != 200:
            return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

        body: dict[str, Any] = r.json()
        if "id" not in body:
            return HealthResult(ok=False, detail="Unexpected response — no 'id' in member profile")

        display_name = body.get("fullName") or body.get("username") or ""
        return HealthResult(ok=True, detail=display_name)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            handler = self._query_handlers().get(q.resource)
            if handler is None:
                raise ValueError(f"Unsupported Trello resource: {q.resource!r}")
            return await handler(client, q)

    def _query_handlers(
        self,
    ) -> dict[str, Callable[[httpx.AsyncClient, ConnectorQuery], Awaitable[ConnectorResult]]]:
        return {
            "boards": self._query_boards,
            "lists": self._query_lists,
            "cards": self._query_cards,
            "card": self._query_card,
            "members": self._query_members,
        }

    async def _query_boards(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, str] = {}
        params.update(self._filter_param(q))
        params.update(self._fields_param(q))
        r = await client.get("/members/me/boards", params=params)
        r.raise_for_status()
        boards: list[dict[str, Any]] = r.json()
        return ConnectorResult(records=boards, total=len(boards))

    async def _query_lists(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        if "board_id" not in q.filters:
            raise ValueError("Trello lists query requires 'board_id' filter")
        board_id = q.filters["board_id"]
        params = self._filter_param(q)
        r = await client.get(f"/boards/{board_id}/lists", params=params)
        r.raise_for_status()
        lists: list[dict[str, Any]] = r.json()
        return ConnectorResult(records=lists, total=len(lists))

    async def _query_cards(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params = self._fields_param(q)
        r = await client.get(self._cards_endpoint(q), params=params)
        r.raise_for_status()
        cards: list[dict[str, Any]] = r.json()
        return ConnectorResult(records=cards, total=len(cards))

    async def _query_card(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        if "card_id" not in q.filters:
            raise ValueError("Trello card query requires 'card_id' filter")
        card_id = q.filters["card_id"]
        r = await client.get(f"/cards/{card_id}")
        r.raise_for_status()
        card: dict[str, Any] = r.json()
        return ConnectorResult(records=[card])

    async def _query_members(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        if "board_id" not in q.filters:
            raise ValueError("Trello members query requires 'board_id' filter")
        board_id = q.filters["board_id"]
        r = await client.get(f"/boards/{board_id}/members")
        r.raise_for_status()
        members: list[dict[str, Any]] = r.json()
        return ConnectorResult(records=members, total=len(members))

    @staticmethod
    def _cards_endpoint(q: ConnectorQuery) -> str:
        board_id = q.filters.get("board_id")
        list_id = q.filters.get("list_id")
        if board_id:
            return f"/boards/{board_id}/cards"
        if list_id:
            return f"/lists/{list_id}/cards"
        raise ValueError("Trello cards query requires 'board_id' or 'list_id' filter")

    @staticmethod
    def _filter_param(q: ConnectorQuery) -> dict[str, str]:
        if "filter" in q.filters:
            return {"filter": q.filters["filter"]}
        return {}

    @staticmethod
    def _fields_param(q: ConnectorQuery) -> dict[str, str]:
        if "fields" in q.filters:
            return {"fields": q.filters["fields"]}
        return {}

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "card":
                    r = await client.post("/cards", json=payload.data)
                    r.raise_for_status()
                    created: dict[str, Any] = r.json()
                    return created

                case "card_update":
                    if "id" not in payload.data:
                        raise ValueError("Trello card_update requires 'id' in data")
                    card_id = payload.data["id"]
                    r = await client.put(f"/cards/{card_id}", json=payload.data)
                    r.raise_for_status()
                    updated: dict[str, Any] = r.json()
                    return updated

                case "comment":
                    if "card_id" not in payload.data:
                        raise ValueError("Trello comment requires 'card_id' in data")
                    card_id = payload.data["card_id"]
                    text = payload.data.get("text", "")
                    r = await client.post(f"/cards/{card_id}/actions/comments", json={"text": text})
                    r.raise_for_status()
                    action: dict[str, Any] = r.json()
                    return action

                case _:
                    raise ValueError(f"Unsupported Trello write resource: {payload.resource!r}")

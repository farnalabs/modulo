"""TrelloConnector — async Trello REST API v1 connector."""

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
            match q.resource:
                case "boards":
                    params: dict[str, str] = {}
                    if "filter" in q.filters:
                        params["filter"] = q.filters["filter"]
                    if "fields" in q.filters:
                        params["fields"] = q.filters["fields"]
                    r = await client.get("/members/me/boards", params=params)
                    r.raise_for_status()
                    boards: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=boards, total=len(boards))

                case "lists":
                    board_id = q.filters.get("board_id")
                    if not board_id:
                        raise ValueError("Trello lists query requires 'board_id' filter")
                    params = {}
                    if "filter" in q.filters:
                        params["filter"] = q.filters["filter"]
                    r = await client.get(f"/boards/{board_id}/lists", params=params)
                    r.raise_for_status()
                    lists: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=lists, total=len(lists))

                case "cards":
                    params = {}
                    if "fields" in q.filters:
                        params["fields"] = q.filters["fields"]
                    board_id = q.filters.get("board_id")
                    list_id = q.filters.get("list_id")
                    if board_id:
                        r = await client.get(f"/boards/{board_id}/cards", params=params)
                    elif list_id:
                        r = await client.get(f"/lists/{list_id}/cards", params=params)
                    else:
                        raise ValueError("Trello cards query requires 'board_id' or 'list_id' filter")
                    r.raise_for_status()
                    cards: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=cards, total=len(cards))

                case "card":
                    card_id = q.filters.get("card_id")
                    if not card_id:
                        raise ValueError("Trello card query requires 'card_id' filter")
                    r = await client.get(f"/cards/{card_id}")
                    r.raise_for_status()
                    card: dict[str, Any] = r.json()
                    return ConnectorResult(records=[card])

                case "members":
                    board_id = q.filters.get("board_id")
                    if not board_id:
                        raise ValueError("Trello members query requires 'board_id' filter")
                    r = await client.get(f"/boards/{board_id}/members")
                    r.raise_for_status()
                    members: list[dict[str, Any]] = r.json()
                    return ConnectorResult(records=members, total=len(members))

                case _:
                    raise ValueError(f"Unsupported Trello resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "card":
                    r = await client.post("/cards", json=payload.data)
                    r.raise_for_status()
                    created: dict[str, Any] = r.json()
                    return created

                case "card_update":
                    card_id = payload.data.get("id")
                    if not card_id:
                        raise ValueError("Trello card_update requires 'id' in data")
                    r = await client.put(f"/cards/{card_id}", json=payload.data)
                    r.raise_for_status()
                    updated: dict[str, Any] = r.json()
                    return updated

                case "comment":
                    card_id = payload.data.get("card_id")
                    if not card_id:
                        raise ValueError("Trello comment requires 'card_id' in data")
                    text = payload.data.get("text", "")
                    r = await client.post(f"/cards/{card_id}/actions/comments", json={"text": text})
                    r.raise_for_status()
                    action: dict[str, Any] = r.json()
                    return action

                case _:
                    raise ValueError(f"Unsupported Trello write resource: {payload.resource!r}")

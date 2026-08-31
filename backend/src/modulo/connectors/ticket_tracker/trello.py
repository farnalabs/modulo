"""Trello implementation of the TicketTrackerBase ABC.

Trello uses a key + token authentication model, where credentials are
passed as query parameters on every request — never in the request body.
Cards are organised into lists (idList). Status is derived from the
`closed` field: a card with `closed: true` is considered "closed",
otherwise it's "open".
"""

import asyncio
import logging
from typing import Any

import httpx

from modulo.connectors._safe_datetime import safe_datetime as _safe_datetime
from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)
from modulo.connectors.security import CredentialRedactor, redacting
from modulo.connectors.ticket_tracker.base import Ticket, TicketFilter, TicketTrackerBase

logger = logging.getLogger(__name__)

TRELLO_CARD_FIELDS = "id,name,desc,dateLastActivity,closed,due,url,idList,labels"
DEFAULT_TIMEOUT = 10


class TrelloTicketTracker(TicketTrackerBase):
    def __init__(self, config: dict[str, Any], creds: dict[str, Any]) -> None:
        self._config = config
        self._creds = creds
        self._api_key = creds.get("api_key", "")
        self._token = creds.get("token", "")
        self._board_id = config.get("board_id", "")
        self._base_url = "https://api.trello.com/1"
        if not self._api_key or not self._token:
            raise ValueError("Trello connector requires api_key and token credentials")
        # Key + token are sent as QUERY parameters on every request, so httpx
        # includes them in the request URL — and a status/transport error echoes
        # that URL. Redact the credential values at the connector boundary.
        self._redactor = CredentialRedactor([self._api_key, self._token])

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.TICKET_TRACKER

    def _auth(self) -> dict[str, str]:
        return {"key": self._api_key, "token": self._token}

    async def health_check(self) -> HealthResult:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/boards/{self._board_id}",
                    params=self._auth(),
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                return HealthResult(ok=True, detail=resp.json().get("name", ""))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return health_check_failure(self._redactor.redact_exc(e))

    @redacting
    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        filters = q.filters or {}
        if "ticket_id" in filters:
            ticket = await self.get_ticket(filters["ticket_id"])
            return ConnectorResult(records=[ticket.raw], total=1)
        tickets = await self.list_tickets(
            TicketFilter(
                status=filters.get("status"),
                labels=filters.get("labels"),
                search=filters.get("search"),
                limit=filters.get("limit", 20),
                offset=filters.get("offset", 0),
            ),
        )
        return ConnectorResult(records=[t.__dict__ for t in tickets], total=len(tickets))

    @redacting
    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        data = payload.data
        ticket = await self.create_ticket(
            title=data.get("title", ""),
            description=data.get("description"),
            labels=data.get("labels"),
            idList=data.get("list_id"),
        )
        return {"ticket_id": ticket.id, "url": ticket.url or ""}

    async def list_tickets(self, ticket_filter: TicketFilter | None = None) -> list[Ticket]:
        if ticket_filter and ticket_filter.offset:
            logger.warning("offset is not supported by Trello's API; ignoring offset=%s", ticket_filter.offset)

        async with httpx.AsyncClient() as client:
            params: dict[str, Any] = self._auth()
            params["fields"] = TRELLO_CARD_FIELDS
            if ticket_filter and ticket_filter.limit:
                params["limit"] = str(min(ticket_filter.limit, 100))
            try:
                resp = await client.get(
                    f"{self._base_url}/boards/{self._board_id}/cards",
                    params=params,
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                raw_cards = resp.json()
            except httpx.HTTPStatusError as e:
                raise ValueError(
                    f"Trello API error: {e.response.status_code} - {self._redactor.redact(e.response.text)}"
                ) from None
            except httpx.RequestError as e:
                raise ValueError(self._redactor.redact(f"Trello network error: {e}")) from None

        if ticket_filter and ticket_filter.search:
            raw_cards = [
                c
                for c in raw_cards
                if ticket_filter.search.lower() in (c.get("name", "") + (c.get("desc") or "")).lower()
            ]

        tickets = [self._to_ticket(c) for c in raw_cards]

        if ticket_filter and ticket_filter.status:
            tickets = [t for t in tickets if t.status and t.status.lower() == ticket_filter.status.lower()]

        return tickets

    async def get_ticket(self, ticket_id: str) -> Ticket:
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    f"{self._base_url}/cards/{ticket_id}",
                    params={
                        **self._auth(),
                        "fields": TRELLO_CARD_FIELDS,
                    },
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                return self._to_ticket(resp.json())
            except httpx.HTTPStatusError as e:
                raise ValueError(
                    f"Trello API error: {e.response.status_code} - {self._redactor.redact(e.response.text)}"
                ) from None
            except httpx.RequestError as e:
                raise ValueError(self._redactor.redact(f"Trello network error: {e}")) from None

    async def create_ticket(self, title: str, description: str | None = None, **kwargs: Any) -> Ticket:
        if not kwargs.get("idList"):
            raise ValueError("idList is required to create a Trello card")
        body: dict[str, Any] = {"name": title}
        if description:
            body["desc"] = description
        body["idList"] = kwargs["idList"]
        if "labels" in kwargs:
            raw_labels = kwargs["labels"]
            body["labels"] = ",".join(raw_labels) if isinstance(raw_labels, list) else raw_labels
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/cards",
                    params=self._auth(),
                    data=body,
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                return self._to_ticket(resp.json())
            except httpx.HTTPStatusError as e:
                raise ValueError(
                    f"Trello API error: {e.response.status_code} - {self._redactor.redact(e.response.text)}"
                ) from None
            except httpx.RequestError as e:
                raise ValueError(self._redactor.redact(f"Trello network error: {e}")) from None

    async def update_ticket(self, ticket_id: str, **kwargs: Any) -> Ticket:
        body: dict[str, Any] = {}
        if kwargs.get("idList"):
            body["idList"] = kwargs["idList"]
        if "due" in kwargs:
            body["due"] = kwargs["due"]
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.put(
                    f"{self._base_url}/cards/{ticket_id}",
                    params=self._auth(),
                    data=body,
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                return self._to_ticket(resp.json())
            except httpx.HTTPStatusError as e:
                raise ValueError(
                    f"Trello API error: {e.response.status_code} - {self._redactor.redact(e.response.text)}"
                ) from None
            except httpx.RequestError as e:
                raise ValueError(self._redactor.redact(f"Trello network error: {e}")) from None

    def _to_ticket(self, raw: dict[str, Any]) -> Ticket:
        labels = raw.get("labels")
        return Ticket(
            id=raw.get("id", ""),
            title=raw.get("name", ""),
            description=raw.get("desc"),
            status="closed" if raw.get("closed") else "open",
            priority=None,
            ticket_type="task",
            labels=(
                [label.get("name", "") for label in labels if isinstance(label, dict)]
                if isinstance(labels, list)
                else []
            ),
            url=raw.get("url") or raw.get("shortUrl"),
            created_at=None,
            updated_at=_safe_datetime(raw.get("dateLastActivity")),
            raw=raw,
        )

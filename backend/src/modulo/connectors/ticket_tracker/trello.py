"""Trello implementation of the TicketTrackerBase ABC."""

from datetime import datetime
from typing import Any

import httpx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, ConnectorType, HealthResult
from modulo.connectors.ticket_tracker.base import Ticket, TicketFilter, TicketTrackerBase


class TrelloTicketTracker(TicketTrackerBase):
    def __init__(self, config: dict[str, Any], creds: dict[str, Any]) -> None:
        self._config = config
        self._creds = creds
        self._api_key = creds.get("api_key", "")
        self._token = creds.get("token", "")
        self._board_id = config.get("board_id", "")
        self._base_url = "https://api.trello.com/1"

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
                    timeout=10,
                )
                resp.raise_for_status()
                return HealthResult(ok=True, detail=resp.json().get("name", ""))
        except Exception as e:
            return HealthResult(ok=False, detail=str(e)[:200])

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
            )
        )
        return ConnectorResult(records=[t.__dict__ for t in tickets], total=len(tickets))

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        data = payload.data if hasattr(payload, "data") else payload
        ticket = await self.create_ticket(
            title=data.get("title", ""),
            description=data.get("description"),
            labels=data.get("labels"),
            idList=data.get("list_id"),
        )
        return {"ticket_id": ticket.id, "url": ticket.url or ""}

    async def list_tickets(self, filter: TicketFilter | None = None) -> list[Ticket]:
        async with httpx.AsyncClient() as client:
            params: dict[str, Any] = self._auth()
            params["fields"] = "id,name,desc,dateLastActivity,closed,due,url,idList,labels"
            if filter and filter.limit:
                params["limit"] = str(min(filter.limit, 100))
            resp = await client.get(
                f"{self._base_url}/boards/{self._board_id}/cards",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            raw_cards = resp.json()

        if filter and filter.search:
            raw_cards = [
                c for c in raw_cards if filter.search.lower() in (c.get("name", "") + (c.get("desc") or "")).lower()
            ]

        tickets = [self._to_ticket(c) for c in raw_cards]

        if filter and filter.status:
            tickets = [t for t in tickets if t.status and t.status.lower() == filter.status.lower()]

        return tickets

    async def get_ticket(self, ticket_id: str) -> Ticket:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/cards/{ticket_id}",
                params={
                    **self._auth(),
                    "fields": "id,name,desc,dateLastActivity,closed,due,url,idList,labels",
                },
                timeout=10,
            )
            resp.raise_for_status()
            return self._to_ticket(resp.json())

    async def create_ticket(self, title: str, description: str | None = None, **kwargs: Any) -> Ticket:
        body: dict[str, Any] = {"name": title, **self._auth()}
        if description:
            body["desc"] = description
        if kwargs.get("idList"):
            body["idList"] = kwargs["idList"]
        if "labels" in kwargs:
            raw_labels = kwargs["labels"]
            body["labels"] = ",".join(raw_labels) if isinstance(raw_labels, list) else raw_labels
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/cards",
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            return self._to_ticket(resp.json())

    async def update_ticket(self, ticket_id: str, **kwargs: Any) -> Ticket:
        body: dict[str, Any] = {**self._auth()}
        if kwargs.get("idList"):
            body["idList"] = kwargs["idList"]
        if "due" in kwargs:
            body["due"] = kwargs["due"]
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{self._base_url}/cards/{ticket_id}",
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            return self._to_ticket(resp.json())

    def _to_ticket(self, raw: dict) -> Ticket:
        return Ticket(
            id=raw.get("id", ""),
            title=raw.get("name", ""),
            description=raw.get("desc"),
            status="closed" if raw.get("closed") else "open",
            priority=None,
            ticket_type="task",
            labels=[label.get("name", "") for label in raw.get("labels", [])] if raw.get("labels") else [],
            url=raw.get("url") or raw.get("shortUrl"),
            created_at=None,
            updated_at=(datetime.fromisoformat(raw["dateLastActivity"]) if raw.get("dateLastActivity") else None),
            raw=raw,
        )

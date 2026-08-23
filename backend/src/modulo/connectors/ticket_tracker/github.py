"""GitHub Issues implementation of the TicketTrackerBase ABC."""

import asyncio
from typing import Any

import httpx

from modulo.connectors._safe_datetime import safe_datetime as _safe_datetime
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, ConnectorType, HealthResult
from modulo.connectors.ticket_tracker.base import Ticket, TicketFilter, TicketTrackerBase


class GitHubTicketTracker(TicketTrackerBase):
    def __init__(self, config: dict[str, Any], creds: dict[str, Any]) -> None:
        self._config = config
        self._creds = creds
        self._token = creds.get("token") or creds.get("api_key", "")
        self._repo = config.get("repo", "")
        self._base_url = config.get("base_url", "https://api.github.com")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITHUB

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

    async def health_check(self) -> HealthResult:
        headers = self._headers()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url}/repos/{self._repo}", headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                return HealthResult(ok=True, detail=data.get("full_name", self._repo))
        except asyncio.CancelledError:
            raise
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
            ),
        )
        return ConnectorResult(records=[t.__dict__ for t in tickets], total=len(tickets))

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        data = payload.data
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/repos/{self._repo}/issues",
                json={"title": data.get("title", ""), "body": data.get("description", "")},
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            return {"ticket_id": str(result["number"]), "url": result["html_url"]}

    async def list_tickets(self, ticket_filter: TicketFilter | None = None) -> list[Ticket]:
        per_page = min((ticket_filter.limit if ticket_filter else 20), 100)
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": ((ticket_filter.offset if ticket_filter else 0) // per_page) + 1,
            "state": "all",
        }
        if ticket_filter and ticket_filter.status and ticket_filter.status.lower() in ("open", "closed"):
            params["state"] = ticket_filter.status.lower()
        if ticket_filter and ticket_filter.labels:
            params["labels"] = ",".join(ticket_filter.labels)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/repos/{self._repo}/issues",
                headers=self._headers(),
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            raw_tickets = resp.json()
        return [self._to_ticket(t) for t in raw_tickets]

    async def get_ticket(self, ticket_id: str) -> Ticket:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/repos/{self._repo}/issues/{ticket_id}",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return self._to_ticket(resp.json())

    async def create_ticket(self, title: str, description: str | None = None, **kwargs: Any) -> Ticket:
        body: dict[str, Any] = {"title": title}
        if description:
            body["body"] = description
        if "labels" in kwargs:
            body["labels"] = kwargs["labels"]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/repos/{self._repo}/issues",
                json=body,
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return self._to_ticket(resp.json())

    async def update_ticket(self, ticket_id: str, **kwargs: Any) -> Ticket:
        body: dict[str, Any] = {}
        if "status" in kwargs:
            body["state"] = kwargs["status"]
        if "labels" in kwargs:
            body["labels"] = kwargs["labels"]
        if "title" in kwargs:
            body["title"] = kwargs["title"]
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{self._base_url}/repos/{self._repo}/issues/{ticket_id}",
                json=body,
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return self._to_ticket(resp.json())

    def _to_ticket(self, raw: dict[str, Any]) -> Ticket:
        label_objects = [label for label in (raw.get("labels") or []) if isinstance(label, dict)]
        labels = [label.get("name", "") for label in label_objects]
        assignee = raw.get("assignee")
        return Ticket(
            id=str(raw.get("number", "")),
            title=raw.get("title", ""),
            description=raw.get("body"),
            status="open" if not raw.get("closed_at") else "closed",
            priority=None,
            ticket_type="bug" if any(label.get("name") == "bug" for label in label_objects) else "task",
            labels=labels,
            url=raw.get("html_url"),
            assignee=assignee.get("login") if isinstance(assignee, dict) else None,
            created_at=_safe_datetime(raw.get("created_at")),
            updated_at=_safe_datetime(raw.get("updated_at")),
            raw=raw,
        )

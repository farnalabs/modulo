"""Abstract base class for task/ticket tracker connectors."""

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from modulo.connectors.base import ConnectorBase


@dataclass
class Ticket:
    id: str
    title: str
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    ticket_type: str | None = None
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    assignee: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict[str, Any] | None = None


@dataclass
class TicketFilter:
    status: str | None = None
    labels: list[str] | None = None
    search: str | None = None
    limit: int = 20
    offset: int = 0


class TicketTrackerBase(ConnectorBase):
    """ABC for connectors that manage tasks/tickets (Jira, Linear, Trello, GitHub Issues, etc.)."""

    @abstractmethod
    async def list_tickets(self, filter: TicketFilter | None = None) -> list[Ticket]: ...

    @abstractmethod
    async def get_ticket(self, ticket_id: str) -> Ticket: ...

    @abstractmethod
    async def create_ticket(self, title: str, description: str | None = None, **kwargs: Any) -> Ticket: ...

    @abstractmethod
    async def update_ticket(self, ticket_id: str, **kwargs: Any) -> Ticket: ...

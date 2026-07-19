"""Abstract base types for ticket-tracker connectors."""

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
    status: str = "open"
    priority: str | None = None
    ticket_type: str = "task"
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    assignee: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TicketFilter:
    status: str | None = None
    labels: list[str] | None = None
    search: str | None = None
    limit: int = 20
    offset: int = 0


class TicketTrackerBase(ConnectorBase):
    """Abstract base for ticket-tracker connectors.

    Subclasses wrap issue-tracker APIs (GitHub Issues, Linear, Jira, Trello, etc.)
    into a unified ticket interface.
    """

    @abstractmethod
    async def list_tickets(self, filter: TicketFilter | None = None) -> list[Ticket]:
        """List tickets matching an optional filter."""

    @abstractmethod
    async def get_ticket(self, ticket_id: str) -> Ticket:
        """Get a single ticket by ID."""

    @abstractmethod
    async def create_ticket(self, title: str, description: str | None = None, **kwargs: Any) -> Ticket:
        """Create a new ticket."""

    @abstractmethod
    async def update_ticket(self, ticket_id: str, **kwargs: Any) -> Ticket:
        """Update an existing ticket."""

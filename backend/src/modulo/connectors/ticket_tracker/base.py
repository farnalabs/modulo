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

    The base provides a scoped write capability, :meth:`comment`, for the thin
    connector model (FAR-370). It prefixes every comment with
    :attr:`COMMENT_PREFIX` (deduplicating so an already-prefixed body is not
    double-prefixed) and delegates the actual write to :meth:`_post_comment`,
    which subclasses must implement. Connectors that are read-only (document
    stores) or that opt out of comments need not implement ``_post_comment`` —
    the default raises ``NotImplementedError`` so the absence is explicit.
    """

    # Structured, prefixed marker prepended to every connector-authored comment
    # so downstream issue trackers can attribute and de-dupe Modulo comments.
    COMMENT_PREFIX: str = "[Modulo] "

    @abstractmethod
    async def get_ticket(self, ticket_id: str) -> Ticket:
        """Get a single ticket by ID."""

    async def list_tickets(self, filter: TicketFilter | None = None) -> list[Ticket]:
        """List tickets matching an optional filter.

        Thin connectors that are read/scoped-write only may leave this
        unimplemented; the default raises ``NotImplementedError``.
        """
        raise NotImplementedError("list_tickets is not implemented for this tracker")

    async def create_ticket(self, title: str, description: str | None = None, **kwargs: Any) -> Ticket:
        """Create a new ticket.

        The thin connector model (FAR-370) forbids auto-create, so the default
        raises ``NotImplementedError``. Subclasses that support creation
        override this.
        """
        raise NotImplementedError("create_ticket is not implemented for this tracker")

    async def update_ticket(self, ticket_id: str, **kwargs: Any) -> Ticket:
        """Update an existing ticket.

        The default raises ``NotImplementedError``; thin connectors implement
        the specific scoped writes they support (e.g. status) directly.
        """
        raise NotImplementedError("update_ticket is not implemented for this tracker")

    async def comment(self, issue_ref: str, body: str) -> dict[str, Any]:
        """Scoped T3 write: post a structured, prefixed comment to an issue.

        The *body* is prefixed with :attr:`COMMENT_PREFIX` and de-duplicated
        (a body already carrying the prefix is not double-prefixed). The actual
        write is delegated to :meth:`_post_comment`, which subclasses implement.

        Args:
            issue_ref: The issue identifier (id or key, e.g. ``TEAM-123``).
            body: The comment text to post.

        Returns:
            A dict describing the created comment (connector-specific shape).
        """
        if not issue_ref:
            raise ValueError("comment requires a non-empty issue_ref")
        if body is None:
            raise ValueError("comment requires a body")
        prefixed = self._prefix_comment(body)
        return await self._post_comment(issue_ref, prefixed)

    def _prefix_comment(self, body: str) -> str:
        """Prefix *body* with :attr:`COMMENT_PREFIX`, de-duplicating."""
        body = body.strip()
        if body.startswith(self.COMMENT_PREFIX):
            return body
        return f"{self.COMMENT_PREFIX}{body}"

    async def _post_comment(self, issue_ref: str, body: str) -> dict[str, Any]:
        """Perform the comment write. Subclasses must implement this."""
        raise NotImplementedError("comment write is not implemented for this tracker")

"""Unit tests for GitHubTicketTracker connector."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, HealthResult
from modulo.connectors.ticket_tracker.base import TicketFilter
from modulo.connectors.ticket_tracker.github import GitHubTicketTracker


def _response(status_code: int, **kwargs: object) -> httpx.Response:
    req = httpx.Request("GET", "https://api.github.com/")
    return httpx.Response(status_code, request=req, **kwargs)


def _make_mock_issue(overrides: dict | None = None) -> dict:
    base = {
        "number": 42,
        "title": "Fix login bug",
        "body": "Users cannot log in with SSO",
        "state": "open",
        "closed_at": None,
        "labels": [{"name": "bug"}, {"name": "auth"}],
        "html_url": "https://github.com/owner/repo/issues/42",
        "assignee": {"login": "alice"},
        "created_at": "2025-01-15T10:00:00Z",
        "updated_at": "2025-01-16T12:00:00Z",
    }
    if overrides:
        base.update(overrides)
    return base


@pytest.fixture
def tracker() -> GitHubTicketTracker:
    return GitHubTicketTracker(
        config={"repo": "owner/repo", "base_url": "https://api.github.com"},
        creds={"token": "ghp_fake_token"},
    )


class TestToTicket:
    def test_parses_open_issue(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue()
        ticket = tracker._to_ticket(raw)
        assert ticket.id == "42"
        assert ticket.title == "Fix login bug"
        assert ticket.description == "Users cannot log in with SSO"
        assert ticket.status == "open"
        assert ticket.ticket_type == "bug"
        assert ticket.labels == ["bug", "auth"]
        assert ticket.url == "https://github.com/owner/repo/issues/42"
        assert ticket.assignee == "alice"
        assert isinstance(ticket.created_at, datetime)
        assert isinstance(ticket.updated_at, datetime)

    def test_parses_closed_issue(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue({"closed_at": "2025-01-17T08:00:00Z"})
        ticket = tracker._to_ticket(raw)
        assert ticket.status == "closed"

    def test_parses_task_type(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue({"labels": [{"name": "enhancement"}]})
        ticket = tracker._to_ticket(raw)
        assert ticket.ticket_type == "task"

    def test_handles_minimal_issue(self, tracker: GitHubTicketTracker) -> None:
        raw = {"number": 1, "title": "Minimal", "labels": []}
        ticket = tracker._to_ticket(raw)
        assert ticket.id == "1"
        assert ticket.title == "Minimal"
        assert ticket.status == "open"
        assert ticket.ticket_type == "task"
        assert not ticket.labels
        assert ticket.assignee is None

    def test_handles_empty_labels(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue({"labels": None})
        ticket = tracker._to_ticket(raw)
        assert not ticket.labels

    def test_handles_non_dict_label_entries(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue({"labels": ["bug", {"name": "auth"}]})
        ticket = tracker._to_ticket(raw)
        assert ticket.labels == ["auth"]
        assert ticket.ticket_type == "task"

    def test_handles_corrupt_labels(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue({"labels": "bug, auth"})
        ticket = tracker._to_ticket(raw)
        assert not ticket.labels
        assert ticket.ticket_type == "task"

    def test_handles_corrupt_assignee(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue({"assignee": "alice"})
        ticket = tracker._to_ticket(raw)
        assert ticket.assignee is None

    def test_handles_corrupt_created_at(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue({"created_at": "not-a-date", "updated_at": "2025-13-99T10:00:00Z"})
        ticket = tracker._to_ticket(raw)
        assert ticket.created_at is None
        assert ticket.updated_at is None

    def test_handles_non_string_timestamps(self, tracker: GitHubTicketTracker) -> None:
        raw = _make_mock_issue({"created_at": {"$date": "2025-01-15T10:00:00Z"}, "updated_at": 1737043200})
        ticket = tracker._to_ticket(raw)
        assert ticket.created_at is None
        assert ticket.updated_at is None


class TestListTickets:
    @patch("httpx.AsyncClient")
    async def test_lists_tickets_with_default_filter(
        self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker
    ) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json=[_make_mock_issue()])

        tickets = await tracker.list_tickets()

        assert len(tickets) == 1
        assert tickets[0].id == "42"
        mock_client.get.assert_called_once()
        call_kwargs = mock_client.get.call_args[1]
        assert "params" in call_kwargs
        assert call_kwargs["params"]["state"] == "all"

    @patch("httpx.AsyncClient")
    async def test_filters_by_status(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json=[_make_mock_issue()])

        tickets = await tracker.list_tickets(TicketFilter(status="closed"))

        assert len(tickets) == 1
        call_kwargs = mock_client.get.call_args[1]
        assert call_kwargs["params"]["state"] == "closed"

    @patch("httpx.AsyncClient")
    async def test_filters_by_labels(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json=[])

        tickets = await tracker.list_tickets(TicketFilter(labels=["bug"]))

        assert tickets == []
        call_kwargs = mock_client.get.call_args[1]
        assert call_kwargs["params"]["labels"] == "bug"

    @patch("httpx.AsyncClient")
    async def test_offset_page_math_uses_effective_per_page(
        self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker
    ) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json=[])

        tickets = await tracker.list_tickets(TicketFilter(limit=50, offset=100))

        assert tickets == []
        call_kwargs = mock_client.get.call_args[1]
        assert call_kwargs["params"]["per_page"] == 50
        assert call_kwargs["params"]["page"] == 3


class TestGetTicket:
    @patch("httpx.AsyncClient")
    async def test_gets_ticket_by_id(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json=_make_mock_issue())

        ticket = await tracker.get_ticket("42")

        assert ticket.id == "42"
        mock_client.get.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/issues/42",
            headers=tracker._headers(),
            timeout=10,
        )


class TestCreateTicket:
    @patch("httpx.AsyncClient")
    async def test_creates_ticket(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = _response(201, json=_make_mock_issue())

        ticket = await tracker.create_ticket("Fix login bug", description="SSO broken", labels=["bug"])

        assert ticket.id == "42"
        assert ticket.title == "Fix login bug"
        mock_client.post.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/issues",
            json={"title": "Fix login bug", "body": "SSO broken", "labels": ["bug"]},
            headers=tracker._headers(),
            timeout=10,
        )


class TestUpdateTicket:
    @patch("httpx.AsyncClient")
    async def test_updates_ticket_status(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.patch.return_value = _response(200, json=_make_mock_issue({"closed_at": "2025-01-17T08:00:00Z"}))

        ticket = await tracker.update_ticket("42", status="closed")

        assert ticket.status == "closed"
        mock_client.patch.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/issues/42",
            json={"state": "closed"},
            headers=tracker._headers(),
            timeout=10,
        )


class TestHealthCheck:
    @patch("httpx.AsyncClient")
    async def test_returns_healthy_on_success(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json={"full_name": "owner/repo", "id": 1})

        result = await tracker.health_check()

        assert isinstance(result, HealthResult)
        assert result.ok is True
        assert result.detail == "owner/repo"

    @patch("httpx.AsyncClient")
    async def test_returns_unhealthy_on_failure(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=_response(404)
        )

        result = await tracker.health_check()

        assert result.ok is False


class TestQuery:
    @patch("httpx.AsyncClient")
    async def test_query_by_ticket_id(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json=_make_mock_issue())

        result = await tracker.query(ConnectorQuery(resource="issue", filters={"ticket_id": "42"}))

        assert isinstance(result, ConnectorResult)
        assert result.total == 1

    @patch("httpx.AsyncClient")
    async def test_query_lists_tickets(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json=[_make_mock_issue()])

        result = await tracker.query(ConnectorQuery(resource="issues"))

        assert isinstance(result, ConnectorResult)
        assert result.total == 1


class TestWrite:
    @patch("httpx.AsyncClient")
    async def test_writes_ticket(self, mock_client_cls: MagicMock, tracker: GitHubTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = _response(
            201, json={"number": 42, "html_url": "https://github.com/owner/repo/issues/42"}
        )

        result = await tracker.write(
            ConnectorPayload(resource="issue", data={"title": "New bug", "description": "Desc"})
        )

        assert result["ticket_id"] == "42"
        assert result["url"] == "https://github.com/owner/repo/issues/42"


class TestConnectorType:
    def test_returns_github(self, tracker: GitHubTicketTracker) -> None:
        assert tracker.connector_type.value == "github"

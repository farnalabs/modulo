"""Unit tests for TrelloTicketTracker connector."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from modulo.connectors.base import HealthResult
from modulo.connectors.ticket_tracker.base import TicketFilter
from modulo.connectors.ticket_tracker.trello import TrelloTicketTracker


def _response(status_code: int, **kwargs: object) -> httpx.Response:
    req = httpx.Request("GET", "https://api.trello.com/1/")
    return httpx.Response(status_code, request=req, **kwargs)


def _make_mock_card(overrides: dict | None = None) -> dict:
    base = {
        "id": "abc123",
        "name": "Fix login bug",
        "desc": "Users cannot log in with SSO",
        "closed": False,
        "dateLastActivity": "2025-01-16T12:00:00.000Z",
        "due": None,
        "url": "https://trello.com/c/abc123",
        "idList": "list456",
        "labels": [{"name": "bug"}, {"name": "auth"}],
    }
    if overrides:
        base.update(overrides)
    return base


@pytest.fixture
def tracker() -> TrelloTicketTracker:
    return TrelloTicketTracker(
        config={"board_id": "board123"},
        creds={"api_key": "fake_key", "token": "fake_token"},
    )


class TestToTicket:
    def test_parses_open_card(self, tracker: TrelloTicketTracker) -> None:
        raw = _make_mock_card()
        ticket = tracker._to_ticket(raw)
        assert ticket.id == "abc123"
        assert ticket.title == "Fix login bug"
        assert ticket.description == "Users cannot log in with SSO"
        assert ticket.status == "open"
        assert ticket.labels == ["bug", "auth"]
        assert ticket.url == "https://trello.com/c/abc123"
        assert isinstance(ticket.updated_at, datetime)

    def test_parses_closed_card(self, tracker: TrelloTicketTracker) -> None:
        raw = _make_mock_card({"closed": True})
        ticket = tracker._to_ticket(raw)
        assert ticket.status == "closed"

    def test_handles_minimal_card(self, tracker: TrelloTicketTracker) -> None:
        raw = {"id": "min1", "name": "Minimal", "labels": []}
        ticket = tracker._to_ticket(raw)
        assert ticket.id == "min1"
        assert ticket.title == "Minimal"
        assert ticket.status == "open"
        assert ticket.labels == []

    def test_handles_empty_labels(self, tracker: TrelloTicketTracker) -> None:
        raw = _make_mock_card({"labels": None})
        ticket = tracker._to_ticket(raw)
        assert ticket.labels == []


class TestListTickets:
    @patch("httpx.AsyncClient")
    async def test_lists_tickets(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(
            200, json=[_make_mock_card(), _make_mock_card({"id": "def456", "name": "Second card"})]
        )

        tickets = await tracker.list_tickets()

        assert len(tickets) == 2
        assert tickets[0].id == "abc123"
        assert tickets[1].id == "def456"
        mock_client.get.assert_called_once()

    @patch("httpx.AsyncClient")
    async def test_filters_by_search(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(
            200,
            json=[
                _make_mock_card(),
                _make_mock_card({"id": "def456", "name": "Deploy site", "desc": "Push to prod"}),
            ],
        )

        tickets = await tracker.list_tickets(TicketFilter(search="login"))

        assert len(tickets) == 1
        assert tickets[0].id == "abc123"


class TestGetTicket:
    @patch("httpx.AsyncClient")
    async def test_gets_ticket_by_id(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json=_make_mock_card())

        ticket = await tracker.get_ticket("abc123")

        assert ticket.id == "abc123"
        assert ticket.title == "Fix login bug"
        mock_client.get.assert_called_once_with(
            "https://api.trello.com/1/cards/abc123",
            params={
                "key": "fake_key",
                "token": "fake_token",
                "fields": "id,name,desc,dateLastActivity,closed,due,url,idList,labels",
            },
            timeout=10,
        )


class TestCreateTicket:
    @patch("httpx.AsyncClient")
    async def test_creates_ticket(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = _response(200, json=_make_mock_card())

        ticket = await tracker.create_ticket("Fix login bug", description="SSO broken", labels=["bug"])

        assert ticket.id == "abc123"
        assert ticket.title == "Fix login bug"


class TestHealthCheck:
    @patch("httpx.AsyncClient")
    async def test_returns_healthy_on_success(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(200, json={"id": "board123", "name": "My Board"})

        result = await tracker.health_check()

        assert isinstance(result, HealthResult)
        assert result.ok is True
        assert result.detail == "My Board"

    @patch("httpx.AsyncClient")
    async def test_returns_unhealthy_on_failure(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        result = await tracker.health_check()

        assert isinstance(result, HealthResult)
        assert result.ok is False


class TestConnectorType:
    def test_returns_ticket_tracker(self, tracker: TrelloTicketTracker) -> None:
        assert tracker.connector_type.value == "ticket-tracker"

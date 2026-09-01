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


def _response_with_auth(status_code: int, *, text: str = "") -> httpx.Response:
    """A response whose request URL carries the credential query string, as a real
    Trello request would — used to prove error detail never leaks them."""
    req = httpx.Request(
        "GET",
        "https://api.trello.com/1/boards/board123",
        params={"key": "fake_key", "token": "fake_token"},
    )
    return httpx.Response(status_code, request=req, text=text)


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
        assert not ticket.labels

    def test_handles_empty_labels(self, tracker: TrelloTicketTracker) -> None:
        raw = _make_mock_card({"labels": None})
        ticket = tracker._to_ticket(raw)
        assert not ticket.labels

    def test_missing_labels_key(self, tracker: TrelloTicketTracker) -> None:
        raw = {"id": "no-labels", "name": "No Labels", "closed": False}
        ticket = tracker._to_ticket(raw)
        assert not ticket.labels

    def test_handles_non_dict_label_entries(self, tracker: TrelloTicketTracker) -> None:
        raw = _make_mock_card({"labels": ["bug", {"name": "auth"}]})
        ticket = tracker._to_ticket(raw)
        assert ticket.labels == ["auth"]

    def test_handles_corrupt_labels(self, tracker: TrelloTicketTracker) -> None:
        raw = _make_mock_card({"labels": "bug, auth"})
        ticket = tracker._to_ticket(raw)
        assert not ticket.labels

    def test_handles_corrupt_date_last_activity(self, tracker: TrelloTicketTracker) -> None:
        raw = _make_mock_card({"dateLastActivity": "not-a-date"})
        ticket = tracker._to_ticket(raw)
        assert ticket.updated_at is None

    def test_handles_non_string_date_last_activity(self, tracker: TrelloTicketTracker) -> None:
        raw = _make_mock_card({"dateLastActivity": 1737043200})
        ticket = tracker._to_ticket(raw)
        assert ticket.updated_at is None


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

    @patch("httpx.AsyncClient")
    async def test_filters_by_status(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(
            200,
            json=[
                _make_mock_card(),
                _make_mock_card({"id": "def456", "name": "Closed card", "closed": True}),
            ],
        )

        tickets = await tracker.list_tickets(TicketFilter(status="open"))

        assert len(tickets) == 1
        assert tickets[0].id == "abc123"
        assert tickets[0].status == "open"

    @patch("httpx.AsyncClient")
    async def test_http_error(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response(429)

        with pytest.raises(ValueError, match="Trello API error: 429"):
            await tracker.list_tickets()


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

    @patch("httpx.AsyncClient")
    async def test_network_error(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(ValueError, match="Trello network error"):
            await tracker.get_ticket("abc123")


class TestCreateTicket:
    @patch("httpx.AsyncClient")
    async def test_creates_ticket(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = _response(200, json=_make_mock_card())

        ticket = await tracker.create_ticket(
            "Fix login bug", description="SSO broken", labels=["bug"], idList="list456"
        )

        assert ticket.id == "abc123"
        assert ticket.title == "Fix login bug"

    @patch("httpx.AsyncClient")
    async def test_posts_payload(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = _response(200, json=_make_mock_card())

        await tracker.create_ticket("New card", idList="list789")

        mock_client.post.assert_called_once_with(
            "https://api.trello.com/1/cards",
            params={"key": "fake_key", "token": "fake_token"},
            data={"name": "New card", "idList": "list789"},
            timeout=10,
        )

    async def test_missing_idlist(self, tracker: TrelloTicketTracker) -> None:
        with pytest.raises(ValueError, match="idList is required to create a Trello card"):
            await tracker.create_ticket("No list card")

    @patch("httpx.AsyncClient")
    async def test_http_error(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = _response(401, json={"message": "unauthorized"})

        with pytest.raises(ValueError, match="Trello API error: 401"):
            await tracker.create_ticket("Unauthorized", idList="list456")


class TestUpdateTicket:
    @patch("httpx.AsyncClient")
    async def test_updates_ticket(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.put.return_value = _response(200, json=_make_mock_card())

        ticket = await tracker.update_ticket("abc123", idList="list789", due="2025-02-01T00:00:00.000Z")

        assert ticket.id == "abc123"
        assert ticket.title == "Fix login bug"
        mock_client.put.assert_called_once_with(
            "https://api.trello.com/1/cards/abc123",
            params={"key": "fake_key", "token": "fake_token"},
            data={"idList": "list789", "due": "2025-02-01T00:00:00.000Z"},
            timeout=10,
        )

    @patch("httpx.AsyncClient")
    async def test_http_error(self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.put.return_value = _response(404)

        with pytest.raises(ValueError, match="Trello API error: 404"):
            await tracker.update_ticket("nonexistent")


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


class TestCredentialRedaction:
    def test_redact_strips_live_credentials(self, tracker: TrelloTicketTracker) -> None:
        msg = "error for url 'https://api.trello.com/1/boards/x?key=fake_key&token=fake_token'"
        out = tracker._redact(msg)
        assert "fake_key" not in out
        assert "fake_token" not in out
        assert "***" in out

    @patch("httpx.AsyncClient")
    async def test_health_http_error_detail_redacts(
        self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker
    ) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response_with_auth(401, text="unauthorized")

        result = await tracker.health_check()

        assert isinstance(result, HealthResult)
        assert result.ok is False
        assert "fake_key" not in result.detail
        assert "fake_token" not in result.detail

    @patch("httpx.AsyncClient")
    async def test_health_transport_error_detail_redacts(
        self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker
    ) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        result = await tracker.health_check()

        assert isinstance(result, HealthResult)
        assert result.ok is False
        assert "fake_key" not in result.detail
        assert "fake_token" not in result.detail

    @patch("httpx.AsyncClient")
    async def test_list_tickets_http_error_detail_redacts(
        self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker
    ) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = _response_with_auth(401, text="unauthorized")

        with pytest.raises(ValueError, match="Trello API error") as exc_info:
            await tracker.list_tickets()

        assert "fake_key" not in str(exc_info.value)
        assert "fake_token" not in str(exc_info.value)

    @patch("httpx.AsyncClient")
    async def test_list_tickets_network_error_detail_redacts(
        self, mock_client_cls: MagicMock, tracker: TrelloTicketTracker
    ) -> None:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(ValueError, match="Trello network error") as exc_info:
            await tracker.list_tickets()

        assert "fake_key" not in str(exc_info.value)
        assert "fake_token" not in str(exc_info.value)


class TestInit:
    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(ValueError, match="Trello connector requires api_key and token credentials"):
            TrelloTicketTracker(config={"board_id": "board123"}, creds={})

    def test_missing_token_raises(self) -> None:
        with pytest.raises(ValueError, match="Trello connector requires api_key and token credentials"):
            TrelloTicketTracker(config={"board_id": "board123"}, creds={"api_key": "key_only"})

    def test_empty_credentials_raises(self) -> None:
        with pytest.raises(ValueError, match="Trello connector requires api_key and token credentials"):
            TrelloTicketTracker(config={"board_id": "board123"}, creds={"api_key": "", "token": ""})

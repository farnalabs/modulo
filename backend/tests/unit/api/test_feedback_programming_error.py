"""Unit tests for ProgrammingError handling on feedback API routes."""
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )

@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("mock", {}, "table not found"))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()

_RUN_ID = uuid.uuid4()
_RECORD_ID = uuid.uuid4()

class TestFeedbackProgrammingError:
    def test_create_feedback_returns_501(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/feedback",
            json={
                "gate_id": "gate-1",
                "rejection_reason": "Wrong",
                "rejected_output": {},
                "producing_node_id": "node-b",
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_list_feedback_returns_501(self, client: TestClient) -> None:
        resp = client.get("/api/v1/feedback")
        assert resp.status_code == 501

    def test_get_feedback_returns_501(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/feedback/{_RECORD_ID}")
        assert resp.status_code == 501

    def test_update_feedback_status_returns_501(self, client: TestClient) -> None:
        resp = client.patch(
            f"/api/v1/feedback/{_RECORD_ID}/status",
            json={"status": "resolved"},
        )
        assert resp.status_code == 501

    def test_detect_eval_gap_returns_501(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/feedback/{_RECORD_ID}/detect-gap")
        assert resp.status_code == 501

    def test_list_feedback_inbox_returns_501(self, client: TestClient) -> None:
        resp = client.get("/api/v1/feedback/inbox")
        assert resp.status_code == 501

    def test_get_inbox_item_returns_501(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/feedback/inbox/{_RECORD_ID}")
        assert resp.status_code == 501

    def test_review_feedback_returns_501(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/feedback/inbox/{_RECORD_ID}/review",
            json={"action": "mark_reviewed"},
        )
        assert resp.status_code == 501

    def test_list_eval_proposals_returns_501(self, client: TestClient) -> None:
        resp = client.get("/api/v1/feedback/proposals")
        assert resp.status_code == 501

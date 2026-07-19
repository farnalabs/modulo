"""Unit tests for /api/v1/feedback endpoints including inbox/review/proposals."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()
_RECORD_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",  # nosec — test-only value
    )


def _make_mock_record(**overrides: object) -> MagicMock:
    r = MagicMock(spec=FeedbackRecord)
    r.id = overrides.get("id", _RECORD_ID)
    r.organisation_id = _ORG_ID
    r.run_id = overrides.get("run_id", _RUN_ID)
    r.gate_id = overrides.get("gate_id", "gate-1")
    r.account_id = overrides.get("account_id", _USER_ID)
    r.rejection_reason = overrides.get("rejection_reason", "Wrong output")
    r.rejected_output = overrides.get("rejected_output", {"result": "bad"})
    r.producing_node_id = overrides.get("producing_node_id", "node-b")
    r.producing_agent_id = overrides.get("producing_agent_id")
    r.feedback_status = overrides.get("feedback_status", "pending")
    r.feedback_handler_type = overrides.get("feedback_handler_type", "human")
    r.correction_run_id = overrides.get("correction_run_id")
    r.eval_gap = overrides.get("eval_gap")
    r.created_at = overrides.get("created_at", _NOW)
    r.updated_at = overrides.get("updated_at", _NOW)
    return r


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    result = MagicMock()
    result.scalar_one_or_none.return_value = MagicMock(id=_RUN_ID)
    session.execute = AsyncMock(return_value=result)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCreateFeedback:
    def test_creates_feedback_record(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.create_feedback_record") as mock_create,
        ):
            mock_create.return_value = _make_mock_record()

            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/feedback",
                json={
                    "gate_id": "gate-1",
                    "rejection_reason": "Wrong output",
                    "rejected_output": {"result": "bad"},
                    "producing_node_id": "node-b",
                    "feedback_handler_type": "human",
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["feedback_status"] == "pending"
        assert body["gate_id"] == "gate-1"

    def test_returns_404_when_run_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.create_feedback_record") as mock_create,
        ):
            mock_create.side_effect = HTTPException(status_code=404, detail="Run not found")
            resp = client.post(
                f"/api/v1/runs/{uuid.uuid4()}/feedback",
                json={
                    "gate_id": "gate-1",
                    "rejection_reason": "Wrong",
                    "rejected_output": {},
                    "producing_node_id": "node-b",
                },
            )

        assert resp.status_code == 404
        assert "Run not found" in resp.text

    def test_returns_401_when_unauthenticated(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            f"/api/v1/runs/{_RUN_ID}/feedback",
            json={
                "gate_id": "gate-1",
                "rejection_reason": "Wrong",
                "rejected_output": {},
                "producing_node_id": "node-b",
            },
        )
        assert resp.status_code in (401, 403)


class TestListFeedback:
    def test_returns_paginated_list(self, client: TestClient) -> None:
        mock_record = _make_mock_record()

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_records") as mock_list,
        ):
            mock_list.return_value = {
                "items": [mock_record],
                "total": 1,
                "page": 1,
                "page_size": 20,
            }

            resp = client.get("/api/v1/feedback")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_filters_by_status(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_records") as mock_list,
        ):
            mock_list.return_value = {"items": [], "total": 0, "page": 1, "page_size": 20}

            resp = client.get("/api/v1/feedback?status=pending")

        assert resp.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs.get("status") == "pending"
        assert resp.json()["total"] == 0


class TestGetFeedback:
    def test_returns_record(self, client: TestClient) -> None:
        mock_record = _make_mock_record()

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
        ):
            mock_get.return_value = mock_record

            resp = client.get(f"/api/v1/feedback/{_RECORD_ID}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(_RECORD_ID)
        assert body["feedback_status"] == "pending"

    def test_returns_404_when_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
        ):
            mock_get.return_value = None

            resp = client.get(f"/api/v1/feedback/{uuid.uuid4()}")

        assert resp.status_code == 404


class TestUpdateStatus:
    def test_updates_status(self, client: TestClient) -> None:
        mock_record = _make_mock_record(feedback_status="resolved")

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.update_status") as mock_update,
        ):
            mock_update.return_value = mock_record

            resp = client.patch(
                f"/api/v1/feedback/{_RECORD_ID}/status",
                json={"status": "resolved"},
            )

        assert resp.status_code == 200
        assert resp.json()["feedback_status"] == "resolved"

    def test_rejects_invalid_status(self, client: TestClient) -> None:
        resp = client.patch(
            f"/api/v1/feedback/{_RECORD_ID}/status",
            json={"status": "invalid_status"},
        )
        assert resp.status_code == 422

    def test_returns_404_when_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.update_status") as mock_update,
        ):
            mock_update.return_value = None

            resp = client.patch(
                f"/api/v1/feedback/{uuid.uuid4()}/status",
                json={"status": "resolved"},
            )

        assert resp.status_code == 404


class TestDetectEvalGap:
    def test_detects_eval_gap(self, client: TestClient) -> None:
        mock_record = _make_mock_record(run_id=None)

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
            patch("modulo.api.routes.feedback.FeedbackManager.detect_eval_gap") as mock_detect,
        ):
            mock_get.return_value = mock_record
            mock_detect.return_value = True

            resp = client.post(f"/api/v1/feedback/{_RECORD_ID}/detect-gap")

        assert resp.status_code == 200
        assert resp.json()["eval_gap"] is True

    def test_returns_404_when_record_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
        ):
            mock_get.return_value = None

            resp = client.post(f"/api/v1/feedback/{uuid.uuid4()}/detect-gap")

        assert resp.status_code == 404


class TestListFeedbackInbox:
    def test_returns_paginated_inbox(self, client: TestClient) -> None:
        mock_record = _make_mock_record()

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_records_inbox") as mock_inbox,
        ):
            mock_inbox.return_value = {
                "items": [mock_record],
                "pipeline_map": {},
                "total": 1,
                "page": 1,
                "page_size": 20,
            }

            resp = client.get("/api/v1/feedback/inbox")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_filters_by_type_and_status(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_records_inbox") as mock_inbox,
        ):
            mock_inbox.return_value = {
                "items": [],
                "pipeline_map": {},
                "total": 0,
                "page": 1,
                "page_size": 20,
            }

            resp = client.get("/api/v1/feedback/inbox?type=human&status=pending")

        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_filters_by_date_range(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_records_inbox") as mock_inbox,
        ):
            mock_inbox.return_value = {
                "items": [],
                "pipeline_map": {},
                "total": 0,
                "page": 1,
                "page_size": 20,
            }

            resp = client.get("/api/v1/feedback/inbox?date_from=2025-01-01T00:00:00&date_to=2025-12-31T23:59:59")

        assert resp.status_code == 200


class TestGetInboxItem:
    def test_returns_record(self, client: TestClient) -> None:
        mock_record = _make_mock_record(run_id=None)

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
        ):
            mock_get.return_value = mock_record

            resp = client.get(f"/api/v1/feedback/inbox/{_RECORD_ID}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(_RECORD_ID)

    def test_returns_404_when_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
        ):
            mock_get.return_value = None

            resp = client.get(f"/api/v1/feedback/inbox/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestReviewFeedback:
    def test_marks_as_reviewed(self, client: TestClient) -> None:
        mock_record = _make_mock_record(feedback_status="resolved")

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
            patch("modulo.api.routes.feedback.FeedbackManager.update_status") as mock_update,
        ):
            mock_get.return_value = _make_mock_record()
            mock_update.return_value = mock_record

            resp = client.post(
                f"/api/v1/feedback/inbox/{_RECORD_ID}/review",
                json={"action": "mark_reviewed"},
            )

        assert resp.status_code == 200
        assert resp.json()["feedback_status"] == "resolved"

    def test_dismisses_feedback(self, client: TestClient) -> None:
        mock_record = _make_mock_record(feedback_status="dismissed")

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
            patch("modulo.api.routes.feedback.FeedbackManager.update_status") as mock_update,
        ):
            mock_get.return_value = _make_mock_record()
            mock_update.return_value = mock_record

            resp = client.post(
                f"/api/v1/feedback/inbox/{_RECORD_ID}/review",
                json={"action": "dismiss"},
            )

        assert resp.status_code == 200
        assert resp.json()["feedback_status"] == "dismissed"
        call_args, _call_kwargs = mock_update.call_args
        assert call_args[1] == "resolved"

    def test_rejects_invalid_action(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/feedback/inbox/{_RECORD_ID}/review",
            json={"action": "invalid_action"},
        )
        assert resp.status_code == 422

    def test_returns_404_when_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record") as mock_get,
        ):
            mock_get.return_value = None

            resp = client.post(
                f"/api/v1/feedback/inbox/{uuid.uuid4()}/review",
                json={"action": "mark_reviewed"},
            )
        assert resp.status_code == 404


class TestListEvalProposals:
    def test_returns_proposals(self, client: TestClient) -> None:
        mock_record = _make_mock_record(eval_gap=True, run_id=None)

        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_eval_proposals") as mock_proposals,
        ):
            mock_proposals.return_value = {
                "items": [mock_record],
                "total": 1,
                "page": 1,
                "page_size": 20,
            }

            resp = client.get("/api/v1/feedback/proposals")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_returns_empty_when_no_proposals(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.feedback.set_rls_org"),
            patch("modulo.api.routes.feedback.FeedbackManager.get_eval_proposals") as mock_proposals,
        ):
            mock_proposals.return_value = {"items": [], "total": 0, "page": 1, "page_size": 20}

            resp = client.get("/api/v1/feedback/proposals")

        assert resp.status_code == 200
        assert resp.json()["total"] == 0

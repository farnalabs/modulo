"""Unit tests for Remy UI Command API endpoints — permission response, results, reset."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.api.routes.remy import (
    _pending_permissions,
    _pending_ui_results,
    _permission_decisions,
    _session_approvals,
    _ui_command_results,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
SESSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


@pytest.fixture(autouse=True)
def _clean_registries():
    _pending_permissions.clear()
    _permission_decisions.clear()
    _pending_ui_results.clear()
    _ui_command_results.clear()
    _session_approvals.clear()


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="test-user",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
    )

    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)
    chat_session = MagicMock()
    chat_session.id = SESSION_ID
    chat_session.user_id = USER_ID
    mock_session.get = AsyncMock(return_value=chat_session)
    scalar_result = MagicMock()
    scalar_result.scalar = MagicMock(return_value=None)
    scalar_result.scalar_one_or_none = MagicMock(return_value=None)
    scalar_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    mock_session.execute = AsyncMock(return_value=scalar_result)

    async def _override_get_db_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = _override_get_db_session

    with patch("modulo.api.routes.remy._get_registry", return_value=None):
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def mock_session_ownership():
    """Patch the session ownership check to always succeed."""
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    chat_session = MagicMock()
    chat_session.id = SESSION_ID
    chat_session.user_id = USER_ID
    session.get = AsyncMock(return_value=chat_session)

    with (
        patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.remy.get_db_session", return_value=session),
    ):
        yield


class TestPermissionResponseEndpoint:
    """POST /sessions/{id}/permission-response"""

    def test_approve_action(self, client, mock_session_ownership):
        req_id = str(uuid.uuid4())
        event = asyncio.Event()
        _pending_permissions[req_id] = (event, str(SESSION_ID))

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/permission-response",
            json={"request_id": req_id, "action": "approve"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert _permission_decisions[req_id] == {"action": "approve"}
        assert event.is_set()

    def test_approve_for_session_action(self, client, mock_session_ownership):
        req_id = str(uuid.uuid4())
        event = asyncio.Event()
        _pending_permissions[req_id] = (event, str(SESSION_ID))

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/permission-response",
            json={"request_id": req_id, "action": "approve_for_session"},
        )
        assert resp.status_code == 200
        assert _permission_decisions[req_id] == {"action": "approve_for_session"}

    def test_reject_action(self, client, mock_session_ownership):
        req_id = str(uuid.uuid4())
        event = asyncio.Event()
        _pending_permissions[req_id] = (event, str(SESSION_ID))

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/permission-response",
            json={"request_id": req_id, "action": "reject"},
        )
        assert resp.status_code == 200
        assert _permission_decisions[req_id] == {"action": "reject"}

    def test_unknown_request_id_returns_404(self, client, mock_session_ownership):
        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/permission-response",
            json={"request_id": "nonexistent", "action": "approve"},
        )
        assert resp.status_code == 404

    def test_wrong_session_id_returns_403(self, client, mock_session_ownership):
        req_id = str(uuid.uuid4())
        event = asyncio.Event()
        wrong_session = str(uuid.uuid4())
        _pending_permissions[req_id] = (event, wrong_session)

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/permission-response",
            json={"request_id": req_id, "action": "approve"},
        )
        assert resp.status_code == 403

    def test_session_not_found_returns_404(self, client):
        with (
            patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.remy.get_db_session") as mock_get_db,
        ):
            mock_session_inst = AsyncMock()
            begin_cm = MagicMock()
            begin_cm.__aenter__ = AsyncMock(return_value=None)
            begin_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_inst.begin = MagicMock(return_value=begin_cm)
            mock_session_inst.get = AsyncMock(return_value=None)
            mock_get_db.return_value = mock_session_inst

            resp = client.post(
                f"/api/v1/remy/sessions/{SESSION_ID}/permission-response",
                json={"request_id": "req-1", "action": "approve"},
            )
            assert resp.status_code == 404


class TestUiCommandResultsEndpoint:
    """POST /sessions/{id}/ui-command-results"""

    def test_submit_results(self, client, mock_session_ownership):
        event = asyncio.Event()
        _pending_ui_results[str(SESSION_ID)] = event

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/ui-command-results",
            json={
                "results": [
                    {"id": "nav-1", "name": "navigate", "success": True, "result": {"url": "/admin/pipelines"}},
                    {"id": "click-1", "name": "click", "success": True, "result": None},
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert len(_ui_command_results[str(SESSION_ID)]) == 2
        assert _ui_command_results[str(SESSION_ID)][0]["name"] == "navigate"
        assert _ui_command_results[str(SESSION_ID)][0]["success"] is True
        assert event.is_set()

    def test_submit_with_errors(self, client, mock_session_ownership):
        event = asyncio.Event()
        _pending_ui_results[str(SESSION_ID)] = event

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/ui-command-results",
            json={
                "results": [
                    {"id": "click-1", "name": "click", "success": False, "error": "Element not found: .missing-btn"},
                ],
            },
        )
        assert resp.status_code == 200
        assert _ui_command_results[str(SESSION_ID)][0]["error"] == "Element not found: .missing-btn"
        assert _ui_command_results[str(SESSION_ID)][0]["success"] is False

    def test_submit_with_cancelled_by_user(self, client, mock_session_ownership):
        event = asyncio.Event()
        _pending_ui_results[str(SESSION_ID)] = event

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/ui-command-results",
            json={
                "results": [
                    {"id": "nav-1", "name": "navigate", "success": False, "error": "cancelled_by_user"},
                ],
            },
        )
        assert resp.status_code == 200
        assert _ui_command_results[str(SESSION_ID)][0]["error"] == "cancelled_by_user"

    def test_submit_with_optional_fields(self, client, mock_session_ownership):
        event = asyncio.Event()
        _pending_ui_results[str(SESSION_ID)] = event

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/ui-command-results",
            json={
                "results": [
                    {"id": "nav-1", "name": "navigate", "success": True, "result": {"url": "/admin/config"}},
                ],
                "api_key": "sk-test-key",
                "system_prompt": "You are a helpful assistant.",
                "page_context": "/admin/users",
            },
        )
        assert resp.status_code == 200

    def test_no_pending_batch_returns_200(self, client, mock_session_ownership):
        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/ui-command-results",
            json={"results": []},
        )
        assert resp.status_code == 200

    def test_session_not_found_returns_404(self, client):
        mock_session_inst = AsyncMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin = MagicMock(return_value=begin_cm)
        mock_session_inst.get = AsyncMock(return_value=None)
        mock_session_inst.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=None)))

        async def _override_db():
            return mock_session_inst

        orig = app.dependency_overrides.get(get_db_session)
        app.dependency_overrides[get_db_session] = _override_db
        try:
            with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
                resp = client.post(
                    f"/api/v1/remy/sessions/{SESSION_ID}/ui-command-results",
                    json={"results": [{"id": "x", "name": "navigate", "success": True}]},
                )
                assert resp.status_code == 404
        finally:
            if orig is not None:
                app.dependency_overrides[get_db_session] = orig
            else:
                del app.dependency_overrides[get_db_session]

    def test_other_users_session_returns_404(self, client):
        other_session_id = uuid.uuid4()
        mock_session_inst = AsyncMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin = MagicMock(return_value=begin_cm)
        other_user_session = MagicMock()
        other_user_session.id = other_session_id
        other_user_session.user_id = uuid.uuid4()
        mock_session_inst.get = AsyncMock(return_value=other_user_session)
        mock_session_inst.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=None)))

        async def _override_db():
            return mock_session_inst

        orig = app.dependency_overrides.get(get_db_session)
        app.dependency_overrides[get_db_session] = _override_db
        try:
            with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
                resp = client.post(
                    f"/api/v1/remy/sessions/{other_session_id}/ui-command-results",
                    json={"results": []},
                )
                assert resp.status_code == 404
        finally:
            if orig is not None:
                app.dependency_overrides[get_db_session] = orig
            else:
                del app.dependency_overrides[get_db_session]


class TestResetPermissionsEndpoint:
    """POST /sessions/{id}/reset-permissions"""

    def test_reset_permissions(self, client, mock_session_ownership):
        _session_approvals[str(SESSION_ID)] = {
            "click": {
                "page_path": "/admin/users",
                "expires_at": "irrelevant",
            },
        }

        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/reset-permissions",
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert str(SESSION_ID) not in _session_approvals

    def test_reset_permissions_when_none_exist(self, client, mock_session_ownership):
        resp = client.post(
            f"/api/v1/remy/sessions/{SESSION_ID}/reset-permissions",
        )
        assert resp.status_code == 200

    def test_other_users_session_returns_404(self, client):
        other_session_id = uuid.uuid4()
        mock_session_inst = AsyncMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_inst.begin = MagicMock(return_value=begin_cm)
        other_session = MagicMock()
        other_session.id = other_session_id
        other_session.user_id = uuid.uuid4()
        mock_session_inst.get = AsyncMock(return_value=other_session)
        mock_session_inst.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=None)))

        async def _override_db():
            return mock_session_inst

        orig = app.dependency_overrides.get(get_db_session)
        app.dependency_overrides[get_db_session] = _override_db
        try:
            with patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock):
                resp = client.post(
                    f"/api/v1/remy/sessions/{other_session_id}/reset-permissions",
                )
                assert resp.status_code == 404
        finally:
            if orig is not None:
                app.dependency_overrides[get_db_session] = orig
            else:
                del app.dependency_overrides[get_db_session]

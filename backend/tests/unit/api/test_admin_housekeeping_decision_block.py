"""REST housekeeping cleanup surfaces decision-record blocks (FAR-1102 chunk 4).

Covers acceptance criterion 17 (unit-level half): the housekeeping cleanup
response model's error-dict convention includes a ``blocked_by`` key when a
policy_gate_decisions RESTRICT violation blocks a delete, and the error
message tells the operator to archive or purge first.

The full end-to-end shape (real pipeline with decision rows) runs in the
integration suite; here the FK rejection is driven through the actual REST
endpoint with a session whose execute() raises a decision-FK IntegrityError.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()

_URL = "/api/v1/admin/housekeeping/cleanup"


def _make_principal(role: str = "admin", *, is_system_admin: bool = True) -> TenantPrincipal:
    return TenantPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
        is_system_admin=is_system_admin,
    )


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.begin = MagicMock(return_value=MagicMock())
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=MagicMock())
    session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal("admin", is_system_admin=True)
    app.dependency_overrides[get_current_user] = lambda: _make_principal("admin", is_system_admin=True)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _decision_fk_error() -> IntegrityError:
    exc = IntegrityError(
        "DELETE FROM pipelines WHERE id = :id",
        {},
        Exception("update or delete on table violates foreign key constraint"),
    )
    exc.constraint_name = "fk_policy_gate_decisions_gate_org"
    return exc


def _unrelated_fk_error() -> IntegrityError:
    exc = IntegrityError(
        "DELETE FROM widgets WHERE id = :id",
        {},
        Exception("violates foreign key constraint"),
    )
    exc.constraint_name = "fk_unrelated_widget_owner"
    return exc


class TestCleanupDecisionRecordBlock:
    def test_decision_fk_surfaces_blocked_by(self, client: TestClient) -> None:
        """Criterion 17: the error-dict convention carries ``blocked_by``."""
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()
            session.execute = AsyncMock(side_effect=_decision_fk_error())

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(_URL, json={"items": [{"id": str(uuid.uuid4()), "entity_type": "pipeline"}]})

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 0
        errors: list[dict[str, str]] = body["errors"]
        assert len(errors) == 1
        error_entry = errors[0]
        assert error_entry["blocked_by"] == "policy_gate_decisions"
        assert "archive or purge" in error_entry["error"]
        assert "policy_gate_decisions" in error_entry["error"]

    def test_unrelated_fk_is_not_marked_as_decision_block(self, client: TestClient) -> None:
        """A non-decision FK violation keeps the generic error, no blocked_by."""
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()
            session.execute = AsyncMock(side_effect=_unrelated_fk_error())

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(_URL, json={"items": [{"id": str(uuid.uuid4()), "entity_type": "pipeline"}]})

        body = resp.json()
        errors: list[dict[str, Any]] = body["errors"]
        assert len(errors) == 1
        assert "blocked_by" not in errors[0]
        assert errors[0]["error"] == "Foreign key constraint violation"

    def test_non_integrity_failure_is_not_marked_as_decision_block(self, client: TestClient) -> None:
        """Only IntegrityError is classified as a decision-record block."""
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()
            session.execute = AsyncMock(side_effect=RuntimeError("connection reset"))

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(_URL, json={"items": [{"id": str(uuid.uuid4()), "entity_type": "pipeline"}]})

        assert resp.status_code == 500

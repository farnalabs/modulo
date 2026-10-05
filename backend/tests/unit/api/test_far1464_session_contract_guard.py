"""FAR-1464 â€” a session-contract violation must surface as 500, never 503/db_transient.

Production defect (do not re-investigate): route-local ``except
SQLAlchemyError`` arms never reach ``handle_db_errors``, so an
``InvalidRequestError`` (a client-side session-contract violation â€” e.g. a
query issued outside the transaction on the ``autobegin=False`` DI session)
was caught by the local arm and reported as ``503 "Database temporarily
unavailable."`` with ``reason=db_transient``: a retry-inviting outage reply
for a non-retryable programming bug (the FAR-1408 class, un-fixed across the
route layer until FAR-1464).

These tests lock three things:

1. ``raise_session_contract_error`` (the shared guard) raises 500 with
   ``MSG_SESSION_CONTRACT`` for ``InvalidRequestError``/``MissingGreenlet``
   and RETURNS for everything else so each arm's own 503 handling continues
   unchanged â€” including ``PendingRollbackError``, the transient subclass.
2. A representative converted arm that RAISES its 503
   (``admin_create_team`` -> ``_raise_db_temporarily_unavailable``):
   InvalidRequestError -> 500, OperationalError -> 503.
3. A representative converted arm that RETURNS a 503 response
   (``list_feature_flags`` -> ``JSONResponse(status_code=503)``): same split.

Fail-before evidence: with the guard absent from the arm (pre-FAR-1464
admin.py), tests 2/3 answer 503 for InvalidRequestError and these tests FAIL;
with the guard they pass (see PR description for both runs).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    MissingGreenlet,
    OperationalError,
    PendingRollbackError,
)

from modulo.api.db_error_handling import MSG_SESSION_CONTRACT, raise_session_contract_error
from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_DB_ERROR_REPORTING = "modulo.api.db_error_reporting"
_DB_ERROR_HANDLING = "modulo.api.db_error_handling"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _invalid_request_error() -> InvalidRequestError:
    """The FAR-1408/FAR-1464 misclassification: autobegin misuse on the DI session."""
    return InvalidRequestError("Autobegin is disabled on this Session; please call session.begin()")


def _operational_error() -> OperationalError:
    """A GENUINE transient database fault â€” must keep answering 503."""
    return OperationalError("select 1", {}, Exception("server closed the connection unexpectedly"))


def _begin_failing_session(exc: Exception) -> AsyncMock:
    """Session whose transaction entry raises â€” the route's first DB touch."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=exc)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _ok_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _build_client(session: AsyncMock) -> TestClient:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=True,
    )
    return TestClient(app)


@pytest.fixture
def cleanup_overrides() -> Generator[None, None, None]:
    yield
    app.dependency_overrides.clear()


def _service_unavailable_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _DB_ERROR_REPORTING]


class TestRaiseSessionContractErrorGuard:
    """The shared guard's contract (FAR-1464 Step 5, helper-level)."""

    def test_invalid_request_error_raises_500_with_the_classifier_detail(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as excinfo:
            raise_session_contract_error(_invalid_request_error(), "test.route_handler")

        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert excinfo.value.detail == MSG_SESSION_CONTRACT
        # logged under the ONE classifier key, from the shared module
        messages = [r.getMessage() for r in caplog.records if r.name == _DB_ERROR_HANDLING]
        assert "test.route_handler.session_contract_error" in messages, messages
        # ...and NEVER filed as a database outage
        records = _service_unavailable_records(caplog)
        assert not records, f"a programming error must not write a service_unavailable record: {records}"

    def test_missing_greenlet_raises_500_not_503(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as excinfo:
            raise_session_contract_error(MissingGreenlet("MissingGreenlet can only be used..."), "test.mg")

        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert excinfo.value.detail == MSG_SESSION_CONTRACT

    def test_operational_error_returns_so_the_arm_keeps_503ing(self) -> None:
        """Genuine transient faults fall through to the caller's own 503 arm."""
        assert raise_session_contract_error(_operational_error(), "test.transient") is None

    def test_pending_rollback_error_returns_so_the_arm_keeps_503ing(self) -> None:
        """The TRANSIENT InvalidRequestError subclass must NOT become a 500."""
        assert raise_session_contract_error(PendingRollbackError("rolled back"), "test.pre") is None

    def test_integrity_error_returns_to_the_arms_own_handling(self) -> None:
        assert raise_session_contract_error(IntegrityError("stmt", {}, Exception("dup")), "test.integrity") is None


class TestAdminCreateTeamArmGuard:
    """POST /api/v1/admin/teams â€” arm RAISES its 503 via a module helper."""

    URL = "/api/v1/admin/teams"

    def test_invalid_request_error_is_500_not_503(
        self, cleanup_overrides: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _build_client(_begin_failing_session(_invalid_request_error()))
        with caplog.at_level(logging.ERROR):
            resp = client.post(self.URL, json={"name": "guard-test-team"})

        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR, resp.text
        assert resp.json()["detail"] == MSG_SESSION_CONTRACT
        records = _service_unavailable_records(caplog)
        assert not records, f"session-contract 500 must not file a db_transient record: {records}"

    def test_operational_error_still_surfaces_as_503(
        self, cleanup_overrides: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _build_client(_begin_failing_session(_operational_error()))
        with caplog.at_level(logging.ERROR):
            resp = client.post(self.URL, json={"name": "guard-test-team"})

        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, resp.text
        detail = resp.json()["detail"]
        assert detail != MSG_SESSION_CONTRACT
        assert "temporarily" in detail.lower() or "try again" in detail.lower(), detail


class TestFeatureFlagsListArmGuard:
    """GET /api/v1/admin/feature-flags â€” arm RETURNS a JSONResponse(503)."""

    URL = "/api/v1/admin/feature-flags"

    def test_invalid_request_error_is_500_not_503(
        self, cleanup_overrides: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _build_client(_ok_session())
        with (
            patch(
                "modulo.api.routes.admin_feature_flags._build_registry",
                new=AsyncMock(side_effect=_invalid_request_error()),
            ),
            caplog.at_level(logging.ERROR),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR, resp.text
        assert resp.json()["detail"] == MSG_SESSION_CONTRACT
        records = _service_unavailable_records(caplog)
        assert not records, f"session-contract 500 must not file a db_transient record: {records}"

    def test_operational_error_still_surfaces_as_503(self, cleanup_overrides: None) -> None:
        client = _build_client(_ok_session())
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            new=AsyncMock(side_effect=_operational_error()),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, resp.text
        body = resp.json()
        assert body["error"]["code"] == "SERVICE_UNAVAILABLE", body

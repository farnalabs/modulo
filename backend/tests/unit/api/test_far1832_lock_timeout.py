"""FAR-1832 QA F2: a bounded lock timeout (SQLSTATE 55P03) is a 409 conflict.

The org-settings patch bounds every converted writer's row-lock wait
(``set_mutation_row_lock_timeout``), so SQLSTATE 55P03 is now REACHABLE on the
admin / admin-orgs / product-analytics routes. Before F2 their local except
arms collapsed it into a generic 503 ("Database temporarily unavailable" —
which invites a client retry storm against the busy lock) or a 500. These
tests pin the shared classifier at each boundary:

- 55P03 -> 409 with the canonical "another change is in progress" detail;
- a genuine connection/unavailability error -> the pre-existing 503;
- the admin-orgs boundary keeps every other error at its pre-existing 503.

The real in-flight-lock-holder variants (real Postgres, real handlers) live in
``tests/integration/crud/test_org_settings_patch.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from modulo.api.db_error_handling import raise_lock_timeout_conflict_if
from modulo.api.dependencies import get_db_session
from modulo.api.routes import product_analytics as pa_module
from modulo.api.routes.admin import _run_admin_rls_txn
from modulo.api.routes.product_analytics import post_consent
from modulo.auth.jwt import TenantPrincipal

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_CONSENT_URL = "/api/v1/org/product-analytics/consent"

_LOCK_TIMEOUT_DETAIL = "another change is in progress"

app = FastAPI()
app.include_router(pa_module.router)


class _PgError(Exception):
    """Minimal driver-error double carrying a SQLSTATE (asyncpg ``sqlstate``)."""

    def __init__(self, state: str) -> None:
        super().__init__(f"driver error {state}")
        self.sqlstate = state


def _operational(state: str | None) -> OperationalError:
    orig = _PgError(state) if state else Exception("server closed the connection unexpectedly")
    return OperationalError("UPDATE organisations ...", {}, orig)


# ---------------------------------------------------------------------------
# The shared classifier itself
# ---------------------------------------------------------------------------


def test_classifier_maps_55p03_to_409() -> None:
    with pytest.raises(HTTPException) as exc_info:
        raise_lock_timeout_conflict_if(_operational("55P03"), "unit.classifier")
    assert exc_info.value.status_code == 409
    assert _LOCK_TIMEOUT_DETAIL in str(exc_info.value.detail)


def test_classifier_returns_for_a_genuine_connection_error() -> None:
    # Returns (no raise) so the caller's own 503 handling runs unchanged.
    assert raise_lock_timeout_conflict_if(_operational("08006"), "unit.classifier") is None


def test_classifier_returns_for_a_plain_sqlalchemy_error() -> None:
    assert raise_lock_timeout_conflict_if(SQLAlchemyError("boom"), "unit.classifier") is None


# ---------------------------------------------------------------------------
# admin._run_admin_rls_txn (sandbox concurrency / HITL window / flags writers)
# ---------------------------------------------------------------------------


def _admin_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _body_raising(exc: Exception):
    async def _boom() -> None:
        raise exc

    return _boom


async def test_run_admin_rls_txn_maps_lock_timeout_to_409() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _run_admin_rls_txn(
            MagicMock(),
            _admin_principal(),
            _body_raising(_operational("55P03")),
            detail_admin="admin only",
        )
    assert exc_info.value.status_code == 409
    assert _LOCK_TIMEOUT_DETAIL in str(exc_info.value.detail)


async def test_run_admin_rls_txn_keeps_genuine_db_errors_at_503() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _run_admin_rls_txn(
            MagicMock(),
            _admin_principal(),
            _body_raising(_operational("08006")),
            detail_admin="admin only",
        )
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# product_analytics consent route (real handler, failing session)
# ---------------------------------------------------------------------------


def _principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="tester",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _failing_session(exc: Exception) -> AsyncMock:
    session = AsyncMock()

    @asynccontextmanager
    async def _begin() -> AsyncGenerator[None, None]:
        yield

    session.begin = _begin  # type: ignore[assignment]
    session.execute = AsyncMock(side_effect=exc)
    return session


@contextmanager
def _client_overrides(session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncMock:
        yield session

    with (
        patch.object(pa_module, "set_rls_org", new=AsyncMock()),
        patch.object(pa_module, "append_audit_event", new=AsyncMock()),
        patch.object(pa_module, "is_instance_analytics_enabled", new=AsyncMock(return_value=False)),
    ):
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[pa_module.get_current_tenant_user] = _principal
        yield TestClient(app)
        app.dependency_overrides.clear()


def test_consent_route_maps_lock_timeout_to_409() -> None:
    with _client_overrides(_failing_session(_operational("55P03"))) as client:
        resp = client.post(_CONSENT_URL, json={"action": "accept"})
    assert resp.status_code == 409, resp.text
    assert _LOCK_TIMEOUT_DETAIL in resp.json()["detail"]


def test_consent_route_maps_genuine_db_outage_to_503() -> None:
    """A genuine connection/unavailability error re-raises to
    ``handle_db_errors``, which answers 503 — never the old blanket 500."""
    with _client_overrides(_failing_session(_operational("08006"))) as client:
        resp = client.post(_CONSENT_URL, json={"action": "accept"})
    assert resp.status_code == 503, resp.text


async def test_post_consent_direct_call_maps_lock_timeout_to_409() -> None:
    """The handler's own except chain (inside @handle_db_errors) classifies
    55P03 — asserted on a direct coroutine call too."""
    session = _failing_session(_operational("55P03"))
    with (
        patch.object(pa_module, "set_rls_org", new=AsyncMock()),
        pytest.raises(HTTPException) as exc_info,
    ):
        await post_consent.__wrapped__(  # type: ignore[attr-defined]
            pa_module.ConsentRequest(action="accept"), _principal(), session
        )
    assert exc_info.value.status_code == 409

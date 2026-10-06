"""Unit tests for the FAR-1287 Part 2 snapshot-lock operator endpoints.

``GET/POST /api/v1/admin/pipelines/{pipeline_id}/snapshot-lock[/release]``:
strict system-admin gating, the diagnostic's holder payload, the release's
idempotent success, the typed privilege refusal, and the audit event.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.pipeline_snapshot import SnapshotLockTerminateDeniedError
from modulo.settings import Settings, get_settings
from tests.unit.api.route_introspection import get_all_apiroutes

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
_VALID_32 = "a" * 32

_DIAGNOSTIC_PATH = f"/api/v1/admin/pipelines/{_PIPELINE_ID}/snapshot-lock"
_RELEASE_PATH = f"{_DIAGNOSTIC_PATH}/release"
_DIAGNOSTIC_ROUTE = "/api/v1/admin/pipelines/{pipeline_id}/snapshot-lock"
_RELEASE_ROUTE = f"{_DIAGNOSTIC_ROUTE}/release"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_principal(*, is_system_admin: bool, organisation_id: uuid.UUID | None = _ORG_ID) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin" if is_system_admin else "viewer",
        organisation_id=organisation_id,
        account_id=_USER_ID,
        org_role="admin" if is_system_admin else "viewer",
        is_system_admin=is_system_admin,
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _client_for(principal: AuthenticatedPrincipal, session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def override_plan_context() -> MagicMock:
        return MagicMock()

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: principal
    app.dependency_overrides[get_plan_context] = override_plan_context
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    yield from _client_for(_make_principal(is_system_admin=True), _make_session())


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    yield from _client_for(_make_principal(is_system_admin=False), _make_session())


@pytest.fixture
def orgless_admin_client() -> Generator[TestClient, None, None]:
    yield from _client_for(_make_principal(is_system_admin=True, organisation_id=None), _make_session())


def _held_payload() -> dict[str, object]:
    return {
        "held": True,
        "holders": [
            {
                "pid": 4242,
                "application_name": "modulo",
                "state": "idle",
                "backend_start": datetime(2026, 1, 1, tzinfo=UTC),
                "query_start": datetime(2026, 1, 1, tzinfo=UTC),
                "granted": True,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------


def test_diagnostic_reports_held_lock_for_a_system_admin(admin_client: TestClient) -> None:
    with patch("modulo.api.routes.admin.inspect_snapshot_lock", new=AsyncMock(return_value=_held_payload())) as probe:
        resp = admin_client.get(_DIAGNOSTIC_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["held"] is True
    assert [holder["pid"] for holder in body["holders"]] == [4242]
    assert body["holders"][0]["application_name"] == "modulo"
    assert body["holders"][0]["granted"] is True
    probe.assert_awaited_once()
    assert probe.await_args.args[1] == _PIPELINE_ID


def test_diagnostic_reports_a_free_lock(admin_client: TestClient) -> None:
    free = {"held": False, "holders": []}
    with patch("modulo.api.routes.admin.inspect_snapshot_lock", new=AsyncMock(return_value=free)):
        resp = admin_client.get(_DIAGNOSTIC_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["held"] is False
    assert not body["holders"]


def test_diagnostic_requires_system_admin(viewer_client: TestClient) -> None:
    with patch("modulo.api.routes.admin.inspect_snapshot_lock", new=AsyncMock()) as probe:
        resp = viewer_client.get(_DIAGNOSTIC_PATH)

    assert resp.status_code == 403
    assert "requires system admin role" in resp.json()["detail"]
    probe.assert_not_awaited()


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_release_terminates_holders_and_emits_the_audit_event(admin_client: TestClient) -> None:
    terminate = AsyncMock(return_value={"released": 1, "pids": [4242]})
    audit = AsyncMock()
    with (
        patch("modulo.api.routes.admin.terminate_snapshot_lock_holders", new=terminate),
        patch("modulo.api.routes.admin._audit_logger.append_audit_event_isolated", new=audit),
    ):
        resp = admin_client.post(_RELEASE_PATH)

    assert resp.status_code == 200
    assert resp.json() == {"released": 1, "pids": [4242]}
    terminate.assert_awaited_once()

    audit.assert_awaited_once()
    audit_kwargs = audit.await_args.kwargs
    assert audit_kwargs["event_type"] == "pipeline_snapshot_lock_released"
    assert audit_kwargs["resource_type"] == "pipeline"
    assert audit_kwargs["resource_id"] == _PIPELINE_ID
    assert audit_kwargs["payload"]["released"] == 1
    assert audit_kwargs["payload"]["pids"] == [4242]


def test_release_with_no_holders_is_an_idempotent_success(admin_client: TestClient) -> None:
    terminate = AsyncMock(return_value={"released": 0, "pids": []})
    audit = AsyncMock()
    with (
        patch("modulo.api.routes.admin.terminate_snapshot_lock_holders", new=terminate),
        patch("modulo.api.routes.admin._audit_logger.append_audit_event_isolated", new=audit),
    ):
        resp = admin_client.post(_RELEASE_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["released"] == 0
    assert not body["pids"]
    # Still audited: an operator asked for the clear path and got a definitive
    # "nothing was held" answer.
    audit.assert_awaited_once()


def test_release_maps_a_privilege_refusal_to_a_typed_403(admin_client: TestClient) -> None:
    terminate = AsyncMock(side_effect=SnapshotLockTerminateDeniedError("42501"))
    audit = AsyncMock()
    with (
        patch("modulo.api.routes.admin.terminate_snapshot_lock_holders", new=terminate),
        patch("modulo.api.routes.admin._audit_logger.append_audit_event_isolated", new=audit),
    ):
        resp = admin_client.post(_RELEASE_PATH)

    assert resp.status_code == 403
    body = resp.json()
    assert body["type"] == "urn:problem:modulo:forbidden"
    assert "pg_signal_backend" in body["detail"]
    # Refused before anything was terminated or recorded.
    audit.assert_not_awaited()


def test_release_requires_system_admin(viewer_client: TestClient) -> None:
    terminate = AsyncMock()
    with patch("modulo.api.routes.admin.terminate_snapshot_lock_holders", new=terminate):
        resp = viewer_client.post(_RELEASE_PATH)

    assert resp.status_code == 403
    assert "requires system admin role" in resp.json()["detail"]
    terminate.assert_not_awaited()


def test_release_without_an_organisation_context_is_refused(orgless_admin_client: TestClient) -> None:
    """Fail closed: a destructive operator action that cannot reach an audit
    chain is not performed."""
    terminate = AsyncMock()
    audit = AsyncMock()
    with (
        patch("modulo.api.routes.admin.terminate_snapshot_lock_holders", new=terminate),
        patch("modulo.api.routes.admin._audit_logger.append_audit_event_isolated", new=audit),
    ):
        resp = orgless_admin_client.post(_RELEASE_PATH)

    assert resp.status_code == 403
    assert "audit chain" in resp.json()["detail"]
    terminate.assert_not_awaited()
    audit.assert_not_awaited()


def test_routes_are_registered_under_the_system_admin_prefix() -> None:
    """Path contract: both routes live under ``/api/v1/admin/pipelines/...``."""
    registered = {(method, route.path) for route in get_all_apiroutes(app) for method in route.methods}
    assert ("GET", _DIAGNOSTIC_ROUTE) in registered
    assert ("POST", _RELEASE_ROUTE) in registered

"""Route-level unit coverage for the admin evidence-retention endpoints (FAR-961).

Covers the three ``/api/v1/admin/evidence-retention`` handlers (read, update,
purge) and the shared ``_resolve_org_id`` helper at the unit tier so the
changed-lines coverage gate measures them from the backend unit report (the
companion ``tests/integration/db/test_evidence_retention.py`` exercises the
purge sweep against real Postgres, but integration coverage is not part of the
gate's report — the sibling ``test_policy_gate_routes_coverage.py`` docstring
documents this exact gap shape).

Exercised here: the admin gate (422 missing-target / 403 org mismatch), the
typed error-mapping arms — ProgrammingError 501, SQLAlchemyError 503,
IntegrityError 409, ValueError 404, unexpected 500 — and the success bodies.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

import modulo.api.routes.admin_evidence_retention as retention_routes
from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.evidence_retention import (
    EvidenceRetentionPolicy,
    PurgeResult,
)
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_BASE_URL = "/api/v1/admin/evidence-retention"

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")
_INTEGRITY = IntegrityError("s", {}, Exception())
_VALUE_ERROR = ValueError(f"Organisation {_ORG_ID} not found")

_MSG_501 = "Feature is not available. Run database migrations to enable it."
_MSG_503 = "Database temporarily unavailable."
_MSG_500 = "An unexpected error occurred."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


class _BeginRaiser:
    """Async CM whose ``__aenter__`` raises the given exception.

    Every evidence-retention handler wraps its DB work in
    ``try: async with session.begin(): ...``, so a raise surfaced inside that
    transaction (at begin or from the wrapped CRUD calls) exercises the
    handler's error-mapping arms without touching query plumbing.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_session(begin_exc: Exception | None = None) -> AsyncMock:
    session = AsyncMock()
    session.flush = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    if begin_exc is not None:
        session.begin = MagicMock(return_value=_BeginRaiser(begin_exc))
        return session
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_principal(*, role: str = "admin", is_system_admin: bool = False) -> TenantPrincipal:
    return TenantPrincipal(
        username=f"{role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
        is_system_admin=is_system_admin,
    )


def _install_overrides(session: AsyncMock, principal: TenantPrincipal | None = None) -> None:
    if principal is None:
        principal = _make_principal()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant_user] = lambda: principal


@pytest.fixture(autouse=True)
def _patch_route_side_effects() -> Generator[None, None, None]:
    """Stub the RLS helper the handlers call inside every transaction.

    The real ``set_rls_org`` issues ``set_config`` statements through
    ``session.execute``; patching it at the route boundary keeps fake-session
    tests free of RLS plumbing.
    """
    with patch.object(retention_routes, "set_rls_org", new_callable=AsyncMock):
        yield


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    _install_overrides(session)
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _fake_policy() -> EvidenceRetentionPolicy:
    return EvidenceRetentionPolicy(max_age_days=120, max_rows=10, batch_size=200, lock_timeout_seconds=44.0)


def _fake_purge_result() -> PurgeResult:
    return PurgeResult(rows_deleted=7, batches=2, max_age_days=120, max_rows=10)


# ---------------------------------------------------------------------------
# GET /api/v1/admin/evidence-retention
# ---------------------------------------------------------------------------


def test_get_policy_success_returns_current_values(client: tuple[TestClient, AsyncMock]) -> None:
    http, _ = client
    with (
        patch.object(retention_routes, "load_policy", new_callable=AsyncMock, return_value=_fake_policy()),
        patch.object(retention_routes, "count_evidence_rows", new_callable=AsyncMock, return_value=42),
    ):
        resp = http.get(_BASE_URL)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_age_days"] == 120
    assert body["max_rows"] == 10
    assert body["batch_size"] == 200
    assert body["lock_timeout_seconds"] == 44.0
    assert body["current_row_count"] == 42


def test_get_policy_org_admin_mismatched_org_returns_403(client: tuple[TestClient, AsyncMock]) -> None:
    http, _ = client

    resp = http.get(_BASE_URL, params={"organisation_id": str(_OTHER_ORG_ID)})

    assert resp.status_code == 403, resp.text
    assert "Org admins may only operate on their own organisation" in resp.json()["detail"]


def test_get_policy_system_admin_missing_target_returns_422() -> None:
    session = _make_session()
    _install_overrides(session, principal=_make_principal(role="admin", is_system_admin=True))
    try:
        resp = TestClient(app).get(_BASE_URL)
        assert resp.status_code == 422, resp.text
        assert "organisation_id is required" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("exc", "expected", "detail"),
    [(_PROG, 501, _MSG_501), (_SQL, 503, _MSG_503), (_RUNTIME, 500, _MSG_500)],
    ids=["programming-501", "sqlalchemy-503", "unexpected-500"],
)
def test_get_policy_error_mapping(exc: Exception, expected: int, detail: str) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        resp = TestClient(app).get(_BASE_URL)
        assert resp.status_code == expected, resp.text
        assert resp.json()["detail"] == detail
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# PUT /api/v1/admin/evidence-retention
# ---------------------------------------------------------------------------


def test_update_policy_success_round_trips_the_new_policy(client: tuple[TestClient, AsyncMock]) -> None:
    http, _ = client
    save = AsyncMock(return_value=None)
    count = AsyncMock(return_value=13)
    with (
        patch.object(retention_routes, "save_policy", save),
        patch.object(retention_routes, "count_evidence_rows", count),
    ):
        resp = http.put(_BASE_URL, json={"max_age_days": 30, "max_rows": 1000, "batch_size": 100})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_age_days"] == 30
    assert body["max_rows"] == 1000
    assert body["batch_size"] == 100
    assert body["current_row_count"] == 13
    assert save.await_args is not None
    saved: EvidenceRetentionPolicy = save.await_args.args[2]
    assert saved.max_age_days == 30
    assert saved.max_rows == 1000


def test_update_policy_missing_org_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _ = client
    with patch.object(retention_routes, "save_policy", new_callable=AsyncMock, side_effect=_VALUE_ERROR):
        resp = http.put(_BASE_URL, json={"max_age_days": 30})

    assert resp.status_code == 404, resp.text
    assert str(_ORG_ID) in resp.json()["detail"]


@pytest.mark.parametrize(
    ("exc", "expected", "detail"),
    [(_INTEGRITY, 409, "Resource conflict. The policy could not be updated."), (_PROG, 501, _MSG_501)],
    ids=["integrity-409", "programming-501"],
)
def test_update_policy_error_mapping(exc: Exception, expected: int, detail: str) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        resp = TestClient(app).put(_BASE_URL, json={"max_age_days": 30})
        assert resp.status_code == expected, resp.text
        assert resp.json()["detail"] == detail
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("exc", "expected", "detail"),
    [(_SQL, 503, _MSG_503), (_RUNTIME, 500, _MSG_500)],
    ids=["sqlalchemy-503", "unexpected-500"],
)
def test_update_policy_db_and_unexpected_error_mapping(exc: Exception, expected: int, detail: str) -> None:
    """SQLAlchemyError and generic exceptions surface from the flush path, not begin.

    ``save_policy`` flushes inside the caller's transaction, so a 503/500 on
    update is exercised at the flush call site rather than a begin-time raise.
    """
    session = _make_session()
    _install_overrides(session)
    try:
        with patch.object(retention_routes, "save_policy", new_callable=AsyncMock, side_effect=exc):
            resp = TestClient(app).put(_BASE_URL, json={"max_age_days": 30})
        assert resp.status_code == expected, resp.text
        assert resp.json()["detail"] == detail
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# except HTTPException: raise — passthrough arms (one per handler)
# ---------------------------------------------------------------------------


def test_get_policy_passthrough_reraises_http_exception(client: tuple[TestClient, AsyncMock]) -> None:
    """An HTTPException raised inside the transaction passes through un-remapped.

    The app-level problem handler normalises the 418 status, but it preserves
    the original HTTPException detail. If the ``except HTTPException: raise``
    arm were deleted, the ``except Exception`` fallback would instead replace
    the detail with the generic ``MSG_UNEXPECTED_ERROR`` — so the detail is
    the discriminating assertion.
    """
    from fastapi import HTTPException

    http, _ = client
    with patch.object(retention_routes, "load_policy", new_callable=AsyncMock, side_effect=HTTPException(418)):
        resp = http.get(_BASE_URL)

    assert "I'm a Teapot" in resp.text


def test_update_policy_passthrough_reraises_http_exception(client: tuple[TestClient, AsyncMock]) -> None:
    from fastapi import HTTPException

    http, _ = client
    with patch.object(retention_routes, "save_policy", new_callable=AsyncMock, side_effect=HTTPException(418)):
        resp = http.put(_BASE_URL, json={"max_age_days": 30})

    assert "I'm a Teapot" in resp.text


def test_purge_passthrough_reraises_http_exception(client: tuple[TestClient, AsyncMock]) -> None:
    from fastapi import HTTPException

    http, _ = client
    with patch.object(retention_routes, "purge_evidence", new_callable=AsyncMock, side_effect=HTTPException(418)):
        resp = http.post(_BASE_URL + "/purge")

    assert "I'm a Teapot" in resp.text


# ---------------------------------------------------------------------------
# POST /api/v1/admin/evidence-retention/purge
# ---------------------------------------------------------------------------


def test_purge_success_returns_purge_result_fields(client: tuple[TestClient, AsyncMock]) -> None:
    http, _ = client
    with (
        patch.object(retention_routes, "purge_evidence", new_callable=AsyncMock, return_value=_fake_purge_result()),
        patch.object(retention_routes, "load_policy", new_callable=AsyncMock, return_value=_fake_policy()),
        patch.object(retention_routes, "count_evidence_rows", new_callable=AsyncMock, return_value=0),
    ):
        resp = http.post(_BASE_URL + "/purge")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows_deleted"] == 7
    assert body["batches"] == 2
    assert body["max_age_days"] == 120
    assert body["max_rows"] == 10


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(_SQL, 503), (_PROG, 501), (_RUNTIME, 500)],
    ids=["sqlalchemy-503", "programming-501", "unexpected-500"],
)
def test_purge_error_mapping(exc: Exception, expected: int) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        resp = TestClient(app).post(_BASE_URL + "/purge")
        assert resp.status_code == expected, resp.text
    finally:
        app.dependency_overrides.clear()


def test_purge_success_passes_org_id_to_the_sweep(client: tuple[TestClient, AsyncMock]) -> None:
    """The purge call site must pass the resolved org id, not None."""
    http, _ = client
    sweep = AsyncMock(return_value=_fake_purge_result())
    with (
        patch.object(retention_routes, "purge_evidence", sweep),
        patch.object(retention_routes, "load_policy", new_callable=AsyncMock, return_value=_fake_policy()),
        patch.object(retention_routes, "count_evidence_rows", new_callable=AsyncMock, return_value=0),
    ):
        resp = http.post(_BASE_URL + "/purge")

    assert resp.status_code == 200, resp.text
    assert sweep.await_args is not None
    org_id_arg: object = sweep.await_args.args[1]
    assert org_id_arg == _ORG_ID

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

import logging
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError

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


def _lock_timeout_operational() -> OperationalError:
    """An ``OperationalError`` whose driver error carries SQLSTATE 55P03."""

    class _PgError(Exception):
        sqlstate = "55P03"

    return OperationalError("UPDATE organisations ...", {}, _PgError("canceling statement due to lock timeout"))


def test_update_policy_lock_timeout_is_409(client: tuple[TestClient, AsyncMock]) -> None:
    """FAR-1832 QA F16: ``save_policy`` writes through the bounded-lock
    org-settings patch, so 55P03 is reachable on this route. Pre-F16 the local
    ``except SQLAlchemyError`` arm collapsed it to a 503 outage reply.

    FAR-1905 added a ``@handle_db_errors`` backstop to this route, so the arm is
    no longer the only translation — but the arm still PRE-EMPTS it (it raises an
    ``HTTPException``, which the backstop re-raises unchanged). The companion
    ``test_update_policy_lock_timeout_arm_pre_empts_backstop`` pins that
    precedence directly."""
    http, _ = client
    with patch.object(retention_routes, "save_policy", new_callable=AsyncMock, side_effect=_lock_timeout_operational()):
        resp = http.put(_BASE_URL, json={"max_age_days": 30})

    assert resp.status_code == 409, resp.text
    assert "another change is in progress" in resp.json()["detail"]


def test_update_policy_lock_timeout_arm_pre_empts_backstop(
    client: tuple[TestClient, AsyncMock], caplog: pytest.LogCaptureFixture
) -> None:
    """FAR-1905: the PUT arm's own 55P03 -> 409 verdict wins over the backstop.

    The ORIGINAL FAR-1905 acceptance criterion — "the 55P03 -> 409 classification
    on the PUT arm still works and is not shadowed by ``@handle_db_errors``" — was
    previously unverifiable because FAR-1832/PR #1899 had not landed. It has now,
    so this test pins it against the merged file.

    Non-vacuity: the arm and the backstop produce the SAME status (409) and the
    SAME detail (``_MSG_LOCK_TIMEOUT``), so status/detail alone cannot tell the
    two paths apart. The discriminator is the emitted log key — the arm calls the
    classifier with its own route key (``admin_evidence_retention.…``), whereas
    the backstop would log the dotted prefix it was constructed with
    (``admin.evidence.retention.…``). This test asserts the arm's key fired AND
    the backstop's did NOT, plus a wraps-spy proving the arm's classifier ran.

    Fails if (a) the arm's ``raise_lock_timeout_conflict_if`` call is removed —
    the raw error then reaches the backstop, which still answers 409 with the
    same detail, but logs the dotted key and never invokes the arm's classifier;
    or (b) the backstop is ever changed to remap a propagating ``HTTPException``
    instead of re-raising it — the arm's detail would no longer survive.
    """
    http, _ = client
    real_classifier = retention_routes.raise_lock_timeout_conflict_if
    arm_calls: list[str] = []

    def _spy(exc: SQLAlchemyError, log_key: str) -> None:
        arm_calls.append(log_key)
        real_classifier(exc, log_key)

    with (
        caplog.at_level(logging.WARNING),
        patch.object(retention_routes, "raise_lock_timeout_conflict_if", new=_spy),
        patch.object(retention_routes, "save_policy", new_callable=AsyncMock, side_effect=_lock_timeout_operational()),
    ):
        resp = http.put(_BASE_URL, json={"max_age_days": 30})

    assert resp.status_code == 409, resp.text
    assert "another change is in progress" in resp.json()["detail"]

    # The arm's own classifier ran, with the arm's own route key — i.e. the arm
    # handled the 55P03 and the backstop never had to.
    assert arm_calls == ["admin_evidence_retention.update_evidence_retention"]

    messages = [record.getMessage() for record in caplog.records]
    assert "admin_evidence_retention.update_evidence_retention.lock_timeout" in messages
    assert "admin.evidence.retention.update_evidence_retention.lock_timeout" not in messages


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


# ---------------------------------------------------------------------------
# @handle_db_errors backstop (FAR-1905)
# ---------------------------------------------------------------------------
#
# The three handlers already have hand-written ``except`` arms that translate
# the DB errors raised INSIDE their ``try`` block (ProgrammingError -> 501,
# SQLAlchemyError -> 503, IntegrityError -> 409 on update, Exception -> 500),
# so a DB error raised in-transaction is mapped with or without the backstop.
# The backstop's distinguishing job is to catch a DB error that ESCAPES the
# handler's own arms — the "unexpected DB error relies solely on its
# hand-written arms" gap in FAR-1905. ``_resolve_org_id`` is the one seam each
# handler calls BEFORE its ``try`` block, so raising from it is the faithful
# way to exercise exactly that class of escape (there is no DB call outside the
# handler's own ``try``, which is why the hand-written arms cannot cover it).


def _issue(client: TestClient, method: str, url: str, body: dict[str, int] | None) -> httpx.Response:
    """Issue one request for a parametrised route (keeps ``**kwargs`` out of mypy)."""
    if method == "get":
        return client.get(url)
    if method == "put":
        return client.put(url, json=body)
    return client.post(url)


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("get", _BASE_URL, None),
        ("put", _BASE_URL, {"max_age_days": 30}),
        ("post", _BASE_URL + "/purge", None),
    ],
    ids=["get", "put", "purge"],
)
@pytest.mark.parametrize(
    ("exc", "expected", "detail"),
    [(_SQL, 503, _MSG_503), (_PROG, 501, _MSG_501)],
    ids=["sqlalchemy-503", "programming-501"],
)
def test_backstop_maps_escaping_db_error_to_standard_status(
    method: str, url: str, body: dict[str, int] | None, exc: Exception, expected: int, detail: str
) -> None:
    """FAR-1905 prove-the-fix: an unexpected DB error escaping the handler body.

    ``_resolve_org_id`` runs before each handler's ``try``, so a DB error raised
    from it is exactly the error class the hand-written arms do NOT cover. Only
    the ``@handle_db_errors`` backstop maps it: 503 for a generic
    ``SQLAlchemyError``, 501 for a ``ProgrammingError`` (the decorator's
    contract, matching the in-transaction arms' statuses). Without the
    decorator the error escapes to the app's catch-all handler, which answers
    500 with the generic internal-error detail — so this assertion fails.

    ``raise_server_exceptions=False`` lets that catch-all 500 be observed as a
    status code instead of TestClient re-raising it (the default).
    """
    session = _make_session()
    _install_overrides(session)
    try:
        with patch.object(retention_routes, "_resolve_org_id", side_effect=exc):
            resp = _issue(TestClient(app, raise_server_exceptions=False), method, url, body)
        assert resp.status_code == expected, resp.text
        assert resp.json()["detail"] == detail
    finally:
        app.dependency_overrides.clear()


def test_backstop_does_not_shadow_update_arm_conflict_verdict(client: tuple[TestClient, AsyncMock]) -> None:
    """The PUT arm's own IntegrityError -> 409 verdict survives the backstop.

    ``handle_db_errors`` maps ``IntegrityError`` -> 409 with its own detail
    ("Resource conflict. The operation could not be completed."). The route's
    hand-written arm raises ``HTTPException(409, "Resource conflict. The policy
    could not be updated.")`` first, inside its ``try``; the decorator re-raises
    an ``HTTPException`` unchanged, so the arm's exact detail must win. Asserting
    that specific detail (which differs from the backstop's) is the
    discriminating check that the new backstop did not shadow the route's own
    classification — the same precedence that preserves the PUT arm's
    lock-timeout (55P03 -> 409) handling once it is present.
    """
    session = _make_session(begin_exc=_INTEGRITY)
    _install_overrides(session)
    try:
        resp = TestClient(app).put(_BASE_URL, json={"max_age_days": 30})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "Resource conflict. The policy could not be updated."
    finally:
        app.dependency_overrides.clear()

"""Route-level unit coverage for the Policy Gate authoring endpoints (FAR-1106).

Covers the four ``/api/v1/evals/{eval_id}/policy-gate`` handlers (create,
update, delete, read) and their shared helpers at the unit tier so the
changed-lines coverage gate measures them from the backend unit report (the
companion ``tests/integration/api/test_policy_gate_acceptance.py`` exercises
the same contract against real Postgres, but integration coverage is not part
of the gate's report).

Exercised here: the admin gate, the org-scoped eval/gate 404s, the binding
400, the advisory-locked create/replace helper, the typed 409s, the
lock-timeout (SQLSTATE 57014) 503, the generic DB/exception mappings, and the
best-effort audit failure paths.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

import modulo.api.routes.evals as evals_routes
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_EVAL_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_NODE_ID = uuid.UUID("00000000-0000-0000-0000-000000000007")
_GATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000009")

_GATE_URL = f"/api/v1/evals/{_EVAL_ID}/policy-gate"

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")
_INTEGRITY = IntegrityError("s", {}, Exception())


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


class _BeginRaiser:
    """Async CM whose ``__aenter__`` raises the given exception.

    Every policy-gate handler wraps its DB work in
    ``try: async with session.begin(): ...``, so a begin-time raise exercises
    the handler's error-mapping arms without touching query plumbing.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *args: object) -> None:
        return None


class _CommitRaiser:
    """Async CM whose ``__aexit__`` raises — a commit-time failure.

    The delete handler's FK-RESTRICT ``IntegrityError`` surfaces when the
    transaction commits (the soft-delete UPDATE flushes on ``__aexit__``), not
    from a query, so a commit-time raise is the faithful simulation.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        raise self._exc


def _lock_timeout_exc() -> SQLAlchemyError:
    """A SQLAlchemy error whose driver error carries SQLSTATE 57014."""
    exc = SQLAlchemyError("canceling statement due to statement timeout")
    exc.orig = SimpleNamespace(sqlstate="57014")  # type: ignore[attr-defined]
    return exc


def _result(
    scalar_one_or_none: object = None,
    scalar: object = None,
    all_rows: list | None = None,
) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    r.scalar = MagicMock(return_value=scalar)
    r.scalars.return_value.all = MagicMock(return_value=all_rows if all_rows is not None else [])
    r.all = MagicMock(return_value=all_rows if all_rows is not None else [])
    return r


def _make_session(begin_exc: Exception | None = None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock(return_value=None)
    session.flush = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=_result())
    if begin_exc is not None:
        session.begin = MagicMock(return_value=_BeginRaiser(begin_exc))
        return session
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_session_commit_raiser(exc: Exception) -> AsyncMock:
    session = _make_session()
    session.begin = MagicMock(return_value=_CommitRaiser(exc))
    return session


def _queue_execute(session: AsyncMock, results: list[MagicMock]) -> None:
    """Route ``session.execute`` through a result queue, minus authz noise.

    ``require_permission`` issues a per-request kill-switch read on
    ``organisations.authz_enforce``; that read is answered with an empty
    result and never consumes the queued results.
    """
    authz_result = _result()

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        if "authz_enforce" in str(stmt):
            return authz_result
        if not results:
            raise AssertionError("Unexpected session.execute(): the result queue is exhausted")
        item = results.pop(0)
        if isinstance(item, Exception):
            # An exception queued for a handler-body execute (not begin-time),
            # so the handler's own error-mapping arm is exercised rather than
            # the authz dependency's fail-closed SQLAlchemyError guard.
            raise item
        return item

    session.execute = AsyncMock(side_effect=_execute)


def _eval_row(
    *,
    node_id: uuid.UUID | None = _NODE_ID,
    eval_type: str = "regex",
    config_json: dict | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = _EVAL_ID
    row.organisation_id = _ORG_ID
    row.node_id = node_id
    row.eval_type = eval_type
    row.config_json = config_json or {}
    return row


def _gate_row(*, action: str = "warn", version: int | None = 1, node_id: uuid.UUID = _NODE_ID) -> MagicMock:
    row = MagicMock()
    row.id = _GATE_ID
    row.eval_id = _EVAL_ID
    row.organisation_id = _ORG_ID
    row.node_id = node_id
    row.action = action
    row.version = version
    row.pre_version_raw = None
    row.deleted_at = None
    return row


def _install_overrides(session: AsyncMock, *, org_role: str = "admin") -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[deny_break_glass_mint] = lambda: None


@pytest.fixture(autouse=True)
def _patch_route_side_effects():
    """Stub the RLS helpers and the audit sink the handlers call directly.

    The real ``set_rls_org``/``set_rls_user_context`` issue ``set_config``
    statements through ``session.execute`` and ``append_audit_event`` issues
    its own reads; patching them at the route boundary keeps every test's
    result queue limited to the eval/gate lookups under test.
    """
    with (
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.append_audit_event", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    _install_overrides(session)
    yield TestClient(app), session
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Shared helpers — direct coverage for the advisory-lock create/replace path
# ---------------------------------------------------------------------------


async def test_with_gate_advisory_lock_runs_fn_under_lock() -> None:
    session = _make_session()
    _queue_execute(session, [_result(), _result()])

    async def _fn() -> str:
        return "gate"

    result = await evals_routes._with_gate_advisory_lock(_EVAL_ID, session, _fn)  # type: ignore[arg-type]

    assert result == "gate"
    # statement_timeout + advisory lock
    assert session.execute.await_count == 2


async def test_create_or_replace_gate_no_live_gate_inserts() -> None:
    session = _make_session()
    _queue_execute(session, [_result(), _result(), _result(scalar_one_or_none=None)])
    principal = TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    gate = await evals_routes._create_or_replace_gate(
        _EVAL_ID, {"action": "warn", "node_id": _NODE_ID}, session, principal
    )

    assert gate.action == "warn"
    session.add.assert_called_once()
    session.flush.assert_awaited()


async def test_create_or_replace_gate_soft_deletes_live_gate() -> None:
    session = _make_session()
    live_gate = _gate_row()
    _queue_execute(session, [_result(), _result(), _result(scalar_one_or_none=live_gate)])
    principal = TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    gate = await evals_routes._create_or_replace_gate(
        _EVAL_ID, {"action": "block", "node_id": _NODE_ID}, session, principal
    )

    assert gate.action == "block"
    assert live_gate.deleted_at is not None
    assert live_gate.deleted_by == _USER_ID


async def test_load_eval_or_404_missing_raises_404() -> None:
    session = _make_session()
    _queue_execute(session, [_result(scalar_one_or_none=None)])
    principal = TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await evals_routes._load_eval_or_404(session, _EVAL_ID, principal)
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# POST /evals/{eval_id}/policy-gate
# ---------------------------------------------------------------------------


def test_create_gate_non_admin_returns_403() -> None:
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        resp = TestClient(app).post(_GATE_URL, json={"action": "warn"})
        assert resp.status_code == 403, resp.text
        assert "Only admins" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_create_gate_success(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=_eval_row())])
    with patch.object(evals_routes, "_create_or_replace_gate", new=AsyncMock(return_value=_gate_row())):
        resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["action"] == "warn"
    assert body["version"] == 1


def test_create_gate_eval_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=None)])

    resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Eval definition not found"


def test_create_gate_binding_violation_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    # Chunk 8 retired the guardrail-typed binding exclusion, so a guardrail eval
    # is now bindable; the suite-scoped eval (node_id NULL) is the remaining
    # exclusion that rejects a create.
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=_eval_row(node_id=None))])

    resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 400, resp.text
    assert "binding is invalid" in resp.json()["detail"]


def test_create_gate_conflict_returns_409(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=_eval_row())])
    with patch.object(evals_routes, "_create_or_replace_gate", new=AsyncMock(side_effect=_INTEGRITY)):
        resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 409, resp.text


def test_create_gate_audit_failure_still_succeeds(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=_eval_row())])
    with (
        patch.object(evals_routes, "_create_or_replace_gate", new=AsyncMock(return_value=_gate_row())),
        patch.object(evals_routes, "append_audit_event", new=AsyncMock(side_effect=RuntimeError("audit down"))),
    ):
        resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_PROG, 501),
        (_lock_timeout_exc(), 503),
        (_SQL, 503),
    ],
    ids=["programming-501", "lock-timeout-503", "sqlalchemy-503"],
)
def test_create_gate_error_mapping(exc: Exception, expected: int) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        resp = TestClient(app).post(_GATE_URL, json={"action": "warn"})
        assert resp.status_code == expected, resp.text
    finally:
        app.dependency_overrides.clear()


def test_create_gate_unexpected_error_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_RUNTIME])

    resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 500, resp.text


# ---------------------------------------------------------------------------
# PUT /evals/{eval_id}/policy-gate
# ---------------------------------------------------------------------------


def test_update_gate_non_admin_returns_403() -> None:
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        resp = TestClient(app).put(_GATE_URL, json={"action": "block"})
        assert resp.status_code == 403, resp.text
        assert "Only admins" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_update_gate_success_bumps_version(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=_gate_row(action="warn", version=1)),
        ],
    )

    resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "block"
    assert body["version"] == 2
    assert body["pre_version_raw"] == {"action": "warn"}


def test_update_gate_version_none_defaults_to_one(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=_gate_row(version=None)),
        ],
    )

    resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2


def test_update_gate_eval_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=None)])

    resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Eval definition not found"


def test_update_gate_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=None),
        ],
    )

    resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 404, resp.text
    assert "Policy gate not found" in resp.json()["detail"]


def test_update_gate_binding_violation_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    # Chunk 8 retired the guardrail-typed binding exclusion; a suite-scoped eval
    # (node_id NULL) still fails the binding on update.
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row(node_id=None)),
            _result(scalar_one_or_none=_gate_row()),
        ],
    )

    resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 400, resp.text
    assert "binding is invalid" in resp.json()["detail"]


def test_update_gate_audit_failure_still_succeeds(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=_gate_row()),
        ],
    )
    with patch.object(evals_routes, "append_audit_event", new=AsyncMock(side_effect=RuntimeError("audit down"))):
        resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(_PROG, 501), (_SQL, 503)],
    ids=["programming-501", "sqlalchemy-503"],
)
def test_update_gate_error_mapping(exc: Exception, expected: int) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        resp = TestClient(app).put(_GATE_URL, json={"action": "block"})
        assert resp.status_code == expected, resp.text
    finally:
        app.dependency_overrides.clear()


def test_update_gate_unexpected_error_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_RUNTIME])

    resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 500, resp.text


# ---------------------------------------------------------------------------
# F4: update path returns author warnings (FAR-957)
# ---------------------------------------------------------------------------


def test_update_gate_returns_author_warnings(client: tuple[TestClient, AsyncMock]) -> None:
    """F4: the update path must run check_author_warnings and return the
    warnings in PolicyGateResponse.warnings — not create-only.
    """
    from modulo.core.eval_engine.author_warnings import AuthorWarning

    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row(config_json={"evidence_key": "racy.key"})),
            _result(scalar_one_or_none=_gate_row()),
        ],
    )
    fake_warnings = [AuthorWarning("temporal_ordering", "Producer runs after gate.")]
    with patch(
        "modulo.api.routes.evals.check_author_warnings",
        new_callable=AsyncMock,
        return_value=fake_warnings,
    ):
        resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["warnings"] == [{"code": "temporal_ordering", "message": "Producer runs after gate."}]


# ---------------------------------------------------------------------------
# DELETE /evals/{eval_id}/policy-gate
# ---------------------------------------------------------------------------


def test_delete_gate_non_admin_returns_403() -> None:
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        resp = TestClient(app).delete(_GATE_URL)
        assert resp.status_code == 403, resp.text
        assert "Only admins" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_delete_gate_success_returns_204(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    live_gate = _gate_row()
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=live_gate),
        ],
    )

    resp = http.delete(_GATE_URL)

    assert resp.status_code == 204, resp.text
    assert live_gate.deleted_at is not None
    assert live_gate.deleted_by == _USER_ID


def test_delete_gate_eval_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=None)])

    resp = http.delete(_GATE_URL)

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Eval definition not found"


def test_delete_gate_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=None),
        ],
    )

    resp = http.delete(_GATE_URL)

    assert resp.status_code == 404, resp.text
    assert "Policy gate not found" in resp.json()["detail"]


def test_delete_gate_cascade_conflict_returns_409() -> None:
    session = _make_session_commit_raiser(_INTEGRITY)
    _install_overrides(session)
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=_gate_row()),
        ],
    )
    try:
        # The FK-RESTRICT IntegrityError surfaces at transaction commit.
        resp = TestClient(app).delete(_GATE_URL)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text


def test_delete_gate_audit_failure_still_succeeds(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=_gate_row()),
        ],
    )
    with patch.object(evals_routes, "append_audit_event", new=AsyncMock(side_effect=RuntimeError("audit down"))):
        resp = http.delete(_GATE_URL)

    assert resp.status_code == 204, resp.text


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(_PROG, 501), (_SQL, 503)],
    ids=["programming-501", "sqlalchemy-503"],
)
def test_delete_gate_error_mapping(exc: Exception, expected: int) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        resp = TestClient(app).delete(_GATE_URL)
        assert resp.status_code == expected, resp.text
    finally:
        app.dependency_overrides.clear()


def test_delete_gate_unexpected_error_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_RUNTIME])

    resp = http.delete(_GATE_URL)

    assert resp.status_code == 500, resp.text


# ---------------------------------------------------------------------------
# GET /evals/{eval_id}/policy-gate
# ---------------------------------------------------------------------------


def test_get_gate_success(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=_gate_row(action="block", version=3)),
        ],
    )

    resp = http.get(_GATE_URL)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "block"
    assert body["version"] == 3


def test_get_gate_eval_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=None)])

    resp = http.get(_GATE_URL)

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Eval definition not found"


def test_get_gate_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=None),
        ],
    )

    resp = http.get(_GATE_URL)

    assert resp.status_code == 404, resp.text
    assert "Policy gate not found" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(_PROG, 501), (_SQL, 503)],
    ids=["programming-501", "sqlalchemy-503"],
)
def test_get_gate_error_mapping(exc: Exception, expected: int) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        resp = TestClient(app).get(_GATE_URL)
        assert resp.status_code == expected, resp.text
    finally:
        app.dependency_overrides.clear()


def test_get_gate_unexpected_error_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_RUNTIME])

    resp = http.get(_GATE_URL)

    assert resp.status_code == 500, resp.text

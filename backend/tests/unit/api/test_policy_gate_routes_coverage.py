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

import logging
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
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


def test_create_gate_suite_scoped_eval_rejected_before_any_insert(client: tuple[TestClient, AsyncMock]) -> None:
    """A node-less (suite-scoped) eval is rejected before the gate insert.

    The route must not fabricate a ``node_id`` for a node-less eval, so the
    binding validation 400 is returned and the insert is never attempted — the
    failure cannot leak out as an IntegrityError mapped to a misleading 409.
    """
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=_eval_row(node_id=None))])
    insert = AsyncMock()
    with patch.object(evals_routes, "_create_or_replace_gate", new=insert):
        resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 400, resp.text
    insert.assert_not_awaited()


def test_create_gate_binds_the_evals_own_node_id(client: tuple[TestClient, AsyncMock]) -> None:
    """The gate's ``node_id`` is the eval's ``node_id`` verbatim.

    The retired ``eval_row.node_id or uuid.uuid4()`` placeholder would have
    silently bound a gate to a fabricated node; pin the pass-through instead.
    """
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=_eval_row(node_id=_NODE_ID))])
    insert = AsyncMock(return_value=_gate_row())
    with patch.object(evals_routes, "_create_or_replace_gate", new=insert):
        resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 201, resp.text
    gate_fields = insert.await_args.args[1]
    assert gate_fields["node_id"] == _NODE_ID


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
# _collect_author_warnings (FAR-957 §3.2) — candidate extraction + failure fallback
# ---------------------------------------------------------------------------


def _make_admin_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


async def test_collect_author_warnings_checks_top_level_and_nested_detection_keys() -> None:
    """Both ``evidence_key`` mentions are checked: the top-level config key AND
    ``detection.evidence_key`` (the guardrail-config nesting)."""
    session = _make_session()
    check = AsyncMock(return_value=[])
    eval_row = _eval_row(config_json={"evidence_key": "top.key", "detection": {"evidence_key": "det.key"}})
    with patch("modulo.api.routes.evals.check_author_warnings", new=check):
        warnings = await evals_routes._collect_author_warnings(session, _make_admin_principal(), eval_row, _EVAL_ID)

    assert not warnings
    checked_keys = {call.kwargs["evidence_key"] for call in check.await_args_list}
    assert checked_keys == {"top.key", "det.key"}
    assert session.execute.await_count == 0  # the helper only proxies the check


async def test_collect_author_warnings_ignores_non_dict_detection() -> None:
    """A ``detection`` that is not a mapping is not an evidence-key reference."""
    session = _make_session()
    check = AsyncMock(return_value=[])
    eval_row = _eval_row(config_json={"detection": "not-a-mapping"})
    with patch("modulo.api.routes.evals.check_author_warnings", new=check):
        warnings = await evals_routes._collect_author_warnings(session, _make_admin_principal(), eval_row, _EVAL_ID)

    assert not warnings
    check.assert_not_awaited()


async def test_collect_author_warnings_ignores_detection_without_evidence_key() -> None:
    """A ``detection`` mapping without ``evidence_key`` is not an evidence-key
    reference (a bare ``key`` is too ambiguous)."""
    session = _make_session()
    check = AsyncMock(return_value=[])
    eval_row = _eval_row(config_json={"detection": {"key": "threshold"}})
    with patch("modulo.api.routes.evals.check_author_warnings", new=check):
        warnings = await evals_routes._collect_author_warnings(session, _make_admin_principal(), eval_row, _EVAL_ID)

    assert not warnings
    check.assert_not_awaited()


async def test_collect_author_warnings_check_failure_is_logged_and_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An author-warning check failure is advisory: logged at WARNING with the
    eval/evidence context and swallowed so the gate write still proceeds."""
    session = _make_session()
    eval_row = _eval_row(config_json={"evidence_key": "flaky.key"})
    with (
        caplog.at_level(logging.WARNING, logger="modulo.api.routes.evals"),
        patch(
            "modulo.api.routes.evals.check_author_warnings",
            new=AsyncMock(side_effect=RuntimeError("author check down")),
        ),
    ):
        warnings = await evals_routes._collect_author_warnings(session, _make_admin_principal(), eval_row, _EVAL_ID)

    assert not warnings
    failure_records = [r for r in caplog.records if r.msg == "policy_gate.author_warnings_check_failed"]
    assert len(failure_records) == 1
    assert failure_records[0].evidence_key == "flaky.key"
    assert failure_records[0].eval_id == str(_EVAL_ID)


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


# ---------------------------------------------------------------------------
# FAR-967 F6 — create-or-replace INHERITS the operator's enabled state
# ---------------------------------------------------------------------------


async def test_create_or_replace_gate_carries_disabled_state() -> None:
    """F6 load-bearing: replacing a live gate that the operator DISABLED must
    carry ``enabled=False`` (+ its timestamp pair) onto the new row — a
    create-or-replace must never silently re-enable a disabled gate."""
    session = _make_session()
    live_gate = _gate_row(action="block")
    disabled_at = datetime(2026, 1, 1, tzinfo=UTC)
    live_gate.enabled = False
    live_gate.enabled_at = None
    live_gate.disabled_at = disabled_at
    # SET LOCAL + advisory lock + the in-lock live-gate re-check.
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

    assert gate.enabled is False
    assert gate.enabled_at is None
    assert gate.disabled_at == disabled_at
    assert live_gate.deleted_at is not None  # the old row is still replaced


async def test_create_or_replace_gate_fresh_create_stamps_enabled() -> None:
    """Sibling: with NO live gate the fresh create keeps the creation default
    (enabled + enabled_at stamped, disabled_at NULL — the CHECK-acceptable
    creation state)."""
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

    assert gate.enabled is True
    assert gate.enabled_at is not None
    assert gate.disabled_at is None


def test_create_gate_audit_payload_carries_enabled(client: tuple[TestClient, AsyncMock]) -> None:
    """F6: the ``policy_gate.created`` audit payload records whether the gate
    is actively enforcing — an auditor must see it without re-reading state."""
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=_eval_row())])
    gate = _gate_row()
    gate.enabled = True
    with (
        patch.object(evals_routes, "_create_or_replace_gate", new=AsyncMock(return_value=gate)),
        patch("modulo.api.routes.evals.append_audit_event", new_callable=AsyncMock) as audit,
    ):
        resp = http.post(_GATE_URL, json={"action": "warn"})

    assert resp.status_code == 201, resp.text
    payload = audit.await_args.kwargs["payload_json"]
    assert payload["enabled"] is True
    assert audit.await_args.kwargs["event_type"] == "policy_gate.created"


def test_update_gate_audit_payload_carries_enabled(client: tuple[TestClient, AsyncMock]) -> None:
    """F6: the ``policy_gate.updated`` audit payload records ``enabled`` too."""
    http, session = client
    gate = _gate_row(action="warn", version=1)
    gate.enabled = True
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(scalar_one_or_none=gate),
        ],
    )
    with patch("modulo.api.routes.evals.append_audit_event", new_callable=AsyncMock) as audit:
        resp = http.put(_GATE_URL, json={"action": "block"})

    assert resp.status_code == 200, resp.text
    payload = audit.await_args.kwargs["payload_json"]
    assert payload["enabled"] is True
    assert audit.await_args.kwargs["event_type"] == "policy_gate.updated"


# ---------------------------------------------------------------------------
# PATCH /evals/{eval_id}/policy-gate/toggle (FAR-967 F6/F9 operator control)
# ---------------------------------------------------------------------------

_TOGGLE_URL = f"{_GATE_URL}/toggle"


def _toggle_gate_row(*, enabled: bool, action: str = "warn") -> MagicMock:
    row = _gate_row(action=action)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    row.enabled = enabled
    row.enabled_at = stamp if enabled else None
    row.disabled_at = None if enabled else stamp
    return row


def test_toggle_gate_non_admin_returns_403() -> None:
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        resp = TestClient(app).patch(_TOGGLE_URL, json={"enabled": False})
        assert resp.status_code == 403, resp.text
        assert "Only admins can toggle policy gates" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_toggle_gate_disable_success(client: tuple[TestClient, AsyncMock]) -> None:
    """Disabling stamps ``disabled_at`` and clears ``enabled_at`` so the
    symmetric CHECK invariant holds (§4.2), and the response reports the new
    state."""
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(),
            _result(),
            _result(scalar_one_or_none=_toggle_gate_row(enabled=True)),
        ],
    )

    resp = http.patch(_TOGGLE_URL, json={"enabled": False})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["enabled_at"] is None
    assert body["disabled_at"] is not None


def test_toggle_gate_enable_success(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(),
            _result(),
            _result(scalar_one_or_none=_toggle_gate_row(enabled=False)),
        ],
    )

    resp = http.patch(_TOGGLE_URL, json={"enabled": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["enabled_at"] is not None
    assert body["disabled_at"] is None


def test_toggle_gate_holds_advisory_lock_around_the_mutation(client: tuple[TestClient, AsyncMock]) -> None:
    """F6 load-bearing: the toggle's load-and-mutate runs under the SAME
    transaction-scoped advisory lock as create-or-replace — SET LOCAL
    statement_timeout and pg_advisory_xact_lock must execute BEFORE the gate
    read. Remove the lock wrapper and these statements (and their ordering)
    vanish."""
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(),
            _result(),
            _result(scalar_one_or_none=_toggle_gate_row(enabled=True)),
        ],
    )

    resp = http.patch(_TOGGLE_URL, json={"enabled": False})
    assert resp.status_code == 200, resp.text

    stmts = [str(call.args[0]) for call in session.execute.await_args_list if call.args]
    timeout_idx = next(i for i, s in enumerate(stmts) if "statement_timeout" in s)
    lock_idx = next(i for i, s in enumerate(stmts) if "pg_advisory_xact_lock" in s)
    gate_read_idx = next(i for i, s in enumerate(stmts) if "policy_gates" in s and "deleted_at" in s)
    eval_idx = next(i for i, s in enumerate(stmts) if "evals" in s and "policy_gates" not in s)
    # eval load → lock acquisition → gate read (the read happens under lock)
    assert eval_idx < timeout_idx < lock_idx < gate_read_idx


def test_toggle_gate_audit_records_enabled_transition(client: tuple[TestClient, AsyncMock]) -> None:
    """F6/F9: the ``policy_gate.toggled`` audit event carries the new
    ``enabled`` value and the pre-toggle state."""
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(),
            _result(),
            _result(scalar_one_or_none=_toggle_gate_row(enabled=True)),
        ],
    )
    with patch("modulo.api.routes.evals.append_audit_event", new_callable=AsyncMock) as audit:
        resp = http.patch(_TOGGLE_URL, json={"enabled": False})

    assert resp.status_code == 200, resp.text
    assert audit.await_args.kwargs["event_type"] == "policy_gate.toggled"
    payload = audit.await_args.kwargs["payload_json"]
    assert payload["enabled"] is False
    assert payload["pre_enabled"] is True
    assert payload["eval_id"] == str(_EVAL_ID)


def test_toggle_gate_eval_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=None)])

    resp = http.patch(_TOGGLE_URL, json={"enabled": False})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Eval definition not found"


def test_toggle_gate_gate_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(),
            _result(),
            _result(scalar_one_or_none=None),
        ],
    )

    resp = http.patch(_TOGGLE_URL, json={"enabled": False})

    assert resp.status_code == 404, resp.text


def test_toggle_gate_lock_timeout_returns_503(client: tuple[TestClient, AsyncMock]) -> None:
    """A lock-acquisition timeout (SQLSTATE 57014) on the advisory lock maps
    to 503 with the lock-timeout message — same mapping as create."""
    http, session = client
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _lock_timeout_exc(),
        ],
    )

    resp = http.patch(_TOGGLE_URL, json={"enabled": False})

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == evals_routes._MSG_POLICY_GATE_LOCK_TIMEOUT


def test_toggle_gate_check_violation_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    """The symmetric CHECK violation (SQLSTATE 23514) is a server-side bug —
    mapped to the report-it 500, not a generic 503."""
    http, session = client
    check_exc = IntegrityError("s", {}, Exception())
    check_exc.orig = SimpleNamespace(sqlstate="23514")  # type: ignore[attr-defined]
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=_eval_row()),
            _result(),
            _result(),
            check_exc,
        ],
    )

    resp = http.patch(_TOGGLE_URL, json={"enabled": False})

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"] == evals_routes._MSG_POLICY_GATE_TOGGLE_CHECK_VIOLATION


def test_toggle_gate_unexpected_error_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_RUNTIME])

    resp = http.patch(_TOGGLE_URL, json={"enabled": False})

    assert resp.status_code == 500, resp.text


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(_PROG, 501), (_SQL, 503)],
    ids=["programming-501", "sqlalchemy-503"],
)
def test_toggle_gate_error_mapping(exc: Exception, expected: int) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        resp = TestClient(app).patch(_TOGGLE_URL, json={"enabled": False})
        assert resp.status_code == expected, resp.text
    finally:
        app.dependency_overrides.clear()

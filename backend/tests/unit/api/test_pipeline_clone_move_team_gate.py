"""FAR-1276: the team gate on clone / move-to-folder, like every sibling mutation.

``POST /{id}/clone`` and ``PATCH /{id}/folder`` carried NEITHER layer of the
two-layer team gate that ``update`` / ``delete`` / ``archive`` /
``convert-to-agent`` / ``revert-to-manual`` all carry:

* no ``require_team_membership_or_admin(resolve_pipeline_team_scope)``
  request-time dependency, and
* no ``_reapply_team_gate_inside_mutation_txn`` re-check inside the mutation
  transaction (the request-time -> mutation TOCTOU close).

Parity + defence-in-depth, NOT a live Postgres hole: migration 0124 leaves
``rls_team_isolation`` as the sole policy on ``pipelines``, so under Postgres a
non-member's row is already invisible and the resolver 404s before the handler
(the integration tests observe exactly that). The request-time dependency is
the only team layer where RLS does NOT apply - non-Postgres backends,
break-glass / execution_context sessions, and missing or misconfigured policies
- and the in-txn re-check is what closes the TOCTOU atomically with the write.

CLONE PLACEMENT (the one place this differs from its siblings): clone's
``FOR SHARE`` source read runs on a SEPARATE connection inside
``clone_pipeline`` (the torn-read fix), so the in-txn gate must NOT be taken
before it - a share lock conflicts with the gate's ``FOR UPDATE`` and the two
transactions would wait on each other. The gate therefore rides in as
``clone_pipeline``'s ``_on_step_a_committed`` hook: still inside the mutation
transaction, still immediately before the copy write, but after the share lock
has been released. ``test_clone_passes_the_gate_as_the_step_a_commit_hook``
pins that wiring; the tests below drive it by awaiting the hook exactly as
``clone_pipeline`` does.

Tests (mocked session, real FastAPI dependency stack):

* non-member -> 403 from the request-time dependency (both routes),
* member / org-admin -> the request completes,
* TOCTOU half: the request-time gate PASSES while the in-txn re-check DENIES -
  the endpoint must still answer 403, proving the in-txn layer is wired and is
  not a no-op behind a passing dependency,
* clone target: a clone landing on a team the caller does not belong to is
  refused even when the SOURCE gate passed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_FOLDER_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_TEAM_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
#: The team the CLONE lands on in the clone-target test - deliberately not
#: ``_TEAM_ID``, so the target re-check has a real denial to raise.
_OTHER_TEAM = uuid.UUID("77777777-7777-7777-7777-777777777777")
_PREFIX = "modulo.api.routes.pipelines."
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_NON_MEMBER_DETAIL = "Not a member of the team that owns this resource"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _result(*, first: Any = None, scalar_one_or_none: Any = None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = first
    result.scalar_one_or_none.return_value = scalar_one_or_none
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    return result


def _pipeline_row(*, visibility: str, owner_team_id: uuid.UUID | None) -> MagicMock:
    """A stand-in row carrying every field ``PipelineResponse`` reads.

    The response model validates ``from_attributes``, so anything left as an
    auto-created MagicMock child (a datetime, a uuid) would 422 - hence the
    explicit field set, mirroring ``test_pipelines_endpoint._make_pipeline``.
    """
    row = MagicMock()
    row.id = _PIPELINE_ID
    row.organisation_id = _ORG_ID
    row.name = "Team private pipeline"
    row.description = None
    row.visibility = visibility
    row.owner_team_id = owner_team_id
    row.folder_id = None
    row.max_concurrent_runs = 5
    row.lock_wait_timeout_seconds = 300
    row.node_timeout_seconds = 300
    row.run_context_defaults = {}
    row.default_autonomy_level = "manual_approval"
    row.max_autonomy_level = None
    row.rate_limit_config = None
    row.max_duration_seconds = None
    row.archived_at = None
    row.snapshot_count = 0
    row.graph_nodes_json = []
    row.created_by = uuid.uuid4()
    row.account_id = row.created_by
    row.created_at = _NOW
    row.updated_at = _NOW
    row.deleted_at = None
    return row


def _make_team_session(*, is_member: bool) -> AsyncMock:
    """Session double answering the two-layer team gate for clone + move.

    Dialect reports ``sqlite`` (so the Postgres-only ``set_config`` lock-timeout
    statement is not issued - that path has its own test), and every query is
    dispatched on its SQL text rather than by call position, so statement ORDER
    changes cannot silently swap answers.

    The non-``FOR UPDATE`` ``FROM pipelines`` read serves BOTH the team-scope
    resolver (which projects ``owner_team_id, visibility`` and reads
    ``.first()``) and clone's ``get_pipeline`` (full entity, read via
    ``.scalar_one_or_none()``) - one result object answers both accessors.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.info = {}
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)

    gate_row = _pipeline_row(visibility="team", owner_team_id=_TEAM_ID)

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        if "set_config" in sql:
            return _result()
        if "authz_enforce" in sql:
            return _result(scalar_one_or_none=None)
        if "team_memberships" in sql:
            return _result(first=uuid.uuid4() if is_member else None)
        if "FROM pipelines" in sql:
            if isinstance(stmt, Select) and "FOR UPDATE" in sql.upper():
                return _result(scalar_one_or_none=gate_row)
            both = _result(first=(_TEAM_ID, "team"))
            both.scalar_one_or_none.return_value = gate_row
            return both
        return _result()

    session.execute = AsyncMock(side_effect=_execute)
    return session


@contextmanager
def _client_for(session: AsyncMock, role: str) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=f"{role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@contextmanager
def _patched(patches: list) -> Generator[None, None, None]:
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _cloned_row(*, visibility: str = "org", owner_team_id: uuid.UUID | None = None) -> MagicMock:
    row = _pipeline_row(visibility=visibility, owner_team_id=owner_team_id)
    row.id = uuid.uuid4()
    row.name = "Copy of Team private pipeline"
    return row


def _clone_patches(cloned: object) -> list:
    """Patches for the clone route, with the REAL in-txn gate wired in.

    ``clone_pipeline`` is stubbed (its deep copy is crud-layer work, tested in
    ``tests/integration/crud/test_pipeline.py``), but the stub AWAITS the
    ``_on_step_a_committed`` hook it is handed - exactly what the real
    ``clone_pipeline`` does right after step (a) commits. That keeps the gate
    under test instead of mocking it away.
    """

    async def _clone_with_gate(_session: object, *_args: Any, **kwargs: Any) -> object:
        hook = kwargs.get("_on_step_a_committed")
        if hook is not None:
            await hook()
        return cloned

    return [
        patch(f"{_PREFIX}check_pipeline_name_available", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}clone_pipeline", new=AsyncMock(side_effect=_clone_with_gate)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
    ]


def _move_patches(moved: object) -> list:
    return [
        patch(f"{_PREFIX}move_pipeline_to_folder", new=AsyncMock(return_value=moved)),
    ]


# ---------------------------------------------------------------------------
# Request-time dependency: non-member denied, member / admin allowed
# ---------------------------------------------------------------------------


def test_clone_non_member_is_denied_403() -> None:
    session = _make_team_session(is_member=False)
    with _client_for(session, role="operator") as http:
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_move_non_member_is_denied_403() -> None:
    session = _make_team_session(is_member=False)
    with _client_for(session, role="operator") as http:
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": str(_FOLDER_ID)})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_clone_member_succeeds() -> None:
    session = _make_team_session(is_member=True)
    with _client_for(session, role="operator") as http, _patched(_clone_patches(_cloned_row())):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
    assert resp.status_code == 201, resp.text


def test_move_member_succeeds() -> None:
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        _patched(_move_patches(_pipeline_row(visibility="team", owner_team_id=_TEAM_ID))),
    ):
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": str(_FOLDER_ID)})
    assert resp.status_code == 200, resp.text


def test_clone_org_admin_bypasses_the_membership_gate() -> None:
    """Admin bypass (RLS parity): no membership row is required."""
    session = _make_team_session(is_member=False)
    with _client_for(session, role="admin") as http, _patched(_clone_patches(_cloned_row())):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
    assert resp.status_code == 201, resp.text


def test_move_org_admin_bypasses_the_membership_gate() -> None:
    session = _make_team_session(is_member=False)
    with (
        _client_for(session, role="admin") as http,
        _patched(_move_patches(_pipeline_row(visibility="team", owner_team_id=_TEAM_ID))),
    ):
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": None})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Wiring: the clone gate rides in on clone_pipeline's step-(a) commit hook
# ---------------------------------------------------------------------------


def test_clone_passes_the_gate_as_the_step_a_commit_hook() -> None:
    """The in-txn re-check must be HANDLED to clone_pipeline, not dropped.

    This is the placement constraint the clone endpoint has that its siblings
    do not: taking ``FOR UPDATE`` before step (a)'s separate-connection
    ``FOR SHARE`` read would make the two transactions wait on each other, so
    the gate is passed as ``_on_step_a_committed`` and runs after that share
    lock commits - still inside the mutation transaction, still before the
    copy write. A call that omits the kwarg would silently ship no in-txn gate.
    """
    session = _make_team_session(is_member=True)
    seen: dict[str, Any] = {}

    async def _clone_recording(_session: object, *_args: Any, **kwargs: Any) -> object:
        seen.update(kwargs)
        return _cloned_row()

    with (
        _client_for(session, role="operator") as http,
        patch(f"{_PREFIX}check_pipeline_name_available", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}clone_pipeline", new=AsyncMock(side_effect=_clone_recording)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
    ):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})

    assert resp.status_code == 201, resp.text
    assert "_on_step_a_committed" in seen, f"clone_pipeline was not handed the team gate: {sorted(seen)}"
    assert callable(seen["_on_step_a_committed"]), seen["_on_step_a_committed"]


# ---------------------------------------------------------------------------
# In-txn half: the request-time gate passing must NOT mean the mutation runs
# ---------------------------------------------------------------------------


def test_clone_in_txn_team_gate_denial_overrides_a_passing_request_time_gate() -> None:
    """TOCTOU close: dependency says member, the locked row's gate says otherwise.

    ``team_membership_exists`` in ``routes.pipelines`` is what the in-txn
    re-check consults; the dependency consults its own import, so patching the
    routes one leaves the request-time gate passing (the session still answers
    the dependency) while the in-txn re-check denies - exactly what an
    ownership flip inside the request -> mutation window would look like.
    """
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        _patched(_clone_patches(_cloned_row())),
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
    ):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_move_in_txn_team_gate_denial_overrides_a_passing_request_time_gate() -> None:
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        _patched(_move_patches(_pipeline_row(visibility="team", owner_team_id=_TEAM_ID))),
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
    ):
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": str(_FOLDER_ID)})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


# ---------------------------------------------------------------------------
# Clone TARGET: inheriting the source's team must not hand it to a foreign team
# ---------------------------------------------------------------------------


def test_clone_target_owned_by_a_foreign_team_is_denied() -> None:
    """The clone row itself is gated, not only the source it was copied from.

    ``_clone_pipeline_into_org`` copies ``visibility``/``owner_team_id`` from
    the source, so in practice the target team IS the source team - but the
    re-check makes that explicit, and it is what refuses a target the caller
    does not belong to if the two ever diverge.

    ``team_membership_exists`` is answered per call: True for the in-txn gate
    over the SOURCE team (so the earlier gate passes), False for the TARGET
    check over ``_OTHER_TEAM``.
    """
    session = _make_team_session(is_member=True)
    foreign_target = _cloned_row(visibility="team", owner_team_id=_OTHER_TEAM)
    with (
        _client_for(session, role="operator") as http,
        _patched(_clone_patches(foreign_target)),
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(side_effect=[True, False])),
    ):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_clone_target_owned_by_the_callers_team_is_accepted() -> None:
    """The target check must not blanket-block: same-team targets still land."""
    session = _make_team_session(is_member=True)
    same_team_target = _cloned_row(visibility="team", owner_team_id=_TEAM_ID)
    with _client_for(session, role="operator") as http, _patched(_clone_patches(same_team_target)):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
    assert resp.status_code == 201, resp.text

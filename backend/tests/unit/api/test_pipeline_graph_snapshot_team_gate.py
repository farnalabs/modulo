"""FAR-1311 + FAR-1312: team-gate parity on the graph READ and the snapshot WRITES.

PARITY + defence-in-depth, in the shape of
``tests/unit/api/test_pipeline_node_conversion_team_gate.py``:

* **FAR-1311** - ``GET /pipelines/{id}/graph`` was the only pipeline READ that
  carried no ``require_team_membership_or_admin[_any_credential]`` dependency
  while its sibling ``GET /pipelines/{id}`` did. On the layers where RLS does
  not apply (non-Postgres backends, break-glass/execution_context sessions, RLS
  misconfiguration) any org member with ``pipeline.graph.read`` could read a
  TEAM-PRIVATE pipeline's full graph - prompts, ``connector_bindings_json``,
  model pins. It is read-only, so there is deliberately no in-txn re-check.
* **FAR-1312** - ``POST /pipelines/{id}/snapshots`` (save-edit) and
  ``PATCH /pipelines/{id}/snapshots/{snapshot_id}`` (tag) mutated with the
  request-time dependency ONLY - no ``_reapply_team_gate_inside_mutation_txn``
  re-check inside the mutation transaction, unlike update / replace-graph /
  convert / revert / rollback / delete-snapshot.

Neither is a live Postgres hole: migration 0124 leaves ``rls_team_isolation``
as the sole ``pipelines`` policy, so a non-member's row read already returns no
row (the integration tests observe exactly that); the request-time dependency
is the only team layer where RLS does NOT apply, and the in-txn re-check closes
the request-time -> mutation TOCTOU atomically with the write.

Tests (mocked session, real FastAPI dependency stack):

* non-member -> 403 from the request-time dependency,
* member / org-admin -> the request completes,
* TOCTOU half (FAR-1312): the request-time gate PASSES (membership exists)
  while the in-txn re-check DENIES - the endpoint must still answer 403,
  proving the in-txn layer is wired and is not a no-op behind a passing
  dependency.

The in-txn half patches ``modulo.api.routes.pipelines.team_membership_exists``
ONLY: the request-time dependency resolves membership through its own import
(``modulo.api.dependencies`` -> ``modulo.api.team_scope``), so a patch on the
routes module can never short-circuit the dependency.
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
_SNAPSHOT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_TEAM_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_PREFIX = "modulo.api.routes.pipelines."
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_NON_MEMBER_DETAIL = "Not a member of the team that owns this resource"

_MK_KEY = "mk_12345678_" + "x" * 32


def _fake_key(role: str = "operator", team_id: uuid.UUID | None = None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        name="apply-key",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        role=role,
        team_id=team_id,
    )


def _make_auth_session() -> AsyncMock:
    """Session for ``get_current_tenant_user_or_api_key``'s internal key lookups."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock()
    session.get_bind.return_value.dialect.name = "sqlite"
    result = MagicMock()
    result.scalar_one_or_none.return_value = _fake_key()
    session.execute = AsyncMock(return_value=result)
    return session


class _FakeFactory:
    """async_sessionmaker stand-in for the API-key auth path."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> _FakeFactory:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> bool:
        return False


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


def _team_private_pipeline_row() -> MagicMock:
    row = MagicMock()
    row.visibility = "team"
    row.owner_team_id = _TEAM_ID
    row.deleted_at = None
    row.id = _PIPELINE_ID
    row.organisation_id = _ORG_ID
    return row


def _make_team_session(*, is_member: bool) -> AsyncMock:
    """Session double answering every query the two gates and the handlers issue.

    Dispatches on SQL text rather than call position, so statement ORDER
    changes cannot silently swap answers:

    * ``set_config`` (RLS org/user context) -> no-op,
    * ``authz_enforce`` (permission kill-switch read) -> enforce default,
    * ``team_memberships`` -> a membership row iff ``is_member``,
    * ``FROM pipelines ... FOR UPDATE`` (the in-txn gate's locked re-select)
      -> a TEAM-PRIVATE row,
    * ``FROM pipelines`` without ``FOR UPDATE`` -> BOTH the team-scope
      resolver's tuple row (``.first()``) and the ``get_pipeline`` row
      (``.scalar_one_or_none()``), since the two callers share one statement
      shape.

    The dialect reports ``sqlite`` so the Postgres-only
    ``set_config('lock_timeout', ...)`` statement is not issued (that path has
    its own test).
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

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        if "set_config" in sql:
            return _result()
        if "authz_enforce" in sql:
            return _result(scalar_one_or_none=None)
        if "team_memberships" in sql:
            return _result(first=uuid.uuid4() if is_member else None)
        if isinstance(stmt, Select) and "FROM pipelines" in sql:
            if "FOR UPDATE" in sql.upper():
                return _result(scalar_one_or_none=_team_private_pipeline_row())
            result = _result(first=(_TEAM_ID, "team"))
            result.scalar_one_or_none.return_value = _team_private_pipeline_row()
            return result
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


def _make_snapshot(**overrides: object) -> MagicMock:
    s = MagicMock()
    s.id = _SNAPSHOT_ID
    s.pipeline_id = _PIPELINE_ID
    s.snapshot_version = 1
    s.tag = None
    s.notes = None
    s.created_at = _NOW
    s.account_id = _USER_ID
    s.version_kind = "edit"
    s.created_kind = "edit"
    s.draft = False
    s.channel = "none"
    s.graph_json = {"nodes": [], "edges": []}
    s.connector_bindings_json = []
    s.schema_pins_json = []
    s.prompt_pins_json = []
    s.model_backend_pins_json = []
    s.default_autonomy_level = None
    s.run_context_defaults = {}
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _graph() -> tuple[list[dict[str, Any]], list[Any]]:
    return ([{"id": str(uuid.uuid4()), "node_type": "manual", "position": {"x": 0, "y": 0}}], [])


# ---------------------------------------------------------------------------
# FAR-1311: GET /pipelines/{id}/graph - request-time dependency only
# ---------------------------------------------------------------------------


def test_graph_non_member_is_denied_403() -> None:
    """The app-layer branch (a team-private row the caller does not own)."""
    session = _make_team_session(is_member=False)
    with _client_for(session, role="operator") as http:
        resp = http.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_graph_member_succeeds() -> None:
    session = _make_team_session(is_member=True)
    nodes, edges = _graph()
    with (
        _client_for(session, role="operator") as http,
        _patched([patch(f"{_PREFIX}get_pipeline_graph", new=AsyncMock(return_value=(nodes, edges)))]),
    ):
        resp = http.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
    assert resp.status_code == 200, resp.text
    assert resp.json()["nodes"][0]["id"] == nodes[0]["id"]


def test_graph_org_admin_bypasses_the_membership_gate() -> None:
    """Admin bypass (RLS parity): no membership row is required."""
    session = _make_team_session(is_member=False)
    nodes, edges = _graph()
    with (
        _client_for(session, role="admin") as http,
        _patched([patch(f"{_PREFIX}get_pipeline_graph", new=AsyncMock(return_value=(nodes, edges)))]),
    ):
        resp = http.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
    assert resp.status_code == 200, resp.text


def test_graph_still_accepts_an_mk_org_api_key() -> None:
    """The FAR-1311 gate must be the ANY-CREDENTIAL variant - prove the pairing.

    ``GET /graph`` carries ``require_permission_any_credential`` because
    declarative apply fetches current graphs with ``mk_`` org API keys
    (``cli/apply/executor.py`` -> ``GET /pipelines/{id}/graph``). The team
    dependency must therefore be
    ``require_team_membership_or_admin_any_credential`` too: the credential
    flavours have to match, or the JWT-only variant (which resolves through
    ``get_current_tenant_user`` -> ``decode_principal``) would 401 every mk_
    graph fetch.

    This test runs the REAL ``mk_`` resolution (no ``get_current_user``
    override, so the conftest any-credential default falls through to the live
    dependency) against a member's key: 200 means the key passed BOTH the
    permission gate and the new team gate. With the JWT-only dependency it
    would answer 401 "Invalid or expired token".
    """
    route_session = _make_team_session(is_member=True)
    auth_session = _make_auth_session()
    nodes, edges = _graph()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield route_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch(
                "modulo.api.dependencies.get_or_create_session_factory",
                return_value=_FakeFactory(auth_session),
            ),
            patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("operator")),
            patch(
                "modulo.auth.dependencies.resolve_role_from_membership",
                new=AsyncMock(return_value="operator"),
            ),
            patch(f"{_PREFIX}get_pipeline_graph", new=AsyncMock(return_value=(nodes, edges))),
        ):
            resp = TestClient(app).get(
                f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
                headers={"Authorization": f"Bearer {_MK_KEY}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json()["nodes"][0]["id"] == nodes[0]["id"]


# ---------------------------------------------------------------------------
# FAR-1312: POST /snapshots (save-edit) - request-time dependency + in-txn gate
# ---------------------------------------------------------------------------


def test_save_edit_snapshot_non_member_is_denied_403() -> None:
    session = _make_team_session(is_member=False)
    with _client_for(session, role="operator") as http:
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots", json={"draft": True})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_save_edit_snapshot_member_succeeds() -> None:
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        _patched([patch(f"{_PREFIX}create_snapshot_edit", new=AsyncMock(return_value=_make_snapshot(draft=True)))]),
    ):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots", json={"draft": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft"] is True


def test_save_edit_snapshot_org_admin_bypasses_the_membership_gate() -> None:
    session = _make_team_session(is_member=False)
    with (
        _client_for(session, role="admin") as http,
        _patched([patch(f"{_PREFIX}create_snapshot_edit", new=AsyncMock(return_value=_make_snapshot()))]),
    ):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots", json={})
    assert resp.status_code == 200, resp.text


def test_save_edit_snapshot_in_txn_denial_overrides_a_passing_request_time_gate() -> None:
    """TOCTOU close: dependency says member, the locked row says otherwise.

    The request-time dependency resolves membership through
    ``modulo.api.dependencies``' own import (the session answers a membership
    row), while the in-txn re-check resolves it through the one imported into
    ``routes.pipelines`` - patched here to deny, exactly as an ownership flip
    inside the request -> mutation window would behave. The endpoint must still
    answer 403: a passing request-time gate must never be the only gate.
    """
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}create_snapshot_edit", new=AsyncMock(return_value=_make_snapshot())),
    ):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots", json={})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


# ---------------------------------------------------------------------------
# FAR-1312: PATCH /snapshots/{snapshot_id} (tag) - same two layers
# ---------------------------------------------------------------------------


def test_tag_snapshot_non_member_is_denied_403() -> None:
    session = _make_team_session(is_member=False)
    with _client_for(session, role="operator") as http:
        resp = http.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}",
            json={"tag": "prod"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_tag_snapshot_member_succeeds() -> None:
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        _patched([patch(f"{_PREFIX}tag_snapshot", new=AsyncMock(return_value=_make_snapshot(tag="prod")))]),
    ):
        resp = http.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}",
            json={"tag": "prod", "notes": "release"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tag"] == "prod"


def test_tag_snapshot_org_admin_bypasses_the_membership_gate() -> None:
    session = _make_team_session(is_member=False)
    with (
        _client_for(session, role="admin") as http,
        _patched([patch(f"{_PREFIX}tag_snapshot", new=AsyncMock(return_value=_make_snapshot(tag="prod")))]),
    ):
        resp = http.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}",
            json={"tag": "prod"},
        )
    assert resp.status_code == 200, resp.text


def test_tag_snapshot_in_txn_denial_overrides_a_passing_request_time_gate() -> None:
    """Same TOCTOU close as save-edit: the in-txn layer must answer 403."""
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}tag_snapshot", new=AsyncMock(return_value=_make_snapshot(tag="prod"))),
    ):
        resp = http.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}",
            json={"tag": "prod"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL

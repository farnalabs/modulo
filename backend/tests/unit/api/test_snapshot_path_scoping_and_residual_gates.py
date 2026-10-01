"""FAR-1360 (snapshot path scoping) + FAR-1362 (the residual one-layer endpoints).

FAR-1360 - ``PATCH /pipelines/{pipeline_id}/snapshots/{snapshot_id}`` and
``POST /pipelines/{pipeline_id}/snapshots/diff`` resolved their snapshot id(s)
by PRIMARY KEY ALONE, so the ``{pipeline_id}`` path segment was decorative: a
snapshot of ANOTHER pipeline could be tagged or diffed through a foreign path
on any layer where RLS does not apply. Both now scope the read to the path
pipeline (``tag_snapshot(organisation_id=..., pipeline_id=...)`` /
``diff_snapshots(..., pipeline_id=...)``) and answer 404 on a mismatch BEFORE
anything is written.

The sibling snapshot routes were audited in the same pass and already scope:
list (``WHERE pipeline_id``), detail + delete (``get_snapshot_detail(
pipeline_id=...)``), rollback (``rollback_to_snapshot`` compares
``target.pipeline_id != pipeline_id``), save-edit (creates for the path
pipeline), revert-to-manual (``get_snapshot_detail(pipeline_id=...)``).

FAR-1362 - ``diff_snapshot_endpoint`` carried the request-time dependency but
no in-txn re-check; ``restore`` / ``archive`` / ``unarchive`` carried the in-txn
re-check but no request-time dependency. Each now carries BOTH layers. ``restore``
cannot use the stock ``resolve_pipeline_team_scope``: that resolver filters
``deleted_at IS NULL`` and restore's entire target is a SOFT-DELETED row, so the
stock resolver would resolve ``None`` and the dependency would 404 every
non-admin restore before the handler - it uses
``_resolve_pipeline_team_scope_including_deleted`` instead (pinned below).

Session doubles answer by SQL text (never by call position) on top of the shared
``tests.unit.api.mock_session.configure_mock_session`` contract, in the shape of
``test_pipeline_graph_snapshot_team_gate.py``:

* ``FROM pipelines`` -> the pipeline row (team-private when the test says so),
  serving BOTH ``.scalar_one_or_none()`` (the in-txn gate / ``get_pipeline``)
  and ``.first()`` (the team-scope resolver's ``(owner_team_id, visibility)``),
* ``team_memberships`` -> a membership row iff the test says member,
* ``pipeline_snapshots`` -> the fixture snapshot, subject to the statement's own
  ``pipeline_id`` bind (the row iff that pipeline owns it), and always found by
  an unscoped read.
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
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
#: The pipeline the request path addresses (``/pipelines/{_PATH_PIPELINE_ID}/``).
_PATH_PIPELINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
#: The pipeline the fixture snapshot actually belongs to (a FOREIGN pipeline).
_OTHER_PIPELINE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SNAPSHOT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_OTHER_SNAPSHOT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_PREFIX = "modulo.api.routes.pipelines."
_NON_MEMBER_DETAIL = "Not a member of the team that owns this resource"
_SNAPSHOT_NOT_FOUND = "Snapshot not found"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_snapshot(pipeline_id: uuid.UUID) -> MagicMock:
    s = MagicMock()
    s.id = _SNAPSHOT_ID
    s.pipeline_id = pipeline_id
    s.organisation_id = _ORG_ID
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
    return s


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.id = _PATH_PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Scoping Test Pipeline"
    p.description = None
    p.visibility = "team"
    p.owner_team_id = _TEAM_ID
    p.folder_id = None
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.stale_run_timeout_minutes = 30
    p.retry_policy = {}
    p.archived_at = None
    p.snapshot_count = 0
    p.graph_nodes_json = []
    p.account_id = _USER_ID
    p.created_by = _USER_ID
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_session(
    *,
    is_member: bool = True,
    team_private: bool = False,
    snapshot_pipeline_id: uuid.UUID = _OTHER_PIPELINE_ID,
) -> AsyncMock:
    """Session double: ``configure_mock_session`` + pipelines / memberships / snapshots.

    ``snapshot_pipeline_id`` is the pipeline the ONE fixture snapshot belongs
    to. A ``pipeline_id`` bind resolves that row IFF it equals
    ``snapshot_pipeline_id`` - the real predicate's answer, read from the
    compiled bind values (the SQL text alone cannot tell a scoped read from an
    unscoped one, because the column list always mentions ``pipeline_id``).
    An UNSCOPED read always finds the row, which is what makes the scoping
    tests discriminating rather than vacuously green.
    """
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    base_execute = session.execute.side_effect

    pipeline_row = _make_pipeline()
    pipeline_row.visibility = "team" if team_private else "org"
    pipeline_row.owner_team_id = _TEAM_ID if team_private else None
    snapshot = _make_snapshot(snapshot_pipeline_id)
    snapshot_sql: list[str] = []

    async def _execute(stmt: object, *args: Any, **kwargs: Any) -> MagicMock:
        sql = str(stmt)
        if isinstance(stmt, Select) and "team_memberships" in sql:
            res = MagicMock()
            res.first.return_value = uuid.uuid4() if is_member else None
            return res
        if isinstance(stmt, Select) and "pipeline_snapshots" in sql:
            snapshot_sql.append(sql)
            # Read the BIND VALUES, not the SQL text: the column list alone
            # contains "pipeline_id", so a substring test can never tell a
            # scoped read from an unscoped one. A ``pipeline_id`` bind answers
            # exactly what the real predicate would - the row, iff the fixture
            # says that pipeline owns it.
            binds = dict(stmt.compile().params)
            scoped = [value for name, value in binds.items() if "pipeline_id" in str(name)]
            visible = scoped[0] == snapshot_pipeline_id if scoped else True
            res = MagicMock()
            res.scalar_one_or_none.return_value = snapshot if visible else None
            res.first.return_value = snapshot if visible else None
            res.scalars.return_value.all.return_value = [snapshot] if visible else []
            return res
        if isinstance(stmt, Select) and "FROM pipelines" in sql:
            res = MagicMock()
            res.scalar_one_or_none.return_value = pipeline_row
            res.first.return_value = (pipeline_row.owner_team_id, pipeline_row.visibility)
            res.scalars.return_value.all.return_value = [pipeline_row]
            return res
        return base_execute(stmt, *args, **kwargs)

    session.execute = AsyncMock(side_effect=_execute)
    session.snapshot_sql = snapshot_sql
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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
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


def _tag_url(snapshot_id: uuid.UUID = _SNAPSHOT_ID) -> str:
    return f"/api/v1/pipelines/{_PATH_PIPELINE_ID}/snapshots/{snapshot_id}"


def _diff_url() -> str:
    return f"/api/v1/pipelines/{_PATH_PIPELINE_ID}/snapshots/diff"


def _diff_body(snapshot_a: uuid.UUID = _SNAPSHOT_ID, snapshot_b: uuid.UUID = _OTHER_SNAPSHOT_ID) -> dict[str, str]:
    return {"snapshot_a_id": str(snapshot_a), "snapshot_b_id": str(snapshot_b)}


async def _tag_crud(session: AsyncMock, *, pipeline_id: uuid.UUID | None) -> Any:
    """Drive the REAL ``tag_snapshot`` with (or without) the route's scoping."""
    from modulo.db.crud.pipeline_snapshot_versioning import tag_snapshot

    kwargs: dict[str, Any] = {"tag": "prod"}
    if pipeline_id is not None:
        kwargs["pipeline_id"] = pipeline_id
        kwargs["organisation_id"] = _ORG_ID
    return await tag_snapshot(session, _SNAPSHOT_ID, **kwargs)


# ---------------------------------------------------------------------------
# FAR-1360: PATCH /snapshots/{snapshot_id} (tag) - the path segment must mean it
# ---------------------------------------------------------------------------


def test_tag_foreign_snapshot_via_a_foreign_path_is_404_and_writes_nothing() -> None:
    """A snapshot of pipeline B addressed through pipeline A's path 404s.

    The read is scoped (every ``pipeline_snapshots`` statement carries a
    ``pipeline_id`` predicate), so the row is never found, the CRUD returns
    ``None`` before touching any attribute, and ``session.flush`` - the write
    that would have persisted the tag - is never reached.
    """
    session = _make_session(snapshot_pipeline_id=_OTHER_PIPELINE_ID)
    with _client_for(session, role="operator") as http:
        resp = http.patch(_tag_url(), json={"tag": "prod"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == _SNAPSHOT_NOT_FOUND
    assert session.snapshot_sql, "the tag endpoint must read the snapshot row"
    assert all("pipeline_id" in sql for sql in session.snapshot_sql), session.snapshot_sql
    session.flush.assert_not_awaited()


def test_tag_snapshot_of_the_path_pipeline_succeeds() -> None:
    """The scoping does not break the in-path case (real CRUD, no patch)."""
    session = _make_session(snapshot_pipeline_id=_PATH_PIPELINE_ID)
    with _client_for(session, role="operator") as http:
        resp = http.patch(_tag_url(), json={"tag": "prod", "notes": "release"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["tag"] == "prod"


async def test_tag_crud_scoping_discriminates_on_pipeline_id() -> None:
    """Scoped reads resolve IFF the fixture snapshot belongs to that pipeline.

    The route always scopes to ``_PATH_PIPELINE_ID``, so the double's rule is
    "a ``pipeline_id``-scoped read finds the fixture row only when the fixture
    says the path pipeline owns it". Three legs pin that the empty answer is
    caused by the SCOPE and not by a double that simply always answers "no
    row":

    * fixture owner = foreign pipeline -> the route's scoped read misses,
    * the SAME session with NO scope still finds the row (the discriminator),
    * fixture owner = path pipeline -> the identical scoped read resolves.
    """
    foreign_owner = _make_session(snapshot_pipeline_id=_OTHER_PIPELINE_ID)
    path_owner = _make_session(snapshot_pipeline_id=_PATH_PIPELINE_ID)

    assert await _tag_crud(foreign_owner, pipeline_id=_PATH_PIPELINE_ID) is None
    assert await _tag_crud(foreign_owner, pipeline_id=None) is not None

    found = await _tag_crud(path_owner, pipeline_id=_PATH_PIPELINE_ID)
    assert found is not None
    assert found.tag == "prod"


# ---------------------------------------------------------------------------
# FAR-1360: POST /snapshots/diff - BOTH ids scoped to the path pipeline
# ---------------------------------------------------------------------------


def test_diff_of_a_foreign_snapshot_is_404() -> None:
    """A diff whose snapshots belong to another pipeline 404s (real CRUD).

    ``diff_snapshots`` loads each side and compares ``.pipeline_id`` against
    the path pipeline; the route passes it, so a foreign side returns ``None``
    and the endpoint's existing "One or both snapshots not found" 404 fires.
    """
    session = _make_session(snapshot_pipeline_id=_OTHER_PIPELINE_ID)
    with _client_for(session, role="operator") as http:
        resp = http.post(_diff_url(), json=_diff_body())

    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"].lower()
    session.flush.assert_not_awaited()


def test_diff_of_the_path_pipeline_snapshots_succeeds() -> None:
    """Both sides in-path -> the real diff runs (the scoping is not vacuous)."""
    session = _make_session(snapshot_pipeline_id=_PATH_PIPELINE_ID)
    with _client_for(session, role="operator") as http:
        resp = http.post(_diff_url(), json=_diff_body())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot_a"]["id"] == str(_SNAPSHOT_ID)


def test_diff_passes_the_path_pipeline_to_the_crud() -> None:
    """Pin the contract the 404 above depends on: ``pipeline_id=`` is passed."""
    session = _make_session(snapshot_pipeline_id=_PATH_PIPELINE_ID)
    spy = AsyncMock(return_value=None)
    with (
        _client_for(session, role="operator") as http,
        patch(f"{_PREFIX}diff_snapshots", new=spy),
    ):
        resp = http.post(_diff_url(), json=_diff_body())

    assert resp.status_code == 404, resp.text
    spy.assert_awaited_once()
    assert spy.await_args.kwargs.get("pipeline_id") == _PATH_PIPELINE_ID


# ---------------------------------------------------------------------------
# FAR-1362: diff - the missing in-txn layer
# ---------------------------------------------------------------------------


def test_diff_non_member_is_denied_403() -> None:
    session = _make_session(is_member=False, team_private=True, snapshot_pipeline_id=_PATH_PIPELINE_ID)
    with _client_for(session, role="operator") as http:
        resp = http.post(_diff_url(), json=_diff_body())

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
    session.flush.assert_not_awaited()


def test_diff_in_txn_denial_overrides_a_passing_request_time_gate() -> None:
    """The request-time gate PASSES, the in-txn re-check DENIES -> still 403.

    The dependency resolves membership through ``modulo.api.dependencies``'
    own import (the session answers a membership row), while the in-txn
    re-check resolves it through the one imported into ``routes.pipelines`` -
    patched here to deny, exactly as an ownership flip inside the
    request -> handler window would behave. Proves the new in-txn layer on
    ``diff`` is wired and is not a no-op behind a passing dependency.
    """
    session = _make_session(is_member=True, team_private=True, snapshot_pipeline_id=_PATH_PIPELINE_ID)
    with (
        _client_for(session, role="operator") as http,
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
    ):
        resp = http.post(_diff_url(), json=_diff_body())

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
    session.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-1362: archive / unarchive / restore - the missing request-time layer
# ---------------------------------------------------------------------------


def _lifecycle_url(action: str) -> str:
    return f"/api/v1/pipelines/{_PATH_PIPELINE_ID}/{action}"


def _lifecycle_patches(action: str) -> list:
    crud = {"archive": "archive_pipeline", "unarchive": "unarchive_pipeline", "restore": "restore_pipeline"}[action]
    return [
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}{crud}", new=AsyncMock(return_value=_make_pipeline())),
    ]


def test_lifecycle_non_member_is_denied_403() -> None:
    for action in ("archive", "unarchive", "restore"):
        session = _make_session(is_member=False, team_private=True)
        with _client_for(session, role="operator") as http:
            resp = http.post(_lifecycle_url(action))

        assert resp.status_code == 403, f"{action}: {resp.text}"
        assert resp.json()["detail"] == _NON_MEMBER_DETAIL
        session.flush.assert_not_awaited()


def test_lifecycle_member_succeeds() -> None:
    for action in ("archive", "unarchive", "restore"):
        session = _make_session(is_member=True, team_private=True)
        with _client_for(session, role="operator") as http, _patched(_lifecycle_patches(action)):
            resp = http.post(_lifecycle_url(action))

        assert resp.status_code == 200, f"{action}: {resp.text}"


def test_lifecycle_org_admin_bypasses_the_membership_gate() -> None:
    for action in ("archive", "unarchive", "restore"):
        session = _make_session(is_member=False, team_private=True)
        with _client_for(session, role="admin") as http, _patched(_lifecycle_patches(action)):
            resp = http.post(_lifecycle_url(action))

        assert resp.status_code == 200, f"{action}: {resp.text}"


def test_lifecycle_in_txn_denial_overrides_a_passing_request_time_gate() -> None:
    """Same TOCTOU close as diff: dependency says member, locked row says no."""
    for action in ("archive", "unarchive", "restore"):
        session = _make_session(is_member=True, team_private=True)
        with (
            _client_for(session, role="operator") as http,
            _patched(_lifecycle_patches(action)),
            patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
        ):
            resp = http.post(_lifecycle_url(action))

        assert resp.status_code == 403, f"{action}: {resp.text}"
        assert resp.json()["detail"] == _NON_MEMBER_DETAIL


# ---------------------------------------------------------------------------
# FAR-1362: restore's resolver must NOT filter deleted rows
# ---------------------------------------------------------------------------


async def test_restore_resolver_opts_out_of_the_global_soft_delete_filter() -> None:
    """The one reason restore cannot reuse the stock resolver.

    A soft-deleted row is hidden from a plain ORM SELECT TWICE: the stock
    ``resolve_pipeline_team_scope`` adds an explicit ``deleted_at IS NULL``
    predicate, AND a global ``do_orm_execute`` listener
    (``db.soft_delete._apply_soft_delete_filter``) injects the same clause into
    every SELECT on a ``SoftDeleteMixin`` model. Restore's resolver must carry
    NEITHER - it opts out with ``include_soft_deleted``, whose only observable
    trace on an unexecuted statement is the ``include_deleted`` execution
    option (the listener reads exactly that flag to skip the injection).
    """
    from modulo.api.routes.pipelines import _resolve_pipeline_team_scope_including_deleted
    from modulo.api.team_scope import resolve_pipeline_team_scope

    request = MagicMock()
    request.path_params = {"pipeline_id": str(_PATH_PIPELINE_ID)}

    stock_stmts: list[Any] = []
    restore_stmts: list[Any] = []

    def _recorder(bucket: list[Any]) -> AsyncMock:
        async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
            bucket.append(stmt)
            res = MagicMock()
            res.first.return_value = (_TEAM_ID, "team")
            return res

        return AsyncMock(side_effect=_execute)

    stock_session = AsyncMock()
    stock_session.execute = _recorder(stock_stmts)
    restore_session = AsyncMock()
    restore_session.execute = _recorder(restore_stmts)

    stock = await resolve_pipeline_team_scope(request, stock_session)
    restored = await _resolve_pipeline_team_scope_including_deleted(request, restore_session)

    assert stock is not None
    assert restored is not None
    assert restored.owner_team_id == _TEAM_ID
    assert restored.visibility == "team"
    # The stock statement carries the explicit deleted filter...
    assert stock_stmts and "deleted_at" in str(stock_stmts[0]), stock_stmts
    # ...and neither predicate: no explicit one, and the execution option that
    # makes the global listener skip its injection is set.
    assert restore_stmts and "deleted_at" not in str(restore_stmts[0]), restore_stmts
    assert restore_stmts[0].get_execution_options().get("include_deleted") is True, restore_stmts[0]


async def test_restore_resolver_missing_row_returns_none() -> None:
    """Fail-closed parity with the stock resolver: no row -> None -> 404."""
    from modulo.api.routes.pipelines import _resolve_pipeline_team_scope_including_deleted

    request = MagicMock()
    request.path_params = {"pipeline_id": str(_PATH_PIPELINE_ID)}
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = None
    session.execute = AsyncMock(return_value=result)

    assert await _resolve_pipeline_team_scope_including_deleted(request, session) is None


async def test_restore_resolver_missing_path_param_returns_none() -> None:
    from modulo.api.routes.pipelines import _resolve_pipeline_team_scope_including_deleted

    request = MagicMock()
    request.path_params = {}
    session = AsyncMock()

    assert await _resolve_pipeline_team_scope_including_deleted(request, session) is None
    session.execute.assert_not_awaited()

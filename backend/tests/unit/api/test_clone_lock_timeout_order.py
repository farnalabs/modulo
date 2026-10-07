"""FAR-1313: the clone endpoint's ``lock_timeout`` bound must precede its first lock.

``_clone_pipeline_into_org`` -> ``check_pipeline_name_available`` issues
``SELECT ... FOR UPDATE`` on the target-name row as the FIRST lock of the clone
mutation transaction. The transaction-scoped bound
(``set_mutation_row_lock_timeout`` (db.crud.row_lock) ->
``set_config('lock_timeout', <ms>, true)``,
i.e. ``SET LOCAL``) used to be set only inside
``_reapply_team_gate_inside_mutation_txn`` - which the clone endpoint NEVER
calls - so a concurrent holder of a row with that exact target name parked the
request unbounded, contradicting the module's stated invariant ("no part of a
request-scoped mutation should hang indefinitely").

Both tests run against a session double whose bind reports ``postgresql`` (so
the dialect gate lets the ``set_config`` through) and which RECORDS every
statement's SQL, so the ordering claim is an observation rather than an
inference:

* the endpoint sets the bound BEFORE the name check's ``FOR UPDATE``,
* the shared gate helper still sets it before its own ``FOR UPDATE`` (the
  FAR-1313 refactor into a shared helper must not have moved it).

The real-contention half of FAR-1313 (a held name-row lock degrading to the
mapped 409 inside the bound) is covered against real Postgres by
``tests/integration/test_pipeline_mutation_lock_timeout.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.pipelines import _reapply_team_gate_inside_mutation_txn
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.test_pipelines_endpoint import _make_pipeline

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TEAM_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_PREFIX = "modulo.api.routes.pipelines."


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


def _recording_session() -> tuple[AsyncMock, list[str]]:
    """A session double that REPORTS ``postgresql`` and records every statement.

    Recording is the point: the ordering the fix establishes is only proven by
    observing the statement sequence, not by reading the code that emits it.
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
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)

    executed: list[str] = []

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        executed.append(sql)
        if "set_config" in sql:
            return _result()
        if "authz_enforce" in sql:
            return _result(scalar_one_or_none=None)
        if isinstance(stmt, Select) and "FROM pipelines" in sql:
            if "FOR UPDATE" in sql.upper():
                # check_pipeline_name_available: no row with the target name
                # -> the name is available and the clone proceeds.
                return _result(scalar_one_or_none=None)
            # get_pipeline: the source row exists.
            return _result(scalar_one_or_none=_make_pipeline())
        return _result()

    session.execute = AsyncMock(side_effect=_execute)
    return session, executed


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


def _lock_timeout_indexes(executed: list[str]) -> list[int]:
    return [i for i, sql in enumerate(executed) if "set_config" in sql and "lock_timeout" in sql]


def _first_for_update_index(executed: list[str]) -> int:
    for i, sql in enumerate(executed):
        if "FOR UPDATE" in sql.upper() and "FROM pipelines" in sql:
            return i
    raise AssertionError("no SELECT ... FOR UPDATE on pipelines was issued:\n" + "\n".join(executed))


def test_clone_sets_the_lock_bound_before_the_name_check_for_update() -> None:
    """The bound is set (transaction-scoped) BEFORE the name check locks."""
    session, executed = _recording_session()
    cloned = _make_pipeline()
    cloned.id = uuid.uuid4()
    cloned.name = "Copy of Test Pipeline"

    with (
        _client_for(session, role="admin") as http,
        _patched(
            [
                patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
                patch(f"{_PREFIX}clone_pipeline", new=AsyncMock(return_value=cloned)),
                patch(f"{_PREFIX}append_audit_event", new=AsyncMock()),
            ]
        ),
    ):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={"name": "Copy of Test Pipeline"})

    assert resp.status_code == 201, resp.text

    lock_indexes = _lock_timeout_indexes(executed)
    assert lock_indexes, "the clone transaction never set the lock_timeout bound:\n" + "\n".join(executed)
    lock_sql = executed[lock_indexes[0]]
    # Transaction-scoped (SET LOCAL): is_local => true, else the bound would
    # leak past the request onto the pooled connection.
    assert ", true)" in lock_sql, lock_sql
    assert lock_indexes[0] < _first_for_update_index(executed), (
        "the bound was set AFTER the first FOR UPDATE - the name check could "
        "park unbounded:\n" + "\n".join(f"{i}: {sql}" for i, sql in enumerate(executed))
    )


async def test_team_gate_helper_still_sets_the_bound_before_its_own_lock() -> None:
    """Regression guard for the FAR-1313 helper extraction.

    The bound moved out of ``_reapply_team_gate_inside_mutation_txn`` into
    ``set_mutation_row_lock_timeout`` (db.crud.row_lock); the gate's own ``FOR UPDATE`` must
    still be preceded by it.
    """
    session, executed = _recording_session()

    # The gate's locked re-select returns a team-private row; as an org admin
    # the caller short-circuits after the lock, so no membership query runs.
    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        executed.append(sql)
        if "set_config" in sql:
            return _result()
        if isinstance(stmt, Select) and "FROM pipelines" in sql:
            row = MagicMock()
            row.visibility = "team"
            row.owner_team_id = _TEAM_ID
            return _result(scalar_one_or_none=row)
        return _result()

    session.execute = AsyncMock(side_effect=_execute)

    principal = TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    locked = await _reapply_team_gate_inside_mutation_txn(session, principal, _PIPELINE_ID)
    assert locked is not None

    lock_indexes = _lock_timeout_indexes(executed)
    assert lock_indexes, "the gate helper never set the lock_timeout bound"
    assert lock_indexes[0] < _first_for_update_index(executed), (
        "the gate helper's FOR UPDATE ran before the bound was set:\n"
        + "\n".join(f"{i}: {sql}" for i, sql in enumerate(executed))
    )

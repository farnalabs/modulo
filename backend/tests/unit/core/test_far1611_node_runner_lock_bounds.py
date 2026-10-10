"""FAR-1611 — bounded row-lock waits on the remaining node_runner marker writers.

The FAR-1601 class sweep of ``core/pipeline_engine/node_runner.py`` covered
only the two sites that were already bounded CLIENT-side by
``asyncio.wait_for``. Four more hot-``runs`` writers in the same file had NEITHER
a client bound NOR the transaction-scoped ``SET LOCAL lock_timeout`` bound
(``Settings.mutation_row_lock_timeout_ms`` — the same
``db.crud.row_lock.set_mutation_row_lock_timeout`` helper FAR-1584 / FAR-1592 /
FAR-1601 issue). Three of them are bounded here, each with a NON-silent 55P03
contract chosen for its path; the fourth — the sandbox-id store — is deliberately
NOT bounded (see :class:`TestSandboxIdStoreDeliberatelyUnbounded`).

These tests pin, per bounded writer:

1. **Wiring + ORDER** (fail-first): the bound statement is issued BEFORE the
   writer's ``UPDATE runs`` takes its row lock, carries ``is_local => true``
   (transaction-scoped, never leaks onto a pooled connection), and its value
   comes from the operator knob (``mutation_row_lock_timeout_ms``). Without the
   source change no bound statement exists and every ordering assertion fails.
2. **The 55P03 contract**, chosen per path: the best-effort acquire marker
   fails open with a DISTINCT warning (never confused with a real DB fault);
   the script lease claims the expiry loudly and RE-RAISES (the fence must not
   be lost, the script must not start); the teardown clear claims the expiry
   loudly and swallows it (the marker sweep owns the surviving marker).

The real-Postgres contention behaviour (a held row lock actually timing out) is
an integration concern; the unit seam here drives the same 55P03 the bound
produces via the real asyncpg ``LockNotAvailableError`` chain ``is_row_lock_timeout``
walks.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from modulo.core.pipeline_engine import node_runner as nr

_LOGGER = "modulo.core.pipeline_engine.node_runner"
_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_RUN_ID = "run-1"


def _lock_timeout_error(statement: str) -> OperationalError:
    """Simulated row-lock contention: asyncpg's real ``LockNotAvailableError``
    (SQLSTATE 55P03) wrapped exactly the way SQLAlchemy surfaces it — the shape
    ``modulo.db.sqlstates.is_row_lock_timeout`` recognises."""
    from asyncpg import exceptions as asyncpg_exceptions

    driver_error = asyncpg_exceptions.LockNotAvailableError("canceling statement due to lock timeout")
    return OperationalError(statement, {}, driver_error)


class _PgResult:
    def __init__(self, row: tuple[Any, ...] | None = None, *, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _PgSession:
    """Async session double reporting the postgresql dialect, recording SQL.

    ``get_bind()`` returns a bind whose ``dialect.name`` is ``"postgresql"`` so
    BOTH the RLS preamble and the FAR-1611 lock bound take their LIVE branch and
    the real ``set_config(...)`` statements are recorded. ``raise_on_contains``
    raises a 55P03 lock-timeout error when the named statement fragment runs
    (the bound statement itself never matches).
    """

    def __init__(
        self,
        *,
        claim_count: int = 0,
        raise_on_contains: str | None = None,
    ) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any] | None] = []
        self._claim_count = claim_count
        self._raise_on_contains = raise_on_contains

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        return bind

    async def execute(self, stmt: object, params: dict[str, Any] | None = None) -> _PgResult:
        sql = str(stmt)
        self.statements.append(sql)
        self.params.append(params)
        if self._raise_on_contains is not None and self._raise_on_contains in sql:
            raise _lock_timeout_error(sql)
        if "SELECT claim_count FROM runs" in sql:
            return _PgResult(row=(self._claim_count,))
        return _PgResult(row=("id",), rowcount=1)


def _bound_statements(session: _PgSession) -> list[int]:
    return [i for i, sql in enumerate(session.statements) if "set_config('lock_timeout'" in sql]


def _update_statements(session: _PgSession, marker: str) -> list[int]:
    return [i for i, sql in enumerate(session.statements) if marker in sql]


def _assert_bound_precedes_update(session: _PgSession, update_marker: str) -> int:
    """Shared wiring assertion: the bound fires first, locally, at knob value."""
    bound_at = _bound_statements(session)
    update_at = _update_statements(session, update_marker)
    assert bound_at, f"no transaction-local lock_timeout bound issued; statements={session.statements}"
    assert update_at, f"the writer's UPDATE never ran; statements={session.statements}"
    assert bound_at[0] < update_at[0], (
        f"the bound must be set BEFORE the row lock (lock_timeout at {bound_at[0]}, UPDATE at {update_at[0]})"
    )
    # SET LOCAL semantics: set_config(..., is_local => true).
    assert ", true)" in session.statements[bound_at[0]]
    # The value comes from the operator knob, not a hardcoded literal.
    bound_params = session.params[bound_at[0]]
    assert bound_params is not None
    assert bound_params["val"] == "5000ms"
    return bound_at[0]


# ---------------------------------------------------------------------------
# Wiring + ORDER — every bounded writer issues the bound before its row lock
# ---------------------------------------------------------------------------


class TestBoundedWritersIssueTheBoundFirst:
    async def test_best_effort_marker_bounds_before_its_update(self) -> None:
        session = _PgSession(claim_count=3)
        key = await nr._sandbox_acquire_dispatch_marker_best_effort(
            session_factory=lambda: session,
            claim_lease="tok",
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            node_id="n1",
            provider="e2b",
        )
        assert key == f"run:{_RUN_ID}:node:n1:3"
        _assert_bound_precedes_update(session, "UPDATE runs SET sandbox_dispatch_state")

    async def test_script_lease_bounds_before_its_update(self) -> None:
        session = _PgSession()
        await nr._sandbox_store_script_lease(
            session_factory=lambda: session,
            claim_lease="tok",
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            attempt_key="run:run-1:node:n1:0",
            provider="e2b",
        )
        _assert_bound_precedes_update(session, "UPDATE runs SET sandbox_dispatch_state")

    async def test_clear_marker_bounds_before_its_update(self) -> None:
        session = _PgSession()
        await nr._sandbox_clear_dispatch_marker(
            session_factory=lambda: session,
            claim_lease="tok",
            org_id=_ORG_ID,
            run_id=_RUN_ID,
        )
        _assert_bound_precedes_update(session, "UPDATE runs SET sandbox_dispatch_state=NULL")


# ---------------------------------------------------------------------------
# 55P03 contracts — non-silent, path-appropriate
# ---------------------------------------------------------------------------


class TestLockTimeoutHandling:
    async def test_best_effort_marker_lock_timeout_fails_open_with_distinct_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The best-effort acquire marker 55P03s → the SAME markerless fail-open
        fallback key, claimed under the DISTINCT lock-timeout event (never the
        generic DB-fault event), with the bound proven to have preceded it."""
        session = _PgSession(claim_count=3, raise_on_contains="UPDATE runs SET sandbox_dispatch_state")
        with caplog.at_level("WARNING", logger=_LOGGER):
            key = await nr._sandbox_acquire_dispatch_marker_best_effort(
                session_factory=lambda: session,
                claim_lease="tok",
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id="n1",
                provider="e2b",
            )
        assert key == f"run:{_RUN_ID}:node:n1:{nr._claim_token_attempt_suffix('tok')}"
        assert any("best_effort_marker_lock_timeout" in message for message in caplog.messages)
        assert not any("best_effort_marker_failed" in message for message in caplog.messages)
        _assert_bound_precedes_update(session, "UPDATE runs SET sandbox_dispatch_state")

    async def test_best_effort_marker_generic_failure_keeps_its_own_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-lock DB failure keeps the pre-existing generic event — the two
        stay distinguishable."""
        session = _PgSession(claim_count=3)
        original_execute = session.execute

        async def _execute(stmt: object, params: dict[str, Any] | None = None) -> _PgResult:
            if "UPDATE runs SET sandbox_dispatch_state" in str(stmt):
                raise RuntimeError("connection reset")
            return await original_execute(stmt, params)

        session.execute = _execute  # type: ignore[method-assign]
        with caplog.at_level("WARNING", logger=_LOGGER):
            key = await nr._sandbox_acquire_dispatch_marker_best_effort(
                session_factory=lambda: session,
                claim_lease="tok",
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id="n1",
                provider="e2b",
            )
        assert key == f"run:{_RUN_ID}:node:n1:{nr._claim_token_attempt_suffix('tok')}"
        assert any("best_effort_marker_failed" in message for message in caplog.messages)
        assert not any("best_effort_marker_lock_timeout" in message for message in caplog.messages)

    async def test_script_lease_lock_timeout_warns_and_reraises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The script lease is the exactly-once fence: a 55P03 must NOT fail
        open — it is claimed loudly and re-raised so the caller never marks the
        lease claimed and the script never starts."""
        session = _PgSession(raise_on_contains="UPDATE runs SET sandbox_dispatch_state")
        with caplog.at_level("WARNING", logger=_LOGGER), pytest.raises(OperationalError):
            await nr._sandbox_store_script_lease(
                session_factory=lambda: session,
                claim_lease="tok",
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                attempt_key="run:run-1:node:n1:0",
                provider="e2b",
            )
        assert any("script_lease_lock_timeout" in message for message in caplog.messages)
        _assert_bound_precedes_update(session, "UPDATE runs SET sandbox_dispatch_state")

    async def test_script_lease_superseded_still_raises_superseded(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The new try/except must not swallow the fenced (rowcount 0) case:
        it keeps raising SupersededNodeError and claims NO lock-timeout event."""
        session = _PgSession()

        async def _execute(stmt: object, params: dict[str, Any] | None = None) -> _PgResult:
            result = await _PgSession.execute(session, stmt, params)
            if "UPDATE runs SET sandbox_dispatch_state" in str(stmt):
                return _PgResult(row=None, rowcount=0)
            return result

        session.execute = _execute  # type: ignore[method-assign]
        with caplog.at_level("WARNING", logger=_LOGGER), pytest.raises(nr.SupersededNodeError):
            await nr._sandbox_store_script_lease(
                session_factory=lambda: session,
                claim_lease="tok",
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                attempt_key="run:run-1:node:n1:0",
                provider="e2b",
            )
        assert not any("script_lease_lock_timeout" in message for message in caplog.messages)

    async def test_clear_marker_lock_timeout_warns_and_swallows(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The teardown clear is best-effort: a 55P03 is claimed loudly and
        swallowed — the marker survives and the sweep owns clearing it. No
        exception escapes to the caller's generic clear-failed handler."""
        session = _PgSession(raise_on_contains="UPDATE runs SET sandbox_dispatch_state=NULL")
        with caplog.at_level("WARNING", logger=_LOGGER):
            await nr._sandbox_clear_dispatch_marker(
                session_factory=lambda: session,
                claim_lease="tok",
                org_id=_ORG_ID,
                run_id=_RUN_ID,
            )
        assert any("dispatch_marker_clear_lock_timeout" in message for message in caplog.messages)
        _assert_bound_precedes_update(session, "UPDATE runs SET sandbox_dispatch_state=NULL")

    async def test_clear_marker_generic_failure_still_propagates(self) -> None:
        """Only 55P03 is swallowed; any other DB failure keeps propagating to
        the caller's existing ``dispatch_marker_clear_failed`` handler."""
        session = _PgSession()
        original_execute = session.execute

        async def _execute(stmt: object, params: dict[str, Any] | None = None) -> _PgResult:
            if "UPDATE runs SET sandbox_dispatch_state=NULL" in str(stmt):
                raise RuntimeError("connection reset")
            return await original_execute(stmt, params)

        session.execute = _execute  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="connection reset"):
            await nr._sandbox_clear_dispatch_marker(
                session_factory=lambda: session,
                claim_lease="tok",
                org_id=_ORG_ID,
                run_id=_RUN_ID,
            )


# ---------------------------------------------------------------------------
# The sandbox-id store is deliberately NOT bounded (FAR-1611)
# ---------------------------------------------------------------------------


class TestSandboxIdStoreDeliberatelyUnbounded:
    async def test_sandbox_id_store_issues_no_lock_bound(self) -> None:
        """``_sandbox_store_dispatch_marker_sandbox`` persists ``runs.sandbox_id``
        — the ONLY durable record the heartbeat-lost kill reads, with no
        Modulo-side sweep that re-creates a lost id. A bounded wait expiring on
        transient contention would drop that record with no recovery, so the
        bound is deliberately NOT added; this pins the decision against a future
        well-meaning sweep."""
        session = _PgSession()
        await nr._sandbox_store_dispatch_marker_sandbox(
            "sbx-1",
            session_factory=lambda: session,
            claim_lease="tok",
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            attempt_key="run:run-1:node:n1:0",
            provider="e2b",
        )
        assert not _bound_statements(session), (
            f"the sandbox-id store must stay unbounded (FAR-1611); statements={session.statements}"
        )
        assert _update_statements(session, "UPDATE runs SET sandbox_dispatch_state")

    async def test_sandbox_id_store_propagates_a_lock_timeout(self) -> None:
        """Because it is unbounded AND its caller has no dedicated 55P03
        contract, a lock timeout (were one to fire) propagates — this documents
        the audited caller contract that keeps the bound off this site."""
        session = _PgSession(raise_on_contains="UPDATE runs SET sandbox_dispatch_state")
        with pytest.raises(OperationalError):
            await nr._sandbox_store_dispatch_marker_sandbox(
                "sbx-1",
                session_factory=lambda: session,
                claim_lease="tok",
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                attempt_key="run:run-1:node:n1:0",
                provider="e2b",
            )


# ---------------------------------------------------------------------------
# Faithful PG path — the bound reads the postgresql dialect off the session bind
# ---------------------------------------------------------------------------


class TestBoundUsesTheSharedHelper:
    async def test_bound_value_comes_from_the_operator_knob(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bound is the shared helper, so the value tracks
        ``Settings.mutation_row_lock_timeout_ms`` — assert a relaxed knob changes
        the recorded ``set_config`` value. The knob is read by the shared helper
        (``db.crud.row_lock``), so the patch targets THAT module."""
        import modulo.db.crud.row_lock as _row_lock

        monkeypatch.setattr(_row_lock, "get_settings", lambda: SimpleNamespace(mutation_row_lock_timeout_ms=1234))
        session = _PgSession()
        await nr._sandbox_clear_dispatch_marker(
            session_factory=lambda: session,
            claim_lease="tok",
            org_id=_ORG_ID,
            run_id=_RUN_ID,
        )
        bound_at = _bound_statements(session)
        assert bound_at
        bound_params = session.params[bound_at[0]]
        assert bound_params is not None
        assert bound_params["val"] == "1234ms"

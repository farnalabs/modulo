"""Unit tests for the FAR-583 run-outputs failure orchestration.

Covers the core-side surface (:mod:`modulo.core.run_outputs_dualwrite`) and the
extended ``saq_hooks._mark_run_failed`` primitive:

* ``orchestrate_dual_write_failure`` — the separate-session terminalize with
  ``error_code='dual_write_failed'`` + bounded lock_timeout, and the
  best-effort error event;
* ``guard_dual_write`` — rollback-BEFORE-orchestrate ordering, then re-raise;
* ``_mark_run_failed``'s guard extension — the FULL terminal vocabulary plus
  the explicit ``'unknown'`` exclusion (an unknown run is never transitioned),
  driven against a real SQLite engine so the raw SQL actually executes;
* the event-suppression window's Redis leg — bounded client, TTL at
  creation, the expiry-in-gap heal.

The dual-write kill-switch, its Redis/counters machinery, and the degraded
signal were removed at B2a (FAR-694) — the store write is unconditional, so
their tests went with the machinery.

DB-backed cases run on in-memory SQLite with ``Base.metadata.create_all`` over
the involved tables only (no migrations): the raw UPDATE's Postgres-style
``now()`` is exposed via ``create_function`` (the
``test_run_classification`` precedent).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.error_tracking import saq_hooks
from modulo.core.run_outputs_dualwrite import (
    _redis_incr_window,
    guard_dual_write,
    orchestrate_dual_write_failure,
)
from modulo.db.crud.run_node_outputs import DualWriteError
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.run_node_outputs import RunNodeOutput

_ORG = uuid.uuid4()
_PIPELINE = uuid.uuid4()
_SNAPSHOT = uuid.uuid4()

_RUN_AND_ORG_TABLES = (Organisation.__table__, Run.__table__, RunNodeOutput.__table__)


def _dual_write_error(**overrides: Any) -> DualWriteError:
    run_id = overrides.pop("run_id", uuid.uuid4())
    org_id = overrides.pop("organisation_id", _ORG)
    return DualWriteError(
        "injected dual-write failure",
        run_id=run_id,
        organisation_id=org_id,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


class _FakeRedis:
    """A scripted async Redis double for the Redis-window helpers."""

    def __init__(self, *, get_value: Any = None, get_error: Exception | None = None) -> None:
        self.get_value = get_value
        self.get_error = get_error
        self.set_calls: list[dict[str, Any]] = []
        self.expire_calls: list[dict[str, Any]] = []
        self.get_calls: list[Any] = []
        self.closed = False

    async def get(self, key: Any) -> Any:
        self.get_calls.append(key)
        if self.get_error is not None:
            raise self.get_error
        return self.get_value

    async def set(self, key: Any, value: Any, **kwargs: Any) -> Any:
        self.set_calls.append({"key": key, "value": value, **kwargs})
        return True

    async def expire(self, key: Any, ttl: Any, **kwargs: Any) -> Any:
        self.expire_calls.append({"key": key, "ttl": ttl, **kwargs})
        return True

    async def incr(self, key: Any) -> int:
        self.set_calls.append({"key": key, "value": "__incr__"})
        return 1

    async def incrby(self, key: Any, amount: int) -> int:
        self.set_calls.append({"key": key, "value": f"__incrby__{amount}"})
        return amount

    async def aclose(self) -> None:
        self.closed = True


def _redis_client(**kwargs: Any) -> _FakeRedis:
    return _FakeRedis(**kwargs)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class TestOrchestration:
    @pytest.mark.asyncio
    async def test_terminalize_uses_error_code_and_lock_timeout(self) -> None:
        exc = _dual_write_error(claim_token="tok-1", sqlstate="42501", origin="update_run_status.orm")
        with (
            patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, return_value=1) as mark,
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock),
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 1
        mark.assert_awaited_once()
        kwargs = mark.await_args.kwargs
        assert kwargs["error_code"] == "dual_write_failed"
        assert kwargs["claim_token"] == "tok-1"
        assert kwargs["lock_timeout_ms"] is not None
        assert mark.await_args.args == (str(exc.run_id), str(_ORG))

    @pytest.mark.asyncio
    async def test_rowcount_zero_superseded_still_returns_zero(self) -> None:
        exc = _dual_write_error()
        with (
            patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, return_value=0) as mark,
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock),
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 0
        mark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_event_failure_is_swallowed(self) -> None:
        exc = _dual_write_error()
        with (
            patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, return_value=1),
            patch(
                "modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ingest down"),
            ),
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 1

    @pytest.mark.asyncio
    async def test_terminalize_failure_is_swallowed_and_event_still_fires(self) -> None:
        """qa M10b rider: the terminalize and the event legs are failure-
        isolated — a DB-down terminalize never suppresses the event."""
        exc = _dual_write_error()
        with (
            patch.object(
                saq_hooks,
                "_mark_run_failed",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock) as emit,
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 0
        emit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_guard_rolls_back_before_orchestrating_then_reraises(self) -> None:
        session = AsyncMock()
        exc = _dual_write_error()
        order: list[str] = []

        async def _rollback() -> None:
            order.append("rollback")

        async def _orchestrate(*args: Any, **kwargs: Any) -> int:
            order.append("orchestrate")
            return 1

        session.rollback = _rollback
        with (
            patch(
                "modulo.core.run_outputs_dualwrite.orchestrate_dual_write_failure",
                _orchestrate,
            ),
            pytest.raises(DualWriteError) as caught,
        ):
            async with guard_dual_write(session):
                raise exc
        assert caught.value is exc
        assert order[0] == "rollback"
        assert order[1] == "orchestrate"


# ---------------------------------------------------------------------------
# _mark_run_failed guard extension (real SQLite engine)
# ---------------------------------------------------------------------------


async def _now_sqlite(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_now(dbapi_connection: Any, connection_record: Any) -> None:
        dbapi_connection.create_function("now", 0, lambda: datetime.now(UTC).isoformat())


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    await _now_sqlite(eng)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_RUN_AND_ORG_TABLES))
        # Production _mark_run_failed never hits FK-adjacent tables; the runs
        # FK to organisations/pipelines would fail create-order inserts, so the
        # reference is left unenforced exactly like the
        # test_run_classification harness.
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
def sqlite_sessionmaker(sqlite_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)


async def _seed_run(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    status: str = "running",
    claim_token: str | None = "tok-a",
    organisation_id: uuid.UUID = _ORG,
    run_id_text: str | None = None,
    org_id_text: str | None = None,
) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, 'mark-run-failed org', :slug, '{}', '{}')"
            ),
            {"id": org_id_text or organisation_id.hex, "slug": f"mark-run-failed-{organisation_id.hex[:12]}"},
        )
        await session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id, claim_token, cancellation_requested) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :status, 1, 'ih', :thread, :tok, 0)"
            ),
            {
                "id": run_id_text or run_id.hex,
                "oid": org_id_text or organisation_id.hex,
                "pid": _PIPELINE.hex,
                "sid": _SNAPSHOT.hex,
                "status": status,
                "thread": f"mark-run-failed-{run_id}",
                "tok": claim_token,
            },
        )


async def _read_run(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    run_id_text: str | None = None,
) -> dict[str, Any]:
    async with maker() as session, session.begin():
        row = (
            await session.execute(
                text("SELECT status, error_code, claim_token FROM runs WHERE id = :rid"),
                {"rid": run_id_text or run_id.hex},
            )
        ).fetchone()
    assert row is not None
    return {"status": row[0], "error_code": row[1], "claim_token": row[2]}


class TestMarkRunFailedGuard:
    @pytest.mark.asyncio
    async def test_running_run_is_terminalized_with_custom_error_code(self, sqlite_sessionmaker: Any) -> None:
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
            rowcount = await saq_hooks._mark_run_failed(
                run_id.hex,
                _ORG.hex,
                claim_token="tok-a",
                error_code="dual_write_failed",
                error_detail="dual-write leg failed",
            )
        assert rowcount == 1
        row = await _read_run(sqlite_sessionmaker, run_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "dual_write_failed"

    @pytest.mark.asyncio
    async def test_default_error_code_is_task_failure(self, sqlite_sessionmaker: Any) -> None:
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
            rowcount = await saq_hooks._mark_run_failed(run_id.hex, _ORG.hex)
        assert rowcount == 1
        row = await _read_run(sqlite_sessionmaker, run_id)
        assert row["error_code"] == "task_failure"

    @pytest.mark.asyncio
    async def test_unknown_run_is_never_transitioned(self, sqlite_sessionmaker: Any) -> None:
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id, status="unknown")
        with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
            rowcount = await saq_hooks._mark_run_failed(
                run_id.hex,
                _ORG.hex,
                error_code="dual_write_failed",
            )
        assert rowcount == 0
        row = await _read_run(sqlite_sessionmaker, run_id)
        assert row["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_claim_token_fence_honored(self, sqlite_sessionmaker: Any) -> None:
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id, claim_token="tok-current")
        with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
            rowcount = await saq_hooks._mark_run_failed(
                run_id.hex,
                _ORG.hex,
                claim_token="tok-stale",
                error_code="dual_write_failed",
            )
        assert rowcount == 0
        row = await _read_run(sqlite_sessionmaker, run_id)
        assert row["status"] == "running"
        assert row["claim_token"] == "tok-current"

    @pytest.mark.asyncio
    async def test_full_terminal_vocabulary_is_excluded(self, sqlite_sessionmaker: Any) -> None:
        # Every TERMINAL_STATUSES member must be excluded by the guard — drive
        # the real primitive against each status and require rowcount 0.
        for status in sorted(TERMINAL_STATUSES):
            run_id = uuid.uuid4()
            org_id = uuid.uuid4()
            await _seed_run(sqlite_sessionmaker, run_id, status=status, organisation_id=org_id)
            with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
                rowcount = await saq_hooks._mark_run_failed(
                    run_id.hex,
                    org_id.hex,
                    error_code="dual_write_failed",
                )
            assert rowcount == 0
            row = await _read_run(sqlite_sessionmaker, run_id)
            assert row["status"] == status

    @pytest.mark.asyncio
    async def test_invalid_error_code_marker_is_rejected(self, sqlite_sessionmaker: Any) -> None:
        with pytest.raises(ValueError, match="invalid error_code marker"):
            await saq_hooks._mark_run_failed(
                uuid.uuid4().hex,
                _ORG.hex,
                error_code="bad'; DROP TABLE runs; --",
            )

    @pytest.mark.asyncio
    async def test_lock_timeout_is_skipped_on_sqlite(self, sqlite_sessionmaker: Any) -> None:
        # The SET LOCAL is Postgres-only; on SQLite it must be skipped without
        # touching the mock session's execute count.
        session = AsyncMock()
        result = AsyncMock()
        result.rowcount = 0
        session.execute.return_value = result
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        with (
            patch.object(saq_hooks, "_open_factory", return_value=MagicMock(return_value=session)),
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
        ):
            rowcount = await saq_hooks._mark_run_failed(
                uuid.uuid4().hex,
                _ORG.hex,
                error_code="dual_write_failed",
                lock_timeout_ms=2000,
            )
        assert rowcount == 0
        assert session.execute.await_count == 1


# ---------------------------------------------------------------------------
# The dual-write chokepoint helper (db side) — SQLite, no RLS-org sessions
# ---------------------------------------------------------------------------


class TestDualWriteHelperSkipsWithoutRlsOrg:
    @pytest.mark.asyncio
    async def test_no_rls_org_raises_orchestratable_dual_write_error(self, sqlite_sessionmaker: Any) -> None:
        """B1 contract cut + qa iteration 1 Major 1: a session with NO bound
        RLS org raises :class:`DualWriteError` (origin='rls_precheck') — NOT a
        raw :class:`OutputsRlsMismatch` — so the chokepoint's catch/rollback/
        orchestrate contract covers the org-context failure shape exactly like
        an in-savepoint store failure (an un-orchestrated escape would skip
        terminalize/event)."""
        from modulo.db.crud.run import write_run_outputs_from_run
        from modulo.db.rls import OutputsRlsMismatch

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        async with maker() as session, session.begin():
            with pytest.raises(DualWriteError, match="requires a bound RLS organisation context") as err:
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert err.value.origin == "rls_precheck"
        assert not isinstance(err.value, OutputsRlsMismatch)
        assert err.value.run_id == run_id
        assert err.value.claim_token is None
        assert err.value.sqlstate is None
        row = await _read_run(maker, run_id)
        assert row["status"] == "running"

    @pytest.mark.asyncio
    async def test_mismatched_org_raises_dual_write_error_with_both_orgs(self, sqlite_sessionmaker: Any) -> None:
        """qa iteration 1 Major 1: a MISMATCHED RLS org context also raises
        :class:`DualWriteError` (origin='rls_precheck') carrying BOTH orgs in
        the message (never silently skip a cross-tenant anomaly)."""
        from modulo.db.crud.run import write_run_outputs_from_run

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        other_org = uuid.uuid4()
        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with pytest.raises(DualWriteError) as err:
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=other_org,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        message = str(err.value)
        assert str(_ORG) in message
        assert str(other_org) in message
        assert err.value.origin == "rls_precheck"

    @pytest.mark.asyncio
    async def test_org_less_failure_flows_through_guard_and_orchestrator(self, sqlite_sessionmaker: Any) -> None:
        """The FULL orchestration contract on the rls_precheck shape: the
        org-less DualWriteError is caught by ``guard_dual_write`` (rollback
        first), terminalized ``dual_write_failed`` via the separate-session
        ``_mark_run_failed``, and the failure event is emitted — no
        un-orchestrated escape."""
        from modulo.db.crud.run import write_run_outputs_from_run

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        # The orchestrate terminalize leg binds str(run_id) AND str(org) (DASHED);
        # the raw text seeds store ids verbatim, so seeds here use the dashed
        # forms and read them back dashed (a Postgres Uuid bind normalises
        # both — test-harness-only distinction).
        await _seed_run(maker, run_id, run_id_text=str(run_id), org_id_text=str(_ORG))

        async def _write_through_guard() -> None:
            async with maker() as session, session.begin(), guard_dual_write(session):
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )

        with (
            patch.object(saq_hooks, "_open_factory", return_value=maker),
            patch(
                "modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event",
                new_callable=AsyncMock,
            ) as emit,
            pytest.raises(DualWriteError) as err,
        ):
            await _write_through_guard()
        assert err.value.origin == "rls_precheck"
        emit.assert_awaited_once()
        row = await _read_run(maker, run_id, run_id_text=str(run_id))
        assert row["status"] == "failed"
        assert row["error_code"] == "dual_write_failed"


async def set_rls_org_for_test(session: AsyncSession, org_id: uuid.UUID) -> None:
    """SQLite-side RLS binding (session.info), bypassing the transaction guard."""
    from modulo.db.rls import _TENANT_KEY

    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[_TENANT_KEY] = org_id


class TestDualWriteHelperSentinelAbort:
    @pytest.mark.asyncio
    async def test_sentinel_squatting_payload_fails_closed(self, sqlite_sessionmaker: Any) -> None:
        """A ``__``-prefixed node id in the payload aborts the dual-write leg.

        The repo's sentinel gate raises inside the savepoint; the helper
        converts it to the fail-closed DualWriteError (non-retryable).
        """
        from modulo.db.crud.run import write_run_outputs_from_run

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with pytest.raises(DualWriteError) as caught:
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"__run_meta__": {"squat": True}},
                    telemetry=None,
                    origin="unit-test",
                )
        assert caught.value.origin == "unit-test"
        assert caught.value.sqlstate is None


class TestDualWriteHelperRetryable:
    @pytest.mark.asyncio
    async def test_statement_timeout_57014_retries_once_then_succeeds(self, sqlite_sessionmaker: Any) -> None:
        """A 57014 (statement timeout) failure once → the retry leg runs the
        REAL write. ONLY 57014 stays retryable — the transaction-aborting
        states abort the whole transaction on Postgres, so re-entering a
        savepoint after them would fail with 25P02 (qa iteration-1)."""
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import write_run_outputs_from_run

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        real_replace = run_crud.replace_run_node_outputs
        calls: list[int] = []

        async def _flaky_replace(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                err = OperationalError("stmt", {}, Exception("statement timeout"))
                err.orig = type("_FakePG", (Exception,), {"sqlstate": "57014"})("statement timeout")
                raise err
            return await real_replace(*args, **kwargs)

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with patch.object(run_crud, "replace_run_node_outputs", _flaky_replace):
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_serialization_failure_40001_raises_immediately(self, sqlite_sessionmaker: Any) -> None:
        """40001 aborts the WHOLE Postgres transaction — a savepoint retry
        after it would fail with 25P02 (doomed round-trip), so the helper goes
        STRAIGHT to DualWriteError (ONE replace attempt, no retry leg)."""
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import write_run_outputs_from_run

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        calls: list[int] = []

        async def _failing(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            err = OperationalError("stmt", {}, Exception("could not serialize"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "40001"})("could not serialize")
            raise err

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _failing),
                pytest.raises(DualWriteError) as caught,
            ):
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert calls == [1], "40001 must NOT be retried in-session"
        assert caught.value.sqlstate == "40001"

    @pytest.mark.asyncio
    async def test_retryable_sqlstate_twice_raises_dual_write_error(self, sqlite_sessionmaker: Any) -> None:
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import write_run_outputs_from_run

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        async def _always_failing(*args: Any, **kwargs: Any) -> None:
            err = OperationalError("stmt", {}, Exception("statement timeout"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "57014"})("statement timeout")
            raise err

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _always_failing),
                pytest.raises(DualWriteError) as caught,
            ):
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert caught.value.sqlstate == "57014"

    @pytest.mark.asyncio
    async def test_deadlock_40p01_raises_immediately(self, sqlite_sessionmaker: Any) -> None:
        """40P01 (deadlock) aborts the transaction → straight to
        DualWriteError, no doomed savepoint retry."""
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import write_run_outputs_from_run

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        calls: list[int] = []

        async def _always_failing(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            err = OperationalError("stmt", {}, Exception("deadlock"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "40P01"})("deadlock")
            raise err

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _always_failing),
                pytest.raises(DualWriteError) as caught,
            ):
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert calls == [1]
        assert caught.value.sqlstate == "40P01"


# ---------------------------------------------------------------------------
# Redis races — the failure-event window leg
# ---------------------------------------------------------------------------


class TestRedisHelpers:
    """qa M12/M13 + riders (a): bounded Redis, TTL-at-creation, loud failures."""

    def test_socket_timeout_is_bounded(self) -> None:
        """qa M13: _open_redis bounds BOTH socket timeouts — a hung Redis
        (accept but never answer) must not stall the abort path forever."""
        import redis.asyncio as redis_module

        from modulo.core.run_outputs_dualwrite import _open_redis

        created: dict[str, Any] = {}

        def _from_url(url: str, **kwargs: Any) -> Any:
            created.update(kwargs)
            return MagicMock()

        with (
            patch.object(redis_module, "Redis") as redis_cls,
            patch("modulo.settings.get_settings", return_value=MagicMock(redis_url="redis://localhost:6379/0")),
        ):
            redis_cls.from_url = _from_url
            _open_redis()
        assert created["socket_connect_timeout"] == 2
        assert created["socket_timeout"] == 2

    @pytest.mark.asyncio
    async def test_incr_window_stamps_ttl_at_creation(self) -> None:
        """qa M12: the window TTL is stamped by a SET NX EX BEFORE the INCR —
        a process death between the two leaves a key that still expires, so
        the dual_write_failed event channel can never be suppressed forever."""
        client = _FakeRedis()
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            count = await _redis_incr_window("saq:run_outputs:dual_write_failed_window:o1", 60)
        assert count == 1
        assert client.set_calls[0] == {
            "key": "saq:run_outputs:dual_write_failed_window:o1",
            "value": 0,
            "ex": 60,
            "nx": True,
        }
        assert client.set_calls[1] == {"key": "saq:run_outputs:dual_write_failed_window:o1", "value": "__incrby__1"}
        # Fixed window: the post-increment heal is NX (restores a no-TTL key
        # WITHOUT deferring the window).
        assert client.expire_calls[-1] == {
            "key": "saq:run_outputs:dual_write_failed_window:o1",
            "ttl": 60,
            "nx": True,
        }

    @pytest.mark.asyncio
    async def test_expiry_in_gap_heals_the_no_ttl_key(self) -> None:
        """qa iteration 2 (Major 6, second form): if the key expires between
        the SET NX EX and the INCRBY, INCRBY recreates it with NO TTL — the
        post-increment EXPIRE NX restores one so the window can never
        suppress events forever. Simulated: the SET lands, the key
        'expires', INCRBY recreates bare, and the EXPIRE still fires."""
        client = _FakeRedis()

        async def _incr_recreates_bare(key: Any, amount: int) -> int:
            # The key expired in the gap: INCRBY recreates it with NO TTL.
            return amount

        client.incrby = _incr_recreates_bare  # type: ignore[method-assign]
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            count = await _redis_incr_window("k", 60)
        assert count == 1
        assert client.expire_calls == [{"key": "k", "ttl": 60, "nx": True}], (
            "the NX expiry-in-gap heal must run after every increment"
        )

    @pytest.mark.asyncio
    async def test_incr_window_failure_between_set_and_incr_still_expires(self) -> None:
        """Injected failure AFTER the SET NX EX but before/during the INCRBY:
        the TTL is already stamped (the SET landed) — the key still expires."""
        client = _FakeRedis()

        async def _boom(key: Any, amount: int) -> int:
            raise RuntimeError("process dies here")

        client.incrby = _boom  # type: ignore[method-assign]
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            assert await _redis_incr_window("k", 60) is None
        assert client.set_calls[0]["ex"] == 60, "the TTL survived the injected failure"

    @pytest.mark.asyncio
    async def test_incr_window_failure_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("WARNING", logger="modulo.core.run_outputs_dualwrite")
        client = _FakeRedis()

        async def _boom(key: Any, amount: int) -> int:
            raise RuntimeError("redis down")

        client.incrby = _boom  # type: ignore[method-assign]
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            assert await _redis_incr_window("k", 60) is None
        assert any("redis_incr_window_failed" in r.message for r in caplog.records)


class TestFailureEventDetailHygiene:
    """qa rider (d): the dual_write_failed event's error_detail is sanitized
    AND truncated — blob content must never land verbatim in error_events."""

    @pytest.mark.asyncio
    async def test_detail_is_sanitized_and_truncated(self) -> None:
        from modulo.core.run_outputs_dualwrite import _emit_dual_write_failed_event

        dirty = "x" * 3000 + "\x00" + "password=hunter2 secret-key=abc"
        exc = _dual_write_error(sqlstate="42501", origin="update_run_status.orm")
        captured: dict[str, Any] = {}

        async def _emit(org_id: Any, *, level: str, message: str, context_json: dict[str, Any]) -> None:
            captured["context_json"] = context_json

        with (
            patch("modulo.core.run_outputs_dualwrite._redis_incr_window", new_callable=AsyncMock, return_value=None),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", _emit),
        ):
            await _emit_dual_write_failed_event(exc, rowcount=1, error_detail=dirty)
        detail = captured["context_json"]["error_detail"]
        assert detail is not None
        assert len(detail) <= 2001, "truncated before embedding"
        assert "\x00" not in detail
        assert "hunter2" not in detail, "secret patterns are redacted"
        assert detail.endswith("…")

    @pytest.mark.asyncio
    async def test_clean_short_detail_passes_verbatim(self) -> None:
        from modulo.core.run_outputs_dualwrite import _emit_dual_write_failed_event

        exc = _dual_write_error()
        captured: dict[str, Any] = {}

        async def _emit(org_id: Any, *, level: str, message: str, context_json: dict[str, Any]) -> None:
            captured["context_json"] = context_json

        with (
            patch("modulo.core.run_outputs_dualwrite._redis_incr_window", new_callable=AsyncMock, return_value=None),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", _emit),
        ):
            await _emit_dual_write_failed_event(exc, rowcount=0, error_detail="permission denied for table runs")
        assert captured["context_json"]["error_detail"] == "permission denied for table runs"


class TestOrgLessRaiseNotSkip:
    """qa rider (g), retired by the B1 contract cut: the org-less store write
    raises fail-closed instead of skipping, so there is no skip to count.
    Since qa iteration 1 Major 1 the raise is a DualWriteError
    (origin='rls_precheck', see TestDualWriteHelperSkipsWithoutRlsOrg)."""

    @pytest.mark.asyncio
    async def test_no_rls_org_raises_instead_of_skipping(self, sqlite_sessionmaker: Any) -> None:
        from modulo.db.crud.run import write_run_outputs_from_run

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        async with maker() as session, session.begin():
            with pytest.raises(DualWriteError):
                await write_run_outputs_from_run(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )


class TestSqlstateExtraction:
    """qa iteration 2 (Major 2): the shared :mod:`modulo.db.sqlstates`
    extractor — PG fidelity for the savepoint failure chain.

    On Postgres, a transaction-aborting failure INSIDE a savepoint makes the
    savepoint's ``__aexit__`` raise the ROLLBACK-TO-SAVEPOINT failure (25P02)
    with the ORIGINAL driver error as ``__context__``. An extraction that
    only walks ``.orig``/``__cause__`` catches 25P02: the node-runner's
    transaction-aborting classification never fires on the exact failure it
    exists for, and DualWriteError.sqlstate is misattributed."""

    def test_savepoint_rollback_wrapper_does_not_mask_the_original(self) -> None:
        """25P02 (outer, raised by the savepoint __aexit__) with
        ``__context__`` = 40P01 (the original deadlock) → 40P01 wins."""
        from modulo.db.sqlstates import sqlstate_of

        original = OperationalError("stmt", {}, Exception("deadlock detected"))
        original.orig = type("_FakePG", (Exception,), {"sqlstate": "40P01"})("deadlock detected")

        class _RollbackWrapperError(OperationalError):
            """The 25P02 wrapper SQLAlchemy raises from the savepoint exit."""

        wrapper = _RollbackWrapperError("stmt", {}, Exception("current transaction is aborted"))
        wrapper.orig = type("_FakePG", (Exception,), {"sqlstate": "25P02"})("current transaction is aborted")
        wrapper.__context__ = original

        assert sqlstate_of(wrapper) == "40P01"

    def test_cause_chain_still_walks(self) -> None:
        from modulo.db.sqlstates import sqlstate_of

        original = OperationalError("stmt", {}, Exception("statement timeout"))
        original.orig = type("_FakePG", (Exception,), {"sqlstate": "57014"})("statement timeout")
        raised = RuntimeError("wrapped")
        raised.__cause__ = original
        assert sqlstate_of(raised) == "57014"

    def test_plain_25p02_with_no_other_state_is_the_fallback(self) -> None:
        """25P02 is skipped in favour of any other state in the chain; when
        the whole chain carries nothing else it is the honest answer."""
        from modulo.db.sqlstates import sqlstate_of

        wrapper = OperationalError("stmt", {}, Exception("aborted"))
        wrapper.orig = type("_FakePG", (Exception,), {"sqlstate": "25P02"})("aborted")
        assert sqlstate_of(wrapper) == "25P02"

    def test_no_sqlstate_anywhere_returns_none(self) -> None:
        from modulo.db.sqlstates import sqlstate_of

        assert sqlstate_of(RuntimeError("no sqlstate here")) is None

    def test_cycle_safe_and_bounded(self) -> None:
        from modulo.db.sqlstates import sqlstate_of

        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__context__ = b
        b.__context__ = a  # cycle
        assert sqlstate_of(a) is None

    def test_vocabularies_are_shared_not_forked(self) -> None:
        """The crud + node-runner vocabularies ARE the shared module's.

        B1 removed node_runner's module-local ``_MARKER_TXN_ABORTING_SQLSTATES``
        alias: the read sites consume the imported shared constant directly
        (the B1 SQLSTATE consolidation), and crud.run keeps only its
        delegation alias."""
        import inspect

        from modulo.core.pipeline_engine import node_runner
        from modulo.db.crud.run import _DUAL_WRITE_RETRYABLE_SQLSTATES
        from modulo.db.sqlstates import DUAL_WRITE_RETRYABLE_SQLSTATES

        source = inspect.getsource(node_runner)
        assert "MARKER_TXN_ABORTING_SQLSTATES" in source
        assert "_MARKER_TXN_ABORTING_SQLSTATES" not in source, "the module-local alias must stay removed (B1)"
        assert _DUAL_WRITE_RETRYABLE_SQLSTATES is DUAL_WRITE_RETRYABLE_SQLSTATES

    def test_marker_abort_vocabulary_matches_the_shared_copy(self) -> None:
        from modulo.db.sqlstates import MARKER_TXN_ABORTING_SQLSTATES

        assert {
            "40P01",
            "57P01",
            "57P02",
            "08000",
            "08001",
            "08003",
            "08004",
            "08006",
            "08007",
        } == MARKER_TXN_ABORTING_SQLSTATES

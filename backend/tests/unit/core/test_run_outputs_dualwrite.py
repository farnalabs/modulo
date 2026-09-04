"""Unit tests for the FAR-583 run-outputs dual-write orchestration.

Covers the core-side surface (:mod:`modulo.core.run_outputs_dualwrite`) and the
extended ``saq_hooks._mark_run_failed`` primitive:

* the kill-switch read — per-call semantics (a flip between calls changes the
  behaviour) and the fail-closed default when the store read fails;
* ``orchestrate_dual_write_failure`` — the separate-session terminalize with
  ``error_code='dual_write_failed'`` + bounded lock_timeout, the best-effort
  error event, and the best-effort Redis counters;
* ``guard_dual_write`` — rollback-BEFORE-orchestrate ordering, then re-raise;
* ``_mark_run_failed``'s guard extension — the FULL terminal vocabulary plus
  the explicit ``'unknown'`` exclusion (an unknown run is never transitioned),
  driven against a real SQLite engine so the raw SQL actually executes.

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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.error_tracking import saq_hooks
from modulo.core.run_outputs_dualwrite import (
    DUAL_WRITE_ENABLED_KEY,
    guard_dual_write,
    is_dual_write_enabled,
    orchestrate_dual_write_failure,
)
from modulo.core.runtime_config.store import get_runtime_config_store
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


class TestKillSwitch:
    def setup_method(self) -> None:
        get_runtime_config_store().clear_all_overrides()

    def teardown_method(self) -> None:
        get_runtime_config_store().clear_all_overrides()

    def test_default_is_on(self) -> None:
        assert is_dual_write_enabled() is True

    def test_false_disables(self) -> None:
        get_runtime_config_store().set_override(DUAL_WRITE_ENABLED_KEY, "false")
        assert is_dual_write_enabled() is False

    def test_value_is_read_per_call_not_cached_at_import(self) -> None:
        assert is_dual_write_enabled() is True
        # Flip BETWEEN calls — the next read must observe the new value.
        get_runtime_config_store().set_override(DUAL_WRITE_ENABLED_KEY, "false")
        assert is_dual_write_enabled() is False
        get_runtime_config_store().clear_override(DUAL_WRITE_ENABLED_KEY)
        assert is_dual_write_enabled() is True

    def test_case_insensitive_false_only_disables(self) -> None:
        get_runtime_config_store().set_override(DUAL_WRITE_ENABLED_KEY, " False ")
        assert is_dual_write_enabled() is False
        get_runtime_config_store().set_override(DUAL_WRITE_ENABLED_KEY, "off")
        # Fail-closed: anything but the literal "false" keeps dual-write ON.
        assert is_dual_write_enabled() is True

    def test_store_read_failure_is_fail_closed_on(self) -> None:
        store = MagicMock()
        store.get.side_effect = RuntimeError("store unavailable")
        with patch("modulo.core.runtime_config.store.get_runtime_config_store", return_value=store):
            assert is_dual_write_enabled() is True


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
            patch("modulo.core.run_outputs_dualwrite.bump_dispatcher_reconcile_counter", new_callable=AsyncMock),
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
            patch("modulo.core.run_outputs_dualwrite.bump_dispatcher_reconcile_counter", new_callable=AsyncMock),
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
            patch("modulo.core.run_outputs_dualwrite.bump_dispatcher_reconcile_counter", new_callable=AsyncMock),
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 1

    @pytest.mark.asyncio
    async def test_terminalize_failure_is_swallowed_and_counters_still_fire(self) -> None:
        exc = _dual_write_error()
        with (
            patch.object(
                saq_hooks,
                "_mark_run_failed",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock),
            patch(
                "modulo.core.run_outputs_dualwrite.bump_dispatcher_reconcile_counter",
                new_callable=AsyncMock,
            ) as bump,
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 0
        fields = {call.args[0] for call in bump.await_args_list}
        assert fields == {"outputs_dual_write_failed"}

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
) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, 'mark-run-failed org', :slug, '{}', '{}')"
            ),
            {"id": str(organisation_id), "slug": f"mark-run-failed-{organisation_id.hex[:12]}"},
        )
        await session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id, claim_token, cancellation_requested) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :status, 1, 'ih', :thread, :tok, 0)"
            ),
            {
                "id": run_id.hex,
                "oid": organisation_id.hex,
                "pid": _PIPELINE.hex,
                "sid": _SNAPSHOT.hex,
                "status": status,
                "thread": f"mark-run-failed-{run_id}",
                "tok": claim_token,
            },
        )


async def _read_run(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> dict[str, Any]:
    async with maker() as session, session.begin():
        row = (
            await session.execute(
                text("SELECT status, error_code, claim_token FROM runs WHERE id = :rid"),
                {"rid": run_id.hex},
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
    async def test_no_rls_org_skips_the_new_table_leg(self, sqlite_sessionmaker: Any) -> None:
        """A session with no bound RLS org skips the new-table leg silently.

        The repo's write gate requires a bound org; the only org-less sessions
        are unit tests / maintenance sessions where the legacy write governs.
        The absence of a raised error IS the assertion (a real replace attempt
        would either raise the org gate or hit the table).
        """
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        async with maker() as session, session.begin():
            await dual_write_run_node_outputs(
                session,
                run_id=run_id,
                organisation_id=_ORG,
                outputs={"n1": {"a": 1}},
                telemetry=None,
            )
        row = await _read_run(maker, run_id)
        assert row["status"] == "running"


class TestDualWriteHelperKillSwitchOff:
    @pytest.mark.asyncio
    async def test_kill_switch_off_skips_with_degraded_note(self, sqlite_sessionmaker: Any) -> None:
        from modulo.core.run_outputs_dualwrite import note_dual_write_disabled
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        store = get_runtime_config_store()
        store.set_override(DUAL_WRITE_ENABLED_KEY, "false")
        try:
            async with maker() as session, session.begin():
                await set_rls_org_for_test(session, _ORG)
                with patch(
                    "modulo.core.run_outputs_dualwrite.note_dual_write_disabled",
                    wraps=note_dual_write_disabled,
                ) as note:
                    await dual_write_run_node_outputs(
                        session,
                        run_id=run_id,
                        organisation_id=_ORG,
                        outputs={"n1": {"a": 1}},
                        telemetry=None,
                    )
            assert note.await_count == 1
        finally:
            store.clear_override(DUAL_WRITE_ENABLED_KEY)


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
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with pytest.raises(DualWriteError) as caught:
                await dual_write_run_node_outputs(
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
    async def test_retryable_sqlstate_retries_once_then_succeeds(self, sqlite_sessionmaker: Any) -> None:
        """A 40001-class failure once → the retry leg runs the REAL write."""
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        real_replace = run_crud.replace_run_node_outputs
        calls: list[int] = []

        async def _flaky_replace(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            if len(calls) == 1:
                err = OperationalError("stmt", {}, Exception("serialization"))
                err.orig = type("_FakePG", (Exception,), {"sqlstate": "40001"})("serialization")
                raise err
            await real_replace(*args, **kwargs)

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _flaky_replace),
                patch(
                    "modulo.core.run_outputs_dualwrite.bump_dispatcher_reconcile_counter",
                    new_callable=AsyncMock,
                ),
            ):
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_retryable_sqlstate_twice_raises_dual_write_error(self, sqlite_sessionmaker: Any) -> None:
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        async def _always_failing(*args: Any, **kwargs: Any) -> None:
            err = OperationalError("stmt", {}, Exception("deadlock"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "40P01"})("deadlock")
            raise err

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _always_failing),
                patch(
                    "modulo.core.run_outputs_dualwrite.bump_dispatcher_reconcile_counter",
                    new_callable=AsyncMock,
                ),
                pytest.raises(DualWriteError) as caught,
            ):
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert caught.value.sqlstate == "40P01"

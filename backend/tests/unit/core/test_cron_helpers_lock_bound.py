"""FAR-1601 — bounded row-lock waits for the cron_helpers periodic sweeps.

Every hot-``runs`` write the dispatcher-reconcile sweep makes lives in ONE
transaction: the per-org pass in ``_reconcile_org``. That transaction now
issues the transaction-scoped ``lock_timeout`` bound
(``db.crud.row_lock.set_mutation_row_lock_timeout``, value
``Settings.mutation_row_lock_timeout_ms``) as its FIRST statement — before the
nodeless router, the four raw-SQL batch terminalizers (``_MID_GRAPH_WEDGE_SQL``,
claim-cap, expired-HITL, missing-HITL), the enqueue-failed TTL backstop, the
previous-attempt marker clear and the capacity-defer stamp.

Until FAR-1601 those waits were UNBOUNDED: a live writer (executor
claim/heartbeat/gate decision) holding a needed row could stall the sweep
silently past the Fly HAProxy 30-minute session window — the FAR-1524 class
that got prod connections culled mid-operation, already bounded on the dispatch
path (FAR-1584) and the run_admission sweeps (FAR-1592).

These tests pin, with fail-first evidence:

1. the bound is the org transaction's FIRST statement, precedes EVERY
   ``UPDATE runs`` in it (terminalizers AND the per-row writers), is
   transaction-local (``set_config(..., is_local => true)``) and takes its
   value from the operator knob;
2. a 55P03 raised in the READ phase propagates out of ``_reconcile_org``
   instead of being mislabelled a ``read failed``;
3. the reconcile BODY skips that ONE org with a ``WARNING`` + full chain,
   unwinds its counts and compensating-fact ids, and keeps the tick going —
   never a silent no-op, never a lost recovery;
4. the catch is scoped to 55P03 ONLY (any other failure keeps its own
   contract) and ``CancelledError`` is never treated as a lock timeout.

The real-Postgres contention behaviour (a held row lock actually timing out) is
an integration concern — this unit seam drives the same 55P03 the bound
produces.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncpg import exceptions as asyncpg_exceptions
from sqlalchemy.exc import OperationalError

from modulo.core import cron_helpers as ch
from modulo.db.sqlstates import sqlstate_of

ORG = uuid.uuid4()
RUN_ID = uuid.uuid4()
LOCK_TIMEOUT_MS = 4321


def _lock_timeout_error(statement: str) -> OperationalError:
    """Simulated row-lock contention: asyncpg's real ``LockNotAvailableError``
    (SQLSTATE 55P03) wrapped exactly the way SQLAlchemy surfaces it — the seam
    ``tests/unit/test_dispatch.py`` drives for FAR-1584's writers."""
    driver_error = asyncpg_exceptions.LockNotAvailableError("canceling statement due to lock timeout")
    return OperationalError(statement, {}, driver_error)


def _non_lock_timeout_error(statement: str) -> OperationalError:
    """A different SQLSTATE (serialization failure) — NOT a lock timeout."""
    return OperationalError(statement, {}, SimpleNamespace(sqlstate="40001"))


def _row_lock_settings() -> MagicMock:
    """Settings double carrying the bound's value knob only."""
    return MagicMock(mutation_row_lock_timeout_ms=LOCK_TIMEOUT_MS)


def _tuning() -> ch.ReconcileTuning:
    return ch.ReconcileTuning(
        nodeless_window=20,
        max_age_minutes=60,
        claim_cap=3,
        stale_window=600,
        capacity_redispatch_seconds=120,
        hitl_review_cancel_grace_seconds=3600,
    )


class _Begin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        # Propagate: an exception raised inside the org transaction must roll
        # it back (and reach the caller's 55P03 handler), exactly like the
        # real ``AsyncSession.begin()``.
        return False


class _OrgSession:
    """AsyncSession double reporting the postgresql dialect, recording SQL.

    Takes the LIVE branch of ``set_mutation_row_lock_timeout``'s dialect gate
    (``get_bind().dialect.name == "postgresql"``) and records EVERY statement —
    including the bound and ``_set_rls_org``'s GUCs — so order assertions can
    pin the bound as the transaction's first statement.

    Scripting knobs:
      * ``rows`` / ``rows_marker`` — return *rows* from the reconcile row
        select (identified by the selected ``retry_policy`` column) so the
        per-row loop runs; every other ``.all()`` is empty.
      * ``raise_on_update`` — raise the given error on the first
        ``UPDATE runs`` (i.e. out of a terminalizer).
      * the pipeline select and its active-run count answer
        ``_capacity_defer_pending_run``'s two reads (a saturated pipeline).
    """

    def __init__(
        self,
        *,
        rows: list[Any] | None = None,
        rows_marker: str = "retry_policy",
        raise_on_update: OperationalError | None = None,
    ) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any] | None] = []
        self.rows = rows or []
        self.rows_marker = rows_marker
        self.raise_on_update = raise_on_update
        self.info: dict[str, Any] = {}
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._bind = bind

    def get_bind(self) -> MagicMock:
        return self._bind

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = str(stmt)
        self.statements.append(sql)
        self.params.append(params)
        if self.raise_on_update is not None and "UPDATE runs" in sql:
            raise self.raise_on_update
        result = MagicMock()
        if self.rows_marker in sql:
            result.all.return_value = list(self.rows)
        else:
            result.all.return_value = []
        if "count(*)" in sql and "FROM runs" in sql:
            # _capacity_defer_pending_run's active-run count: at capacity.
            result.scalar_one_or_none.return_value = 1
        if "FROM pipelines" in sql:
            # _capacity_defer_pending_run's pipeline read: max_concurrent_runs=1.
            result.scalar_one_or_none.return_value = SimpleNamespace(max_concurrent_runs=1)
        return result

    async def flush(self) -> None:
        return None

    def executed(self, marker: str) -> list[int]:
        """Indices of every recorded statement containing *marker*."""
        return [i for i, sql in enumerate(self.statements) if marker in sql]

    def bound_indices(self) -> list[int]:
        return self.executed("set_config('lock_timeout'")


async def _run_org_pass(
    session: _OrgSession,
    *,
    summary: dict[str, Any] | None = None,
    terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]] | None = None,
    row_budget: int | None = None,
) -> int:
    """Drive the REAL ``_reconcile_org`` against *session*."""
    return await ch._reconcile_org(
        factory=MagicMock(return_value=session),
        q=MagicMock(),
        redis_client=AsyncMock(),
        org_id=ORG,
        re_dispatch_predicate=ch.text("1 = 1"),
        tuning=_tuning(),
        enqueue_failed_redispatched=0,
        summary=summary if summary is not None else ch._dispatcher_summary(),
        terminalized_run_ids=terminalized_run_ids if terminalized_run_ids is not None else [],
        row_budget=row_budget,
    )


# ---------------------------------------------------------------------------
# 1. The bound: first statement of the org transaction, before every lock
# ---------------------------------------------------------------------------


class TestOrgTransactionLockBound:
    """FAR-1601: the bound precedes EVERY ``runs`` write the org transaction
    makes — the four raw-SQL terminalizers AND the three per-row writers."""

    async def test_bound_is_the_transactions_first_statement_and_comes_from_the_setting(self) -> None:
        """THE pin: the bound is the transaction's FIRST statement (before any
        lock, before even the RLS GUCs), is transaction-local, precedes the
        terminalizers' ``UPDATE runs``, and its value is the operator knob."""
        session = _OrgSession()
        with patch("modulo.db.crud.row_lock.get_settings", return_value=_row_lock_settings()):
            await _run_org_pass(session)

        sqls = session.statements
        assert sqls, "the org transaction executed no statements"
        bound_at = session.bound_indices()
        assert bound_at, f"no transaction-local lock bound issued; statements={sqls}"
        assert bound_at[0] == 0, f"the bound must be the transaction's FIRST statement; statements={sqls}"
        # SET LOCAL semantics: set_config(..., is_local => true) — reverts on
        # COMMIT/ROLLBACK, never leaks onto a pooled connection.
        assert ", true)" in sqls[bound_at[0]]
        assert session.params[bound_at[0]] == {"val": f"{LOCK_TIMEOUT_MS}ms"}
        updates = session.executed("UPDATE runs")
        assert updates, f"the batch terminalizers never ran an UPDATE runs; statements={sqls}"
        assert bound_at[0] < updates[0], (
            f"the bound must precede the row lock (lock_timeout at {bound_at[0]}, UPDATE at {updates[0]})"
        )

    async def test_bound_precedes_the_per_row_writers_in_the_same_transaction(self) -> None:
        """The three PER-ROW writers — ``_fail_run_dispatch_failed`` (TTL
        backstop), ``_clear_previous_attempt_marker`` and
        ``_capacity_defer_pending_run`` — run inside the SAME org transaction,
        so the one bound issued at its head covers them too: every one of
        their ``UPDATE runs`` statements lands after it."""
        row = SimpleNamespace(id=RUN_ID, pipeline_id=uuid.uuid4(), status="pending")
        marker_row = SimpleNamespace(id=RUN_ID, sandbox_dispatch_state="dispatch:abc")
        session = _OrgSession(rows=[row])
        summary = ch._dispatcher_summary()

        async def _row_loop_writers(
            session: Any,
            q: Any,
            redis_client: Any,
            org_id: uuid.UUID,
            row: Any,
            _nodeless_window: int,
            counter: int,
            summary: dict[str, Any],
            terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
            **_kwargs: Any,
        ) -> int:
            await ch._fail_run_dispatch_failed(session, row.id, org_id)
            await ch._clear_previous_attempt_marker(session, org_id, marker_row)
            await ch._capacity_defer_pending_run(session, row, summary)
            return counter

        with (
            patch.object(ch, "_reconcile_one_row", new_callable=AsyncMock, side_effect=_row_loop_writers),
            patch("modulo.db.crud.row_lock.get_settings", return_value=_row_lock_settings()),
        ):
            await _run_org_pass(session, summary=summary)

        bound_at = session.bound_indices()
        assert bound_at, "no transaction-local lock bound issued"
        assert bound_at[0] == 0, "the bound must be the transaction's FIRST statement"
        for marker in (
            "AND enqueue_failed_at IS NOT NULL",  # _fail_run_dispatch_failed
            "sandbox_dispatch_state = NULL",  # _clear_previous_attempt_marker
            "error_code IS DISTINCT FROM",  # _capacity_defer_pending_run
        ):
            hits = session.executed(marker)
            assert hits, f"the row-loop writer {marker!r} never executed; statements={session.statements}"
            assert bound_at[0] < hits[0], f"the bound must precede {marker!r}"
        # The capacity writer took its stamping branch (active >= max).
        assert summary["capacity_deferred"] == 1


# ---------------------------------------------------------------------------
# 2. The 55P03 contract — read phase, then the reconcile body's org skip
# ---------------------------------------------------------------------------


class TestLockTimeoutContract:
    async def test_55p03_in_the_read_phase_propagates_to_the_org_skip(self, caplog: pytest.LogCaptureFixture) -> None:
        """A bounded expiry during the batch terminalizers must NOT be
        mislabelled ``read failed``: it propagates out of ``_reconcile_org``
        (rolling the org transaction back at the ``async with``) so the
        reconcile body can apply the org-level skip + WARNING contract."""
        session = _OrgSession(raise_on_update=_lock_timeout_error("UPDATE runs SET ..."))
        caplog.set_level(logging.WARNING, logger="modulo.core.cron_helpers")

        with (
            patch("modulo.db.crud.row_lock.get_settings", return_value=_row_lock_settings()),
            pytest.raises(OperationalError) as excinfo,
        ):
            await _run_org_pass(session)

        assert sqlstate_of(excinfo.value) == "55P03"
        # Handled by the BODY (below), not here — and never as a read failure.
        assert not any("read failed" in message for message in caplog.messages)
        assert not any("org_lock_timeout" in message for message in caplog.messages)

    async def test_non_lock_read_failure_keeps_the_read_failed_contract(self, caplog: pytest.LogCaptureFixture) -> None:
        """Scope guard on the read phase: a NON-lock failure keeps the
        pre-existing contract — logged ``read failed``, the org skipped, no
        exception escaping — and is never routed through the 55P03 skip."""
        session = _OrgSession(raise_on_update=_non_lock_timeout_error("UPDATE runs SET ..."))
        caplog.set_level(logging.ERROR, logger="modulo.core.cron_helpers")

        with patch("modulo.db.crud.row_lock.get_settings", return_value=_row_lock_settings()):
            got = await _run_org_pass(session)

        assert got == 0
        assert any("read failed" in message for message in caplog.messages)
        assert not any("org_lock_timeout" in message for message in caplog.messages)

    async def test_cancelled_error_is_never_treated_as_a_lock_timeout(self) -> None:
        """``CancelledError`` is re-raised FIRST — never swallowed as a lock
        timeout and never turned into a skip."""
        session = _OrgSession()
        with (
            patch.object(
                ch, "_terminalize_mid_graph_wedges", new_callable=AsyncMock, side_effect=asyncio.CancelledError()
            ),
            patch("modulo.db.crud.row_lock.get_settings", return_value=_row_lock_settings()),
            pytest.raises(asyncio.CancelledError),
        ):
            await _run_org_pass(session)


# ---------------------------------------------------------------------------
# 3. The reconcile BODY: skip the org, WARN, unwind, keep the tick going
# ---------------------------------------------------------------------------


async def _drive_body(
    org_ids: list[uuid.UUID],
    fake_reconcile_org: Any,
    *,
    summary: dict[str, Any],
    terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
    record_facts: Any,
) -> tuple[dict[str, Any], AsyncMock]:
    """Drive the REAL ``_dispatcher_reconcile_body`` over *org_ids* with a
    controlled ``_reconcile_org`` double (the FAR-1525 harness shape).

    Returns the tick summary plus the ``_run_reconcile_sweeps`` mock, so a
    test can assert the tick still reached its compensating sweeps.
    """
    settings = MagicMock(
        dispatcher_reconcile_budget_seconds=95,
        dispatcher_reconcile_org_budget_seconds=1,
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(ch, "_collect_org_ids", new_callable=AsyncMock, return_value=org_ids))
        stack.enter_context(patch.object(ch, "_reconcile_org", side_effect=fake_reconcile_org))
        stack.enter_context(patch.object(ch, "reconciler_recovery_predicate"))
        stack.enter_context(patch.object(ch, "_open_system_factory"))
        sweeps = stack.enter_context(patch.object(ch, "_run_reconcile_sweeps", new_callable=AsyncMock))
        stack.enter_context(patch.object(ch, "_record_fact_for_terminalized_run", record_facts))
        stack.enter_context(patch.object(ch, "_record_terminalisation_audits", new_callable=AsyncMock))
        stack.enter_context(patch("modulo.core.cron_helpers.AsyncRedis"))
        summary_out = await ch._dispatcher_reconcile_body(
            _settings=settings,
            factory=MagicMock(),
            queue_name="runs",
            reenqueue_window=5,
            tuning=_tuning(),
            terminalize_max=25,
            facts_max=25,
            max_rows=500,
            redis_client=MagicMock(),
            summary=summary,
            terminalized_run_ids=terminalized_run_ids,
        )
    return summary_out, sweeps


class TestOrgLockTimeoutSkip:
    async def test_lock_timeout_skips_the_org_with_a_warning_and_keeps_the_tick(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """THE 55P03 case: org 1's sweep meets a held ``runs`` row lock and the
        BOUND fires. Its transaction rolled back WHOLE — not one of its
        terminalizations or repairs committed — so its counts and
        compensating-fact ids are unwound, the org is skipped with a WARNING
        carrying the SQLSTATE, the org and the recovery, and the tick still
        processes org 2 and reaches the compensating sweeps. Never silent,
        never a lost recovery, and the TICK itself is not a failure."""
        timeout_org, good_org = uuid.uuid4(), uuid.uuid4()
        rolled_back_run = uuid.uuid4()
        summary = ch._dispatcher_summary()
        terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]] = []
        record_facts = AsyncMock()

        async def fake_reconcile_org(
            *,
            org_id: uuid.UUID,
            terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
            **_kwargs: Any,
        ) -> int:
            if org_id == timeout_org:
                # Work the org did before the bound fired — all of it rolls back.
                summary["scanned"] += 5
                summary["repaired"] += 3
                terminalized_run_ids.append((rolled_back_run, org_id))
                raise _lock_timeout_error("UPDATE runs SET ...")
            summary["scanned"] += 1
            return 0

        caplog.set_level(logging.WARNING, logger="modulo.core.cron_helpers")
        summary_out, sweeps = await _drive_body(
            [timeout_org, good_org],
            fake_reconcile_org,
            summary=summary,
            terminalized_run_ids=terminalized_run_ids,
            record_facts=record_facts,
        )

        # The tick COMPLETED: org 2 ran and the compensating sweeps were reached.
        assert summary_out["scanned"] == 1
        assert summary_out["repaired"] == 0
        assert sweeps.await_count == 1
        # The rolled-back org's ids are gone — no phantom compensating fact.
        fact_runs = [call.args[0] for call in record_facts.await_args_list]
        assert rolled_back_run not in fact_runs
        # A per-org skip is not a tick failure: the heartbeat stays truthful.
        assert summary_out["status"] == "ok"
        assert summary_out["last_error"] is None
        assert summary_out["org_timeouts"] == 0
        # The skip is visible in the health summary, not only the WARNING log.
        assert summary_out["org_lock_timeouts"] == 1
        # Never silent: WARNING with the SQLSTATE, the org, and the recovery.
        records = [record for record in caplog.records if "org_lock_timeout" in record.getMessage()]
        assert records, f"no org_lock_timeout WARNING emitted; log={caplog.text}"
        message = records[0].getMessage()
        assert "55P03" in message
        assert str(timeout_org) in message
        assert "next 60s tick" in message
        assert records[0].exc_info is not None

    async def test_non_lock_timeout_error_still_fails_the_tick(self, caplog: pytest.LogCaptureFixture) -> None:
        """The handler is targeted at 55P03 ONLY: any other failure keeps the
        tick's own failure contract — propagated to the outer failure
        heartbeat — and is never routed through the lock-timeout skip.

        Scope note: this guards the BOUNDARY of the new handler, so it passes
        both before the fix (nothing is caught at all) and after it (only
        55P03 is caught) — it is red exactly when the catch is too wide."""
        org = uuid.uuid4()
        summary = ch._dispatcher_summary()

        async def fake_reconcile_org(*, org_id: uuid.UUID, **_kwargs: Any) -> int:
            raise _non_lock_timeout_error("UPDATE runs SET ...")

        caplog.set_level(logging.WARNING, logger="modulo.core.cron_helpers")
        with pytest.raises(OperationalError):
            await _drive_body(
                [org],
                fake_reconcile_org,
                summary=summary,
                terminalized_run_ids=[],
                record_facts=AsyncMock(),
            )

        assert not any("org_lock_timeout" in record.getMessage() for record in caplog.records)

    async def test_cancelled_error_propagates_out_of_the_body_unchanged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The body re-raises ``CancelledError`` FIRST — before the 55P03
        handler can see it — so a cancellation is never swallowed as a lock
        timeout, never turned into an org skip, and never unwinds the tick's
        in-memory accounting as if the org had merely contended a row lock.

        Scope note: this is the body's OWN cancellation contract (the
        per-org time bound and the 55P03 skip both sit below it), and it is
        red exactly when the ``except Exception`` arm is allowed to swallow a
        cancellation."""
        org = uuid.uuid4()
        summary = ch._dispatcher_summary()

        async def fake_reconcile_org(*, org_id: uuid.UUID, **_kwargs: Any) -> int:
            raise asyncio.CancelledError

        caplog.set_level(logging.WARNING, logger="modulo.core.cron_helpers")
        with pytest.raises(asyncio.CancelledError):
            await _drive_body(
                [org],
                fake_reconcile_org,
                summary=summary,
                terminalized_run_ids=[],
                record_facts=AsyncMock(),
            )

        assert not any("org_lock_timeout" in record.getMessage() for record in caplog.records)

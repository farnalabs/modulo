"""Unit tests for modulo.db.crud.run_retention (FAR-427).

Mock/fake based — no Postgres. Focuses on the safety-critical purge cascade:

* only terminal-status runs are ever selected for deletion
* terminal runs are removed together with their checkpoint rows and SET NULL /
  RESTRICT run_id rows
* the purge is batched and idempotent
* the retention filter builder scopes by org / date / pipeline / status

The checkpoint tables are Postgres-only (JSONB / BYTEA, created by
ModuloPostgresSaver.setup()), so the byte estimates and the checkpoint deletes
are asserted at the orchestration level (which tables are targetted, with which
thread-ids) rather than against a live schema.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import SQLAlchemyError

from modulo.db.crud import run_retention as rr
from modulo.db.models.run import TERMINAL_STATUSES, Run

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _compile_with_literal_binds(conditions: list[object]) -> str:
    """Render the filter conditions as SQL with literal values baked in.

    Introspecting the string form of a SQLAlchemy expression is brittle
    (``in_`` renders as a POSTCOMPILE bind name), so the conditions are
    compiled against a real dialect with ``literal_binds`` — the resulting SQL
    contains the actual status literals, which is what these tests assert.
    """

    stmt = select(Run).where(*conditions)  # type: ignore[arg-type]
    return str(stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))


def _run(status: str, *, thread_id: str | None = None) -> Run:
    tid = thread_id or f"{_ORG}:{uuid.uuid4()}"
    return Run(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        trigger_type="manual",
        status=status,
        run_number=1,
        input_hash="a" * 64,
        langgraph_thread_id=tid,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        outputs_json={"k": "v" * 100},
        node_telemetry_json={"n": 1},
        cost_breakdown=[{"amount": "1"}],
    )


def _nested_cm(*, enter_exc: Exception | None = None) -> MagicMock:
    cm = MagicMock()
    if enter_exc is not None:
        cm.__aenter__ = AsyncMock(side_effect=enter_exc)
    else:
        cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# _retention_conditions — filter building (candidate listing / export / purge)
# ---------------------------------------------------------------------------


class TestRetentionConditions:
    def test_org_scope_added_when_org_id_given(self) -> None:
        conds = rr._retention_conditions(
            org_id=_ORG, date_from=None, date_to=None, pipeline_id=None, status=None, statuses=None
        )
        assert any("organisation_id" in str(c) for c in conds)

    def test_no_org_scope_for_cross_org(self) -> None:
        conds = rr._retention_conditions(
            org_id=None, date_from=None, date_to=None, pipeline_id=None, status=None, statuses=None
        )
        assert len(conds) == 0

    def test_date_range_conditions(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 2, 1, tzinfo=UTC)
        conds = rr._retention_conditions(
            org_id=None, date_from=start, date_to=end, pipeline_id=None, status=None, statuses=None
        )
        text = "\n".join(str(c) for c in conds)
        assert ">=" in text
        assert "<=" in text

    def test_terminal_status_whitelist_limits_status_expression(self) -> None:
        conds = rr._retention_conditions(
            org_id=None, date_from=None, date_to=None, pipeline_id=None, status=None, statuses=TERMINAL_STATUSES
        )
        sql = _compile_with_literal_binds(conds)
        # The whitelist is exactly the terminal set — never a live status.
        for ts in TERMINAL_STATUSES:
            assert ts in sql
        assert "running" not in sql
        assert "pending" not in sql
        assert "awaiting_human" not in sql

    def test_non_purgable_status_yields_no_match(self) -> None:
        """A live status requested on the purge can never widen the whitelist."""
        conds = rr._retention_conditions(
            org_id=None,
            date_from=None,
            date_to=None,
            pipeline_id=None,
            status="running",
            statuses=TERMINAL_STATUSES,
        )
        sql = _compile_with_literal_binds(conds)
        # The terminal whitelist collides with the requested live status, so the
        # purge can never match anything.
        assert "running" not in sql
        assert "id IS NULL" in sql or "1 != 1" in sql or "false" in sql.lower()


# ---------------------------------------------------------------------------
# list_retention_candidates — count + page + per-run estimate orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestListRetentionCandidates:
    async def test_returns_count_and_estimated_bytes(self) -> None:
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 3
        session.execute = AsyncMock(return_value=count_result)
        runs = [_run("complete"), _run("failed")]

        # FAR-583: per-run blob bytes come from the repo reader; the mock
        # session cannot execute it, so the orchestration-level test mocks
        # the reader (zero bytes) like _checkpoint_detail above.
        # FAR-660: the whole-set estimate is the SQL-side grouped scan —
        # mocked here at the orchestration boundary.
        with (
            patch.object(rr, "_select_run_page", new=AsyncMock(return_value=runs)),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({"tid": 500}, {"tid": 2}))),
            patch.object(rr, "_estimate_bytes_by_status", new=AsyncMock(return_value={"complete": 100, "failed": 23})),
            patch.object(rr, "read_node_output_blob_bytes", new=AsyncMock(return_value={})),
        ):
            result = await rr.list_retention_candidates(session, org_id=_ORG, status=None)

        assert result["total_count"] == 3
        # Both statuses are terminal, so the whole-set estimate is the sum of
        # every status group and the terminal figure equals it.
        assert result["total_estimated_bytes"] == 123
        assert result["terminal_estimated_bytes"] == 123
        assert len(result["runs"]) == 2
        # Each run's estimate = its own JSON columns + its checkpoint bytes.
        for item in result["runs"]:
            # thread "tid" yields 500 checkpoint bytes; "tid" is not one of the
            # runs' real thread-ids, so their checkpoint bytes resolve to 0.
            assert item["estimated_bytes"] >= rr._run_row_bytes(_run("failed"))

    async def test_terminal_total_is_surfaced_and_distinct_from_total(self) -> None:
        """terminal_total/terminal_estimated_bytes are reported separately so the
        purge UI can show the uncapped terminal count, not the page-capped set.

        FAR-660: the terminal byte figure derives from the grouped estimate's
        terminal-status groups only — no second walk of the dataset."""
        session = AsyncMock()
        total_result = MagicMock()
        total_result.scalar_one.return_value = 10
        terminal_result = MagicMock()
        terminal_result.scalar_one.return_value = 7
        # First execute = total_count (all matches); second = terminal_total.
        session.execute = AsyncMock(side_effect=[total_result, terminal_result])
        runs = [_run("complete"), _run("failed")]

        with (
            patch.object(rr, "_select_run_page", new=AsyncMock(return_value=runs)),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({}, {}))),
            patch.object(rr, "_estimate_bytes_by_status", new=AsyncMock(return_value={"complete": 99, "pending": 5})),
            patch.object(rr, "read_node_output_blob_bytes", new=AsyncMock(return_value={})),
        ):
            result = await rr.list_retention_candidates(session, org_id=_ORG, status=None)

        assert result["total_count"] == 10
        assert result["terminal_total"] == 7
        assert result["total_estimated_bytes"] == 104
        assert result["terminal_estimated_bytes"] == 99

    async def test_deadline_is_threaded_to_the_estimator(self) -> None:
        """The candidates listing passes its deadline through to the estimator
        untouched; with no explicit deadline the estimator applies the module
        default budget (asserted in TestEstimateBytesByStatus)."""
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=count_result)
        estimate = AsyncMock(return_value={})

        with (
            patch.object(rr, "_select_run_page", new=AsyncMock(return_value=[])),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({}, {}))),
            patch.object(rr, "_estimate_bytes_by_status", new=estimate),
            patch.object(rr, "read_node_output_blob_bytes", new=AsyncMock(return_value={})),
        ):
            await rr.list_retention_candidates(session, org_id=_ORG, status=None)
            assert estimate.call_args.kwargs["deadline"] is None

            explicit_deadline = time.monotonic() + 5
            await rr.list_retention_candidates(session, org_id=_ORG, status=None, deadline=explicit_deadline)
            assert estimate.call_args.kwargs["deadline"] == explicit_deadline


# ---------------------------------------------------------------------------
# _estimate_bytes_by_status — the FAR-660 SQL-side estimate (aggregate SQL
# building + bounded best-effort execution)
# ---------------------------------------------------------------------------


class _FakeEstimateRows:
    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, int]]:
        return list(self._rows)


class _EstimateSession:
    """Fake AsyncSession for the estimate pass: canned per-component rows."""

    def __init__(self, results: list[list[tuple[str, int]]]) -> None:
        self._results = list(results)
        self.executed: list[object] = []
        self.begin_nested = MagicMock(return_value=_nested_cm())

    async def execute(self, stmt: object, params: object = None) -> _FakeEstimateRows:
        self.executed.append(stmt)
        rows = self._results.pop(0) if self._results else []
        return _FakeEstimateRows(rows)


class TestEstimateStatementBuilders:
    """The aggregate SQL is built (never string-interpolated) — compile each
    statement and assert its shape against a real dialect."""

    def _compile(self, stmt: object, dialect: object, *, literal_binds: bool = False) -> str:
        compile_kwargs = {"literal_binds": True} if literal_binds else {}
        return str(stmt.compile(dialect=dialect, compile_kwargs=compile_kwargs))  # type: ignore[attr-defined]

    def test_runs_payload_stmt_groups_by_status_and_measures_the_three_columns(self) -> None:
        sql = self._compile(rr._runs_payload_stmt([]), sqlite.dialect())
        assert "GROUP BY runs.status" in sql
        for col in ("cost_breakdown", "input_payload", "run_classification"):
            assert col in sql
        assert "length(" in sql

    def test_runs_payload_stmt_applies_the_filter_conditions(self) -> None:
        sql = self._compile(rr._runs_payload_stmt([Run.status == "failed"]), sqlite.dialect(), literal_binds=True)
        assert "failed" in sql

    def test_node_output_stmt_excludes_metadata_rows_and_joins_runs(self) -> None:
        sql = self._compile(rr._node_output_stmt([]), sqlite.dialect(), literal_binds=True)
        assert "__run_meta__" in sql
        assert "run_node_outputs" in sql
        assert "JOIN runs" in sql
        assert "GROUP BY runs.status" in sql

    def test_checkpoint_agg_stmt_uses_octet_length_and_joins_on_thread(self) -> None:
        cp_table, size_expr = rr._CHECKPOINT_AGG_SOURCES[0]
        sql = self._compile(rr._checkpoint_agg_stmt(cp_table, size_expr, [], None), postgresql.dialect())
        assert "octet_length" in sql
        assert "checkpoints" in sql
        assert "runs.langgraph_thread_id = checkpoints.thread_id" in sql
        assert "GROUP BY runs.status" in sql
        # Cross-org system admin (org_id=None): no checkpoint org filter.
        assert "organisation_id" not in sql

    def test_checkpoint_agg_stmt_adds_org_filter_when_scoped(self) -> None:
        cp_table, size_expr = rr._CHECKPOINT_AGG_SOURCES[0]
        sql = self._compile(rr._checkpoint_agg_stmt(cp_table, size_expr, [], _ORG), postgresql.dialect())
        assert "checkpoints.organisation_id =" in sql

    def test_checkpoint_blob_sources_measure_the_bytea_column(self) -> None:
        for cp_table, size_expr in rr._CHECKPOINT_AGG_SOURCES[1:]:
            sql = self._compile(rr._checkpoint_agg_stmt(cp_table, size_expr, [], None), postgresql.dialect())
            assert cp_table.name in sql
            assert f"octet_length({cp_table.name}.blob)" in sql


@pytest.mark.asyncio
class TestEstimateBytesByStatus:
    async def test_accumulates_per_status_across_all_five_components(self) -> None:
        """Component order: runs payload, run_node_outputs, then the three
        checkpoint tables; per-status bytes accumulate across all of them."""
        session = _EstimateSession(
            [
                [("complete", 10), ("failed", 5)],
                [("complete", 3)],
                [("complete", 100)],
                [("failed", 7)],
                [],
            ]
        )
        result = await rr._estimate_bytes_by_status(session, org_id=_ORG)

        assert result == {"complete": 113, "failed": 12}
        assert len(session.executed) == 5
        assert session.begin_nested.call_count == 5

    async def test_default_deadline_is_applied_when_none(self) -> None:
        session = _EstimateSession([])
        await rr._estimate_bytes_by_status(session, org_id=_ORG)
        # The default budget is in the future, so every component ran.
        assert len(session.executed) == 5

    async def test_expired_deadline_skips_every_component(self) -> None:
        session = _EstimateSession([])
        result = await rr._estimate_bytes_by_status(session, org_id=_ORG, deadline=time.monotonic() - 1)

        assert not result
        assert not session.executed
        session.begin_nested.assert_not_called()

    async def test_failed_component_is_skipped_without_failing_the_estimate(self) -> None:
        """A failed scan (missing table / unsupported function) contributes 0;
        the remaining components still run and the caller still gets a total."""
        session = _EstimateSession([[("complete", 10)]])
        calls = {"n": 0}
        canned = [[("complete", 10)], None, [("complete", 100)], [], []]

        async def flaky_execute(stmt: object, params: object = None) -> _FakeEstimateRows:
            calls["n"] += 1
            if calls["n"] == 2:
                raise SQLAlchemyError("no such function: length")
            rows = canned[calls["n"] - 1] or []
            return _FakeEstimateRows(rows)

        session.execute = flaky_execute
        result = await rr._estimate_bytes_by_status(session, org_id=_ORG)

        assert result == {"complete": 110}
        assert calls["n"] == 5


# ---------------------------------------------------------------------------
# purge_terminal_runs — terminal-only, checkpoint cascade, batching, idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPurgeTerminalRuns:
    async def _purge(self, session: AsyncMock, **kwargs: object) -> dict[str, int]:
        return await rr.purge_terminal_runs(session, org_id=_ORG, **kwargs)

    async def test_purges_only_terminal_runs(self) -> None:
        """The select must be restricted to TERMINAL_STATUSES (never a live run)."""
        session = AsyncMock()
        captured: dict[str, object] = {}

        async def fake_select(session_arg, **kwargs):
            captured.update(kwargs)
            return []

        with (
            patch.object(rr, "_select_run_page", side_effect=fake_select),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({}, {}))),
        ):
            await self._purge(session)

        assert captured["statuses"] == TERMINAL_STATUSES
        assert captured["status"] is None
        session.begin_nested.assert_not_called()

    async def test_deletes_checkpoints_and_run_id_rows_then_runs(self) -> None:
        """Each purge batch deletes checkpoints + SET NULL/RESTRICT rows + runs."""
        session = AsyncMock()
        session.begin_nested = MagicMock(return_value=_nested_cm())
        runs = [_run("complete"), _run("failed")]
        thread_ids = [r.langgraph_thread_id for r in runs]
        delete_checkpoints = AsyncMock()
        delete_run_id_rows = AsyncMock()

        with (
            patch.object(rr, "_select_run_page", side_effect=[runs, []]),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({}, {"t1": 3}))),
            patch.object(rr, "_delete_checkpoints", new=delete_checkpoints),
            patch.object(rr, "_delete_run_id_rows", new=delete_run_id_rows),
            patch.object(rr, "read_node_output_blob_bytes", new=AsyncMock(return_value={})),
        ):
            result = await self._purge(session)

        assert result["purged_runs"] == 2
        assert result["purged_checkpoints"] == 3
        delete_checkpoints.assert_awaited_once()
        delete_run_id_rows.assert_awaited_once()
        # Checkpoints were targetted with exactly the two runs' thread-ids.
        assert delete_checkpoints.call_args.args[1] == thread_ids
        assert delete_checkpoints.call_args.args[2] == _ORG
        session.flush.assert_awaited()
        # One SAVEPOINT for the single purge batch (the sibling
        # test_batches_at_batch_size proves one main savepoint per batch) —
        # plus the best-effort quarantine delete's OWN savepoint in it.
        assert session.begin_nested.call_count == 2

    async def test_purge_deletes_quarantine_rows_for_the_batch(self) -> None:
        """qa Major 3a: each purge batch explicitly deletes the batch's
        ``run_node_outputs_quarantine`` rows — the table deliberately has no
        FK, so without the explicit delete every purged run would leave a
        permanent orphaned blob copy."""
        session = AsyncMock()
        session.begin_nested = MagicMock(return_value=_nested_cm())
        runs = [_run("complete")]
        delete_quarantine = AsyncMock()

        with (
            patch.object(rr, "_select_run_page", side_effect=[runs, []]),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({}, {}))),
            patch.object(rr, "_delete_checkpoints", new=AsyncMock()),
            patch.object(rr, "_delete_quarantine_rows", new=delete_quarantine),
            patch.object(rr, "_delete_run_id_rows", new=AsyncMock()),
            patch.object(rr, "read_node_output_blob_bytes", new=AsyncMock(return_value={})),
        ):
            await self._purge(session)

        delete_quarantine.assert_awaited_once_with(session, [runs[0].id])

    async def test_batches_at_batch_size(self) -> None:
        """A set larger than batch_size is processed in more than one SAVEPOINT."""
        session = AsyncMock()
        session.begin_nested = MagicMock(return_value=_nested_cm())
        runs = [_run("complete") for _ in range(5)]
        counter = {"n": 0}

        def fake_select(session_arg, **kwargs):
            counter["n"] += 1
            if counter["n"] == 1:
                return runs[:2]
            if counter["n"] == 2:
                return runs[2:]
            return []

        with (
            patch.object(rr, "_select_run_page", side_effect=fake_select),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({}, {}))),
            patch.object(rr, "_delete_checkpoints", new=AsyncMock()),
            patch.object(rr, "_delete_run_id_rows", new=AsyncMock()),
            patch.object(rr, "read_node_output_blob_bytes", new=AsyncMock(return_value={})),
        ):
            result = await self._purge(session, batch_size=2)

        assert result["purged_runs"] == 5
        # Two batches x two savepoints each (qa Major 3a: the best-effort
        # quarantine delete runs in its OWN savepoint inside every batch,
        # beside the batch's main purge savepoint).
        assert session.begin_nested.call_count == 4

    async def test_idempotent_when_no_matching_runs(self) -> None:
        """Re-running after deletion selects nothing and reports zero."""
        session = AsyncMock()
        with patch.object(rr, "_select_run_page", new=AsyncMock(return_value=[])):
            result = await self._purge(session)
        assert result == {"purged_runs": 0, "purged_checkpoints": 0, "freed_estimated_bytes": 0}
        session.begin_nested.assert_not_called()

    async def test_batch_failure_rolls_back_that_batch_and_stops(self) -> None:
        """A SAVEPOINT failure rolls back the batch and returns partial counts."""
        session = AsyncMock()
        session.begin_nested = MagicMock(return_value=_nested_cm(enter_exc=RuntimeError("boom")))
        runs = [_run("complete")]
        counter = {"n": 0}

        def fake_select(session_arg, **kwargs):
            counter["n"] += 1
            return runs if counter["n"] == 1 else []

        with (
            patch.object(rr, "_select_run_page", side_effect=fake_select),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({}, {}))),
            patch.object(rr, "read_node_output_blob_bytes", new=AsyncMock(return_value={})),
        ):
            result = await self._purge(session)

        assert result["purged_runs"] == 0  # the failed batch was rolled back
        assert result["freed_estimated_bytes"] == 0

    async def test_live_status_request_cannot_widen_terminal_set(self) -> None:
        """Even an explicit `status=running` purge request stays terminal-only."""
        session = AsyncMock()
        selected_statuses: list[object] = []

        async def fake_select(session_arg, **kwargs):
            selected_statuses.append(kwargs.get("statuses"))
            return []

        with patch.object(rr, "_select_run_page", side_effect=fake_select):
            await self._purge(session, _status="running")

        assert selected_statuses == [TERMINAL_STATUSES]


# ---------------------------------------------------------------------------
# _delete_checkpoints — the raw checkpoint cascade issues deletes per table
# ---------------------------------------------------------------------------


class TestDeleteCheckpoints:
    async def test_deletes_all_three_checkpoint_tables_scoped_to_org(self) -> None:
        """Every langgraph.* checkpoint table is targetted for the thread-ids."""

        executed: list[tuple[str, dict[str, object]]] = []

        class RecordingSession:
            async def execute(self, stmt, params):
                executed.append((str(stmt), dict(params)))

        thread_ids = ["org:t1", "org:t2"]
        await rr._delete_checkpoints(RecordingSession(), thread_ids, _ORG)  # type: ignore[arg-type]

        stmts = [s for s, _ in executed]
        assert any("checkpoints" in s for s in stmts)
        assert any("checkpoint_blobs" in s for s in stmts)
        assert any("checkpoint_writes" in s for s in stmts)
        assert any(p.get("org") == str(_ORG) for _, p in executed)
        assert any(p.get("tids") == thread_ids for _, p in executed)


class TestDeleteRunIdRows:
    async def test_deletes_set_null_and_restrict_tables_via_orm(self) -> None:
        """trigger_events / notification_delivery_log deleted (leases table dropped, FAR-587)."""
        session = AsyncMock()
        run_ids = [uuid.uuid4()]
        await rr._delete_run_id_rows(session, run_ids)
        statements = [c.args[0] for c in session.execute.call_args_list]
        assert len(statements) == 2
        names = [getattr(getattr(s, "table", None), "name", None) for s in statements]
        assert "trigger_events" in names
        assert "notification_delivery_log" in names


class TestDeleteQuarantineRows:
    """qa Major 3a: the no-FK quarantine table needs an explicit delete —
    best-effort (own savepoint) because the app role has no grant on it on
    Postgres and a failure must never abort the run purge."""

    async def test_deletes_quarantine_rows_for_the_run_ids(self) -> None:
        session = AsyncMock()
        session.begin_nested = MagicMock(return_value=_nested_cm())
        run_ids = [uuid.uuid4(), uuid.uuid4()]
        await rr._delete_quarantine_rows(session, run_ids)
        statements = [c.args[0] for c in session.execute.call_args_list]
        assert len(statements) == 1
        names = [getattr(getattr(s, "table", None), "name", None) for s in statements]
        assert names == ["run_node_outputs_quarantine"]

    async def test_no_run_ids_opens_no_savepoint(self) -> None:
        session = AsyncMock()
        await rr._delete_quarantine_rows(session, [])
        session.begin_nested.assert_not_called()
        session.execute.assert_not_called()

    async def test_delete_failure_is_swallowed_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """A privilege / missing-table failure (Postgres: no app-role grant on
        the quarantine table) must not raise into the purge batch."""
        session = AsyncMock()
        session.begin_nested = MagicMock(return_value=_nested_cm(enter_exc=RuntimeError("permission denied")))
        caplog.set_level("WARNING", logger="modulo.db.crud.run_retention")
        await rr._delete_quarantine_rows(session, [uuid.uuid4()])
        assert any("quarantine_delete_unavailable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# purge_terminal_checkpoints — checkpoint-only purge, keeps the runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPurgeTerminalCheckpoints:
    def _run_select(self, threads: list[str]) -> MagicMock:
        result = MagicMock()
        result.scalars.return_value.all.return_value = threads
        return result

    async def test_purges_only_checkpoints_keeping_runs(self) -> None:
        """Checkpoint rows are deleted for old terminal threads; runs untouched."""
        session = AsyncMock()
        threads = ["org:t1", "org:t2"]
        session.execute = AsyncMock(return_value=self._run_select(threads))
        delete_checkpoints = AsyncMock()

        with (
            patch.object(
                rr,
                "_checkpoint_detail",
                new=AsyncMock(return_value=({"org:t1": 100, "org:t2": 200}, {"org:t1": 3, "org:t2": 4})),
            ),
            patch.object(rr, "_delete_checkpoints", new=delete_checkpoints),
        ):
            result = await rr.purge_terminal_checkpoints(session, org_id=_ORG)

        assert result == {"checkpoints_purged": 7, "threads_purged": 2, "bytes_freed": 300}
        # Only checkpoint rows are deleted — never a ``runs`` row.
        delete_checkpoints.assert_awaited_once()
        assert delete_checkpoints.call_args.args[1] == threads
        assert delete_checkpoints.call_args.args[2] == _ORG
        session.flush.assert_awaited()

    async def test_idempotent_when_no_matching_runs(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(return_value=self._run_select([]))
        with (
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({}, {}))),
            patch.object(rr, "_delete_checkpoints", new=AsyncMock()),
        ):
            result = await rr.purge_terminal_checkpoints(session, org_id=_ORG)
        assert result == {"checkpoints_purged": 0, "threads_purged": 0, "bytes_freed": 0}
        session.flush.assert_not_called()

    async def test_batches_by_thread_and_never_deletes_runs(self) -> None:
        """More thread-ids than batch_size are processed in multiple passes."""
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                self._run_select(["org:t1", "org:t2"]),
                self._run_select(["org:t3"]),
                self._run_select([]),
            ]
        )
        with (
            patch.object(
                rr,
                "_checkpoint_detail",
                new=AsyncMock(
                    side_effect=[
                        ({"org:t1": 10, "org:t2": 20}, {"org:t1": 1, "org:t2": 2}),
                        ({"org:t3": 30}, {"org:t3": 3}),
                    ]
                ),
            ),
            patch.object(rr, "_delete_checkpoints", new=AsyncMock()),
        ):
            result = await rr.purge_terminal_checkpoints(session, org_id=_ORG, batch_size=2)
        assert result["threads_purged"] == 3
        assert result["checkpoints_purged"] == 6
        assert result["bytes_freed"] == 60
        assert session.flush.call_count == 2


# ---------------------------------------------------------------------------
# _json_bytes / _run_row_bytes — estimate helpers
# ---------------------------------------------------------------------------


class TestEstimateHelpers:
    def test_json_bytes_none_is_zero(self) -> None:
        assert rr._json_bytes(None) == 0

    def test_json_bytes_measures_serialized_length(self) -> None:
        assert rr._json_bytes({"a": "bbbb"}) == len('{"a": "bbbb"}')

    def test_run_row_bytes_sums_payload_columns_and_node_output_bytes(self) -> None:
        """FAR-583: the per-node blobs (outputs / telemetry / markers) come
        from the run_node_outputs store, passed in as ``node_output_bytes``
        (metadata rows already excluded by the repo reader); the remaining
        run-row payloads are summed as before."""
        run = _run("complete")
        node_output_bytes = 4321
        expected = node_output_bytes + sum(
            rr._json_bytes(v)
            for v in (
                run.cost_breakdown,
                run.input_payload,
                run.run_classification,
            )
        )
        assert rr._run_row_bytes(run, node_output_bytes) == expected

    def test_run_row_bytes_defaults_to_zero_node_output_bytes(self) -> None:
        run = _run("complete")
        expected = sum(
            rr._json_bytes(v)
            for v in (
                run.cost_breakdown,
                run.input_payload,
                run.run_classification,
            )
        )
        assert rr._run_row_bytes(run) == expected

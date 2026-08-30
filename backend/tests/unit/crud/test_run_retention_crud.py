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

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

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

        with (
            patch.object(rr, "_select_run_page", new=AsyncMock(return_value=runs)),
            patch.object(rr, "_checkpoint_detail", new=AsyncMock(return_value=({"tid": 500}, {"tid": 2}))),
            patch.object(rr, "_estimate_total_bytes", new=AsyncMock(return_value=12345)),
        ):
            result = await rr.list_retention_candidates(session, org_id=_ORG, status=None)

        assert result["total_count"] == 3
        assert result["total_estimated_bytes"] == 12345
        assert len(result["runs"]) == 2
        # Each run's estimate = its own JSON columns + its checkpoint bytes.
        for item in result["runs"]:
            # thread "tid" yields 500 checkpoint bytes; "tid" is not one of the
            # runs' real thread-ids, so their checkpoint bytes resolve to 0.
            assert item["estimated_bytes"] >= rr._run_row_bytes(_run("failed"))

    async def test_terminal_total_is_surfaced_and_distinct_from_total(self) -> None:
        """terminal_total/terminal_estimated_bytes are reported separately so the
        purge UI can show the uncapped terminal count, not the page-capped set."""
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
            patch.object(rr, "_estimate_total_bytes", new=AsyncMock(return_value=99)),
        ):
            result = await rr.list_retention_candidates(session, org_id=_ORG, status=None)

        assert result["total_count"] == 10
        assert result["terminal_total"] == 7
        assert result["terminal_estimated_bytes"] == 99


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
        # Exactly one SAVEPOINT for the single purge batch (the sibling
        # test_batches_at_batch_size proves one begin_nested per batch).
        session.begin_nested.assert_called_once()

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
        ):
            result = await self._purge(session, batch_size=2)

        assert result["purged_runs"] == 5
        assert session.begin_nested.call_count == 2  # two full batches: 2 runs + 3 runs

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
        """trigger_events / notification_delivery_log / workspace_leases deleted."""
        session = AsyncMock()
        run_ids = [uuid.uuid4()]
        await rr._delete_run_id_rows(session, run_ids)
        statements = [c.args[0] for c in session.execute.call_args_list]
        assert len(statements) == 3
        names = [getattr(getattr(s, "table", None), "name", None) for s in statements]
        assert "trigger_events" in names
        assert "notification_delivery_log" in names
        assert "workspace_leases" in names


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

    def test_run_row_bytes_sums_payload_columns(self) -> None:
        run = _run("complete")
        expected = sum(
            rr._json_bytes(v)
            for v in (
                run.outputs_json,
                run.node_telemetry_json,
                run.cost_breakdown,
                run.input_payload,
                run.raw_output_markers,
                run.run_classification,
            )
        )
        assert rr._run_row_bytes(run) == expected

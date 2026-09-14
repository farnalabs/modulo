"""Unit tests for the FAR-801 workspace-input drift compensating sweep.

Covers ``_sweep_workspace_input_drift_flags`` in ``modulo.core.cron_helpers``:

* a terminal run whose audit row reports drift is corrected (flag -> True);
* a terminal run whose audit row reports NO drift is written definitive
  False (drops out of the NULL-flag set, so the bounded scan never loops it);
* a terminal run with no audit row is written definitive False and drops out
  of the NULL-flag set (no longer re-scanned — prevents the starvation that
  clogged the first-200 window when no-audit rows stayed NULL);
* a run whose flag is already set is excluded from the bounded scan;
* a read failure is swallowed (logged, not raised) and the run is untouched.

Uses an in-memory SQLite engine with only the ``runs`` / ``run_node_outputs`` /
``organisations`` tables (no ORM tenant-filter listener, no RLS).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.cron_helpers import _sweep_workspace_input_drift_flags
from modulo.core.pipeline_engine.workspace_input_audit import AUDIT_NODE_ID
from modulo.db.crud.run_node_outputs import FINAL_ATTEMPT_KEY, read_audit_row_outputs_json
from modulo.db.models.base import Base
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.run_node_outputs import RunNodeOutput

_TABLE_NAMES = {"runs", "run_node_outputs", "organisations"}

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_PROJECT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SNAPSHOT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _make_run(
    *,
    run_id: uuid.UUID,
    status: str,
    drift_detected: bool | None,
    run_number: int = 1,
) -> Run:
    return Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PROJECT_ID,
        snapshot_id=_SNAPSHOT_ID,
        trigger_type="manual",
        status=status,
        run_number=run_number,
        input_hash="0" * 64,
        langgraph_thread_id=f"thread-{run_id.hex}",
        workspace_inputs_drift_detected=drift_detected,
    )


def _make_audit_row(*, run_id: uuid.UUID, entries: list[dict[str, Any]]) -> RunNodeOutput:
    return RunNodeOutput(
        organisation_id=_ORG,
        run_id=run_id,
        node_id=AUDIT_NODE_ID,
        attempt_key=FINAL_ATTEMPT_KEY,
        outputs_json={"workspace_inputs": entries},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Sweep behaviour
# ---------------------------------------------------------------------------


class TestSweepWorkspaceInputDriftFlags:
    @pytest.mark.anyio
    async def test_corrects_terminal_run_with_drift(self, factory: async_sessionmaker[AsyncSession]) -> None:
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id, status="complete", drift_detected=None))
            session.add(
                _make_audit_row(
                    run_id=run_id,
                    entries=[
                        {
                            "input_name": "my-repo",
                            "resolved_sha": "a" * 40,
                            "final_sha": "b" * 40,
                            "drift_detected": True,
                        }
                    ],
                )
            )
            await session.commit()

        result = await _sweep_workspace_input_drift_flags(factory)
        assert result == {"scanned": 1, "corrected": 1}

        async with factory() as session:
            row = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert row.workspace_inputs_drift_detected is True

    @pytest.mark.anyio
    async def test_writes_definitive_false_when_no_drift(self, factory: async_sessionmaker[AsyncSession]) -> None:
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id, status="failed", drift_detected=None))
            session.add(
                _make_audit_row(
                    run_id=run_id,
                    entries=[
                        {
                            "input_name": "my-repo",
                            "resolved_sha": "a" * 40,
                            "final_sha": "a" * 40,
                            "drift_detected": False,
                        }
                    ],
                )
            )
            await session.commit()

        result = await _sweep_workspace_input_drift_flags(factory)
        assert result == {"scanned": 1, "corrected": 0}

        async with factory() as session:
            row = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            # Definitive False (not NULL) so the run drops out of the NULL set.
            assert row.workspace_inputs_drift_detected is False

    @pytest.mark.anyio
    async def test_writes_definitive_false_when_no_audit_row(self, factory: async_sessionmaker[AsyncSession]) -> None:
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id, status="complete", drift_detected=None))
            await session.commit()

        result = await _sweep_workspace_input_drift_flags(factory)
        # No audit row => definitive False (not NULL), so the run drops out of
        # the NULL-flag set and is never re-scanned.
        assert result == {"scanned": 1, "corrected": 0}

        async with factory() as session:
            row = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert row.workspace_inputs_drift_detected is False

    @pytest.mark.anyio
    async def test_no_audit_runs_drop_out_of_subsequent_scan(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Regression for the starvation defect: no-audit NULL rows must not
        clog the first-200 window and prevent a later drifted run from ever
        being corrected. Two no-audit runs and one later drifted run prove the
        no-audit runs become definitive False on the first tick and the drifted
        run is corrected on the next."""
        no_audit_ids = [uuid.uuid4() for _ in range(2)]
        drift_id = uuid.uuid4()
        async with factory() as session:
            for idx, run_id in enumerate(no_audit_ids):
                session.add(_make_run(run_id=run_id, status="complete", drift_detected=None, run_number=idx + 1))
            session.add(
                _make_run(
                    run_id=drift_id,
                    status="complete",
                    drift_detected=None,
                    run_number=len(no_audit_ids) + 1,
                )
            )
            session.add(
                _make_audit_row(
                    run_id=drift_id,
                    entries=[
                        {
                            "input_name": "my-repo",
                            "resolved_sha": "a" * 40,
                            "final_sha": "b" * 40,
                            "drift_detected": True,
                        }
                    ],
                )
            )
            await session.commit()

        # First tick: the no-audit runs become False; the drifted run is corrected.
        first = await _sweep_workspace_input_drift_flags(factory)
        assert first["scanned"] == 3
        assert first["corrected"] == 1

        # Second tick: no-audit runs are excluded (definitive False), only the
        # already-corrected drifted run remains — proving the no-audit rows no
        # longer re-occupy the bounded window and starve real corrections.
        second = await _sweep_workspace_input_drift_flags(factory)
        assert second["scanned"] == 0
        assert second["corrected"] == 0

        async with factory() as session:
            for run_id in no_audit_ids:
                row = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                assert row.workspace_inputs_drift_detected is False
            drift_row = (await session.execute(select(Run).where(Run.id == drift_id))).scalar_one()
            assert drift_row.workspace_inputs_drift_detected is True

    @pytest.mark.anyio
    async def test_excludes_runs_already_flagged(self, factory: async_sessionmaker[AsyncSession]) -> None:
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id, status="complete", drift_detected=False))
            await session.commit()

        result = await _sweep_workspace_input_drift_flags(factory)
        assert result == {"scanned": 0, "corrected": 0}

    @pytest.mark.anyio
    async def test_excludes_active_runs(self, factory: async_sessionmaker[AsyncSession]) -> None:
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id, status="running", drift_detected=None))
            session.add(
                _make_audit_row(
                    run_id=run_id,
                    entries=[
                        {
                            "input_name": "my-repo",
                            "resolved_sha": "a" * 40,
                            "final_sha": "b" * 40,
                            "drift_detected": True,
                        }
                    ],
                )
            )
            await session.commit()

        result = await _sweep_workspace_input_drift_flags(factory)
        assert result == {"scanned": 0, "corrected": 0}

    @pytest.mark.anyio
    async def test_read_failure_is_swallowed(
        self, factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id, status="complete", drift_detected=None))
            session.add(
                _make_audit_row(
                    run_id=run_id,
                    entries=[
                        {
                            "input_name": "my-repo",
                            "resolved_sha": "a" * 40,
                            "final_sha": "b" * 40,
                            "drift_detected": True,
                        }
                    ],
                )
            )
            await session.commit()

        with patch(
            "modulo.db.crud.run_node_outputs.read_audit_row_outputs_json",
            side_effect=RuntimeError("db blip"),
        ):
            result = await _sweep_workspace_input_drift_flags(factory)

        # Failure swallowed: run untouched, result still returned (not raised).
        assert result["scanned"] == 1
        assert result["corrected"] == 0
        assert "workspace_input_drift_sweep.error" in caplog.text

        async with factory() as session:
            row = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert row.workspace_inputs_drift_detected is None

    @pytest.mark.anyio
    async def test_reader_contract_matches_producer(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """The sweep reads the audit row at FINAL_ATTEMPT_KEY via
        read_audit_row_outputs_json — pin that the producer-written row is
        exactly what the sweep finds (regression guard for the attempt-key
        contract)."""
        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id, status="complete", drift_detected=None))
            session.add(
                _make_audit_row(
                    run_id=run_id,
                    entries=[
                        {
                            "input_name": "my-repo",
                            "resolved_sha": "a" * 40,
                            "final_sha": "c" * 40,
                            "drift_detected": True,
                        }
                    ],
                )
            )
            await session.commit()

        async with factory() as session:
            audit_row = await read_audit_row_outputs_json(session, run_id=run_id, node_id=AUDIT_NODE_ID)
        assert audit_row is not None
        assert audit_row["workspace_inputs"][0]["drift_detected"] is True

        assert "complete" in TERMINAL_STATUSES

"""FAR-902: index predicate + sweep wiring + integration tests (D4, D7).

Verifies that the partial index predicate in migration 0250 matches the
analytics sweep query filter, and that the sweep is wired into
dispatcher_reconcile.  The integration tests prove the sweep actually
executes its query and performs the correction through the SAME interface
the production caller uses (a factory), not merely that some function was
invoked.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.models.base import Base
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.run_node_outputs import RunNodeOutput

# The migration file (structural: no DB needed).
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0251_schema_enforcement_telemetry.py"
)

# The sweep module source.
_SWEEP_MODULE = Path(__file__).resolve().parents[3] / "src" / "modulo" / "core" / "analytics" / "enforcement_sweep.py"


def test_index_predicate_matches_sweep_filter() -> None:
    """The partial index predicate and the sweep query WHERE clause are identical.

    This prevents drift: if someone changes the sweep query but not the index
    (or vice versa), this test catches it.
    """
    migration_source = _MIGRATION_PATH.read_text(encoding="utf-8")
    sweep_source = _SWEEP_MODULE.read_text(encoding="utf-8")

    # Both must use the same predicate fragments.
    predicate_parts = [
        "schema_enforcement_json IS NOT NULL",
        "attempt_key <> '__final__'",
    ]
    for part in predicate_parts:
        assert part in migration_source, f"Migration missing predicate: {part}"
        assert part in sweep_source, f"Sweep missing predicate: {part}"


def test_sweep_wired_into_dispatcher_reconcile() -> None:
    """The sweep is imported and called from _run_reconcile_sweeps in cron_helpers."""
    cron_helpers_path = Path(__file__).resolve().parents[3] / "src" / "modulo" / "core" / "cron_helpers.py"
    source = cron_helpers_path.read_text(encoding="utf-8")
    assert "sweep_schema_enforcement_facts" in source
    assert "enforcement_sweep_scanned" in source
    assert "enforcement_sweep_corrected" in source


def test_sweep_module_has_named_predicate_constant() -> None:
    """The sweep module exports ENFORCEMENT_SWEEP_PREDICATE as a named constant.

    A sweep with zero production callers is a silent critical — the named
    constant makes the predicate grep-able and verifiable.
    """
    sweep_source = _SWEEP_MODULE.read_text(encoding="utf-8")
    assert "ENFORCEMENT_SWEEP_PREDICATE" in sweep_source
    assert "schema_enforcement_json IS NOT NULL" in sweep_source
    assert "attempt_key <> '__final__'" in sweep_source


def test_sweep_accepts_factory_not_session() -> None:
    """CRITICAL 2 regression: the sweep function signature must accept a factory.

    The production caller passes ``_open_system_factory()`` (an
    ``async_sessionmaker``), not a bare ``AsyncSession``.  If someone changes
    the parameter name back to ``session``, this test catches it.
    """
    import inspect

    from modulo.core.analytics.enforcement_sweep import sweep_schema_enforcement_facts

    sig = inspect.signature(sweep_schema_enforcement_facts)
    params = list(sig.parameters.keys())
    # The first parameter must be named 'factory', not 'session'.
    assert params[0] == "factory", (
        f"sweep_schema_enforcement_facts first param is '{params[0]}', "
        f"expected 'factory' — the production caller passes a session factory"
    )


# ---------------------------------------------------------------------------
# Integration tests — prove the sweep ACTUALLY WORKS through the factory
# ---------------------------------------------------------------------------

_TABLE_NAMES = {"organisations", "runs", "run_daily_facts", "run_node_outputs"}

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_PIPELINE = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SNAPSHOT = uuid.UUID("22222222-2222-2222-2222-222222222222")


_run_counter = 0


def _make_run(*, run_id: uuid.UUID, status: str = "complete") -> Run:
    global _run_counter
    _run_counter += 1
    return Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        status=status,
        run_number=_run_counter,
        input_hash="0" * 64,
        langgraph_thread_id=f"thread-{run_id.hex}",
    )


def _make_enforcement_row(
    *,
    run_id: uuid.UUID,
    node_id: str = "agent-1",
    attempt_key: str = "attempt-0",
    outcome: str = "native_decoded_and_validated",
) -> RunNodeOutput:
    return RunNodeOutput(
        organisation_id=_ORG,
        run_id=run_id,
        node_id=node_id,
        attempt_key=attempt_key,
        outputs_json={"result": "ok"},
        schema_enforcement_json={
            "outcome": outcome,
            "resolved_profile": "provider-strict",
            "native_output": True,
            "repair_attempts": 0,
            "wasted_attempts": 0,
            "validation_errors": [],
            "total_error_count": 0,
            "truncated": False,
        },
    )


def _make_fact_row(*, run_id: uuid.UUID) -> RunDailyFact:
    return RunDailyFact(
        organisation_id=_ORG,
        run_id=run_id,
        run_date=date(2026, 1, 1),
        pipeline_id=_PIPELINE,
        status="complete",
        total_cost_usd=0,
        total_tokens=0,
        trigger_type="manual",
    )


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


class TestSweepSchemaEnforcementFacts:
    """CRITICAL 2: integration tests proving the sweep works through the factory."""

    @pytest.mark.anyio
    async def test_corrects_run_with_null_enforcement_columns(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """A run with NULL enforcement columns and enforcement data in
        run_node_outputs gets corrected.

        FAILS WITHOUT FIX: the sweep crashes with AttributeError on every tick
        because it receives a factory instead of a session.
        """
        from modulo.core.analytics.enforcement_sweep import sweep_schema_enforcement_facts

        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            session.add(_make_fact_row(run_id=run_id))
            session.add(
                _make_enforcement_row(
                    run_id=run_id,
                    outcome="native_decoded_and_validated",
                )
            )
            await session.commit()

        # Verify the fact row starts with NULL enforcement columns.
        async with factory() as session:
            fact = (await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == run_id))).scalar_one()
            assert fact.enforcement_native_count is None

        # Call through the SAME interface the production caller uses (factory).
        result = await sweep_schema_enforcement_facts(factory)
        assert result["scanned"] == 1

        # Verify the EFFECT: the fact row now has non-NULL enforcement columns.
        async with factory() as session:
            fact = (await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == run_id))).scalar_one()
            assert fact.enforcement_native_count is not None, "enforcement_native_count should be non-NULL after sweep"
            assert fact.enforcement_native_count == 1
            assert fact.enforcement_verbatim_count == 0

    @pytest.mark.anyio
    async def test_idempotent_second_tick_does_not_re_correct(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """The sweep is idempotent — a second tick finds no work to do."""
        from modulo.core.analytics.enforcement_sweep import sweep_schema_enforcement_facts

        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            session.add(_make_fact_row(run_id=run_id))
            session.add(_make_enforcement_row(run_id=run_id, outcome="verbatim_passed"))
            await session.commit()

        first = await sweep_schema_enforcement_facts(factory)
        assert first["scanned"] == 1

        # Verify the EFFECT: the fact row now has non-NULL enforcement columns.
        async with factory() as session:
            fact = (await session.execute(select(RunDailyFact).where(RunDailyFact.run_id == run_id))).scalar_one()
            assert fact.enforcement_native_count == 0
            assert fact.enforcement_verbatim_count == 1

        second = await sweep_schema_enforcement_facts(factory)
        assert second["scanned"] == 0
        assert second["corrected"] == 0

    @pytest.mark.anyio
    async def test_excludes_run_without_enforcement_data(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """A run with NULL enforcement columns but NO enforcement data is not scanned."""
        from modulo.core.analytics.enforcement_sweep import sweep_schema_enforcement_facts

        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            session.add(_make_fact_row(run_id=run_id))
            await session.commit()

        result = await sweep_schema_enforcement_facts(factory)
        assert result == {"scanned": 0, "corrected": 0}

    @pytest.mark.anyio
    async def test_multiple_runs_bounded_per_tick(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """The sweep processes at most _SWEEP_MAX_PER_TICK runs per tick."""
        from modulo.core.analytics.enforcement_sweep import (
            _SWEEP_MAX_PER_TICK,
            sweep_schema_enforcement_facts,
        )

        async with factory() as session:
            for _ in range(_SWEEP_MAX_PER_TICK + 5):
                run_id = uuid.uuid4()
                session.add(_make_run(run_id=run_id))
                session.add(_make_fact_row(run_id=run_id))
                session.add(_make_enforcement_row(run_id=run_id))
            await session.commit()

        result = await sweep_schema_enforcement_facts(factory)
        assert result["scanned"] == _SWEEP_MAX_PER_TICK
        # Verify the EFFECT: at least _SWEEP_MAX_PER_TICK fact rows now have
        # non-NULL enforcement columns (we can't easily count exact corrected
        # due to SQLite rowcount limitations, but the scan bound is proven).
        async with factory() as session:
            facts = (await session.execute(select(RunDailyFact))).scalars().all()
            corrected_count = sum(1 for f in facts if f.enforcement_native_count is not None)
            assert corrected_count >= _SWEEP_MAX_PER_TICK

    @pytest.mark.anyio
    async def test_exception_logs_at_error_not_warning(
        self, factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A structural failure (e.g. wrong interface) logs at ERROR level.

        The old code logged at WARNING which silently hid the factory-vs-session
        crash on every tick.
        """
        from unittest.mock import patch

        from modulo.core.analytics.enforcement_sweep import sweep_schema_enforcement_facts

        run_id = uuid.uuid4()
        async with factory() as session:
            session.add(_make_run(run_id=run_id))
            session.add(_make_fact_row(run_id=run_id))
            session.add(_make_enforcement_row(run_id=run_id, outcome="verbatim_passed"))
            await session.commit()

        with patch(
            "modulo.core.analytics.enforcement_sweep.text",
            side_effect=RuntimeError("structural failure"),
        ):
            result = await sweep_schema_enforcement_facts(factory)

        assert result == {"scanned": 0, "corrected": 0}
        # Must be logged at ERROR level (exception), not WARNING.
        assert "analytics.enforcement_sweep_failed" in caplog.text
        assert any(record.levelno >= 40 for record in caplog.records)

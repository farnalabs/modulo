"""Unit tests for the analytics facts enrichment helpers (FAR-102, ADR 020).

``record_run_facts`` is DB-bound, but the derived values it snapshots are
small computations over the run row: UTC day attribution, duration/queue-wait/
final-idle timing math, output-size measurement, and the NULL-safe
graph-dimension derivation. These pin that logic; since FAR-583 the output/
telemetry byte helpers reassemble the payload through the run_node_outputs
repo reader (with the EMPTY fallback to the legacy columns), so their tests
run against a real in-memory SQLite database. The integration suite covers
the write path itself.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.analytics import (
    _derive_graph_dimensions,
    _fact_duration_ms,
    _fact_final_idle_ms,
    _fact_output_bytes,
    _fact_queue_wait_ms,
    _fact_run_date,
    _fact_telemetry_bytes,
    _fact_total_queue_wait_ms,
)
from modulo.db.models.base import Base
from modulo.db.models.run import Run

_TABLE_NAMES = {"organisations", "runs", "run_node_outputs"}


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
async def db_session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session


async def _seed_run(session: AsyncSession, **blob_overrides: object) -> Run:
    values: dict = {
        "organisation_id": uuid.uuid4(),
        "pipeline_id": uuid.uuid4(),
        "snapshot_id": uuid.uuid4(),
        "trigger_type": "manual",
        "run_number": 1,
        "input_hash": "a" * 64,
        "langgraph_thread_id": "t-" + uuid.uuid4().hex,
        "status": "complete",
    }
    values.update(blob_overrides)
    run = Run(**values)
    async with session.begin():
        session.add(run)
        await session.flush()
    return run


def _run(**overrides) -> SimpleNamespace:
    values: dict = {
        "id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "created_at": datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestFactRunDate:
    def test_uses_started_at_utc_date(self) -> None:
        run = _run(started_at=datetime(2026, 8, 7, 23, 30, tzinfo=UTC))
        assert _fact_run_date(run) == date(2026, 8, 7)

    def test_naive_started_at_treated_as_utc(self) -> None:
        run = _run(started_at=datetime(2026, 8, 7, 23, 30))
        assert _fact_run_date(run) == date(2026, 8, 7)

    def test_non_utc_started_at_converts_before_day_attribution(self) -> None:
        # +05:00 2026-08-07T23:30 is 2026-08-07T18:30Z → 2026-08-07. A -05:00
        # 2026-08-07T23:30 is 2026-08-08T04:30Z → 2026-08-08 (crosses the date).
        run = _run(started_at=datetime(2026, 8, 7, 23, 30, tzinfo=timezone(timedelta(hours=5))))
        assert _fact_run_date(run) == date(2026, 8, 7)
        run = _run(started_at=datetime(2026, 8, 7, 23, 30, tzinfo=timezone(timedelta(hours=-5))))
        assert _fact_run_date(run) == date(2026, 8, 8), "a -05:00 offset crossing midnight must attribute to UTC's date"

    def test_missing_started_at_falls_back_to_created_at(self) -> None:
        run = _run(started_at=None, created_at=datetime(2026, 8, 6, 5, 0, tzinfo=UTC))
        assert _fact_run_date(run) == date(2026, 8, 6)

    def test_neither_attributed_to_today(self) -> None:
        run = _run(started_at=None, created_at=None)
        assert _fact_run_date(run) == datetime.now(UTC).date()


class TestFactTimingMs:
    def test_duration_ms(self) -> None:
        run = _run(
            started_at=datetime(2026, 8, 7, 9, 0, 0, tzinfo=UTC),
            completed_at=datetime(2026, 8, 7, 9, 30, 0, tzinfo=UTC),
        )
        assert _fact_duration_ms(run) == 30 * 60 * 1000

    def test_duration_ms_null_when_either_side_missing(self) -> None:
        assert _fact_duration_ms(_run(started_at=None, completed_at=datetime(2026, 8, 7, tzinfo=UTC))) is None
        assert _fact_duration_ms(_run(started_at=datetime(2026, 8, 7, tzinfo=UTC), completed_at=None)) is None

    def test_queue_wait_ms(self) -> None:
        # dispatched_at is stamped BEFORE enqueue, started_at when a worker
        # claims the run — so dispatched < started and the stat is POSITIVE.
        run = _run(
            dispatched_at=datetime(2026, 8, 7, 9, 0, 5, tzinfo=UTC),
            started_at=datetime(2026, 8, 7, 9, 0, 20, tzinfo=UTC),
        )
        assert _fact_queue_wait_ms(run) == 15000

    def test_queue_wait_ms_null_when_either_side_missing(self) -> None:
        assert _fact_queue_wait_ms(_run(dispatched_at=None, started_at=datetime(2026, 8, 7, tzinfo=UTC))) is None
        assert _fact_queue_wait_ms(_run(dispatched_at=datetime(2026, 8, 7, tzinfo=UTC), started_at=None)) is None

    def test_total_queue_wait_ms(self) -> None:
        # FULL queue wait = started_at - created_at (capacity deferral + SAQ
        # queue), unlike queue_wait_ms which is started - dispatched.
        run = _run(
            created_at=datetime(2026, 8, 7, 9, 0, 0, tzinfo=UTC),
            started_at=datetime(2026, 8, 7, 9, 5, 30, tzinfo=UTC),
        )
        assert _fact_total_queue_wait_ms(run) == 330_000

    def test_total_queue_wait_ms_null_when_either_side_missing(self) -> None:
        assert _fact_total_queue_wait_ms(_run(created_at=None, started_at=datetime(2026, 8, 7, tzinfo=UTC))) is None
        assert _fact_total_queue_wait_ms(_run(created_at=datetime(2026, 8, 7, tzinfo=UTC), started_at=None)) is None

    def test_total_queue_wait_ms_null_when_created_at_attribute_absent(self) -> None:
        # The getattr defensive path — a run-shaped object without created_at
        # must degrade to NULL, never raise AttributeError.
        run = SimpleNamespace(
            started_at=datetime(2026, 8, 7, 9, 5, tzinfo=UTC),
        )
        assert _fact_total_queue_wait_ms(run) is None

    def test_final_idle_ms(self) -> None:
        run = _run(
            completed_at=datetime(2026, 8, 7, 11, 0, 0, tzinfo=UTC),
            heartbeat_at=datetime(2026, 8, 7, 10, 59, 0, tzinfo=UTC),
        )
        assert _fact_final_idle_ms(run) == 60000

    def test_final_idle_ms_null_when_heartbeat_missing(self) -> None:
        run = _run(completed_at=datetime(2026, 8, 7, 11, 0, tzinfo=UTC), heartbeat_at=None)
        assert _fact_final_idle_ms(run) is None, "a completed run with no heartbeat leaves the window unknowable"


class TestFactOutputBytes:
    async def test_output_bytes_is_json_dumps_length(self, db_session: AsyncSession) -> None:
        """The payload is the reassembled outputs dict; via the EMPTY fallback
        a run with no new-table rows serves the legacy column — the same
        len(json.dumps(payload, default=str)) bytes as before the re-point."""
        run = await _seed_run(db_session, outputs_json={"node_a": {"result": "ok"}})
        assert await _fact_output_bytes(db_session, run) == len('{"node_a": {"result": "ok"}}')

    async def test_output_bytes_is_pure_return_size_since_p1(self, db_session: AsyncSession) -> None:
        # Since FAR-125 P1 outputs_json holds PURE returns (telemetry excluded),
        # so the fact measures the pure-return size — smaller than the old
        # envelope that carried agent_stdout inline.
        pure_return = {"result": "ok"}
        run = await _seed_run(
            db_session,
            outputs_json=pure_return,
            node_telemetry_json={"agent_stdout": "installing deps...\n"},
        )
        envelope = {**pure_return, "agent_stdout": "installing deps...\n"}
        measured = await _fact_output_bytes(db_session, run)
        assert measured == len(json.dumps(pure_return, default=str))
        assert measured < len(json.dumps(envelope, default=str))

    async def test_none_outputs_returns_none(self, db_session: AsyncSession) -> None:
        run = await _seed_run(db_session)
        assert await _fact_output_bytes(db_session, run) is None

    async def test_read_failure_degrades_to_none(self, db_session: AsyncSession, engine: AsyncEngine) -> None:
        """A reader failure (dead connection) must degrade to NULL with a
        logged warning — the byte helper is best-effort inside the fail-open
        facts writer and must never be what fails a fact write."""
        run = await _seed_run(db_session, outputs_json={"node_a": {"result": "ok"}})
        await engine.dispose()
        assert await _fact_output_bytes(db_session, run) is None


class TestFactTelemetryBytes:
    async def test_telemetry_bytes_is_json_dumps_length(self, db_session: AsyncSession) -> None:
        run = await _seed_run(db_session, node_telemetry_json={"agent_stdout": "installing deps...\n"})
        assert await _fact_telemetry_bytes(db_session, run) == len('{"agent_stdout": "installing deps...\\n"}')

    async def test_none_telemetry_returns_none(self, db_session: AsyncSession) -> None:
        run = await _seed_run(db_session)
        assert await _fact_telemetry_bytes(db_session, run) is None

    async def test_read_failure_degrades_to_none(self, db_session: AsyncSession, engine: AsyncEngine) -> None:
        run = await _seed_run(db_session, node_telemetry_json={"agent_stdout": "x"})
        await engine.dispose()
        assert await _fact_telemetry_bytes(db_session, run) is None


class TestDeriveGraphDimensions:
    def test_non_dict_graph_degrades_to_zeros(self) -> None:
        assert _derive_graph_dimensions("garbage") == (0, 0, None)
        assert _derive_graph_dimensions(None) == (0, 0, None)
        assert _derive_graph_dimensions([]) == (0, 0, None)

    def test_dict_without_nodes_list_degrades_to_zeros(self) -> None:
        assert _derive_graph_dimensions({"nodes": "not-a-list"}) == (0, 0, None)
        assert _derive_graph_dimensions({}) == (0, 0, None)

    def test_counts_nodes_and_sandbox_agents_and_max_timeout(self) -> None:
        graph = {
            "nodes": [
                {"id": "n1", "node_type": "agent", "timeout_seconds": 120},
                {"id": "n2", "node_type": "sandbox_agent", "timeout_seconds": 600},
                {"id": "n3", "node_type": "sandbox_agent", "timeout_seconds": 300},
                {"id": "n4", "node_type": "agent", "timeout_seconds": None},
            ]
        }
        assert _derive_graph_dimensions(graph) == (4, 2, 600)

    def test_non_dict_node_entries_are_skipped(self) -> None:
        graph = {"nodes": [{"id": "n1", "node_type": "agent"}, "not-a-node", 42]}
        assert _derive_graph_dimensions(graph) == (1, 0, None)

    def test_float_timeouts_count_and_round(self) -> None:
        graph = {"nodes": [{"id": "n1", "timeout_seconds": 120.7}, {"id": "n2", "timeout_seconds": 5}]}
        assert _derive_graph_dimensions(graph) == (2, 0, 120)

    def test_bool_timeout_is_skipped(self) -> None:
        # bool is an int subclass — a boolean timeout must never count.
        graph = {"nodes": [{"id": "n1", "timeout_seconds": True}, {"id": "n2", "timeout_seconds": 5}]}
        assert _derive_graph_dimensions(graph) == (2, 0, 5)

    def test_string_timeouts_are_skipped(self) -> None:
        graph = {"nodes": [{"id": "n1", "timeout_seconds": "900"}]}
        assert _derive_graph_dimensions(graph) == (1, 0, None)

    def test_sandbox_agent_count_is_independent_of_timeout_presence(self) -> None:
        graph = {
            "nodes": [
                {"id": "n1", "node_type": "sandbox_agent"},
                {"id": "n2", "node_type": "sandbox_agent", "timeout_seconds": 120},
            ]
        }
        assert _derive_graph_dimensions(graph) == (2, 2, 120)

"""Unit tests for the analytics facts enrichment helpers (FAR-102, ADR 020).

``record_run_facts`` is DB-bound, but the derived values it snapshots are
small computations over the run row: UTC day attribution, duration/queue-wait/
final-idle timing math, output-size measurement, and the NULL-safe
graph-dimension derivation. These pin that logic; since FAR-583 the output/
telemetry byte facts reassemble the payload through the run_node_outputs repo
reader — qa M14 collapsed the two per-side reads into ONE
``_fact_run_blobs`` call (rows fetch + RLS probe + legacy fallback) whose
blobs feed the PURE ``_fact_output_bytes`` / ``_fact_telemetry_bytes``
helpers, so the byte helpers are pinned dict-in/dict-out and the single-read
fallback/failure semantics are pinned against a real in-memory SQLite
database. The integration suite covers the write path itself.
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
    _fact_run_blobs,
    _fact_run_date,
    _fact_telemetry_bytes,
    _fact_total_queue_wait_ms,
)
from modulo.db.crud.run_node_outputs import RunBlobs
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


class TestFactByteHelpers:
    """qa M14: the byte facts are PURE computations over the ONE blobs read —
    the reader's fallback/failure semantics are pinned separately
    (TestFactRunBlobs); here only the dict-to-bytes mapping is."""

    def test_output_bytes_is_json_dumps_length(self) -> None:
        blobs = RunBlobs(outputs={"node_a": {"result": "ok"}}, telemetry=None, markers=None)
        assert _fact_output_bytes(blobs) == len('{"node_a": {"result": "ok"}}')

    def test_output_bytes_is_pure_return_size_since_p1(self) -> None:
        # Since FAR-125 P1 outputs hold PURE returns (telemetry excluded), so
        # the fact measures the pure-return size — smaller than the old
        # envelope that carried agent_stdout inline.
        pure_return = {"result": "ok"}
        blobs = RunBlobs(
            outputs=pure_return,
            telemetry={"agent_stdout": "installing deps...\n"},
            markers=None,
        )
        envelope = {**pure_return, "agent_stdout": "installing deps...\n"}
        measured = _fact_output_bytes(blobs)
        assert measured == len(json.dumps(pure_return, default=str))
        assert measured < len(json.dumps(envelope, default=str))
        assert _fact_telemetry_bytes(blobs) == len(json.dumps({"agent_stdout": "installing deps...\n"}, default=str))

    def test_none_side_returns_none(self) -> None:
        blobs = RunBlobs(outputs=None, telemetry=None, markers=None)
        assert _fact_output_bytes(blobs) is None
        assert _fact_telemetry_bytes(blobs) is None

    def test_failed_read_returns_none_for_both_facts(self) -> None:
        # One read feeds both facts: a read failure (blobs=None) degrades BOTH
        # byte facts to NULL — they share the data source by design.
        assert _fact_output_bytes(None) is None
        assert _fact_telemetry_bytes(None) is None


class TestFactRunBlobs:
    """qa M14: ONE read_run_blobs_with_fallback call serves BOTH byte facts —
    the EMPTY fallback (legacy column when the new table has no rows) and the
    fail-open read-failure degrade are pinned against real SQLite."""

    async def test_legacy_column_served_when_new_table_is_empty(self, db_session: AsyncSession) -> None:
        run = await _seed_run(db_session, outputs_json={"node_a": {"result": "ok"}})
        blobs = await _fact_run_blobs(db_session, run)
        assert blobs is not None
        assert _fact_output_bytes(blobs) == len('{"node_a": {"result": "ok"}}')
        assert _fact_telemetry_bytes(blobs) is None

    async def test_none_outputs_serves_none(self, db_session: AsyncSession) -> None:
        run = await _seed_run(db_session)
        blobs = await _fact_run_blobs(db_session, run)
        assert blobs is not None
        assert blobs.outputs is None
        assert _fact_output_bytes(blobs) is None

    async def test_read_failure_degrades_to_none(self, db_session: AsyncSession, engine: AsyncEngine) -> None:
        """A reader failure (dead connection) must degrade to NULL with a
        logged warning — the blobs read is best-effort inside the fail-open
        facts writer and must never be what fails a fact write."""
        run = await _seed_run(db_session, outputs_json={"node_a": {"result": "ok"}})
        await engine.dispose()
        assert await _fact_run_blobs(db_session, run) is None


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

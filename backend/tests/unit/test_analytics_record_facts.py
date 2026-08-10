"""Unit tests for the analytics facts enrichment helpers (FAR-102, ADR 020).

``record_run_facts`` is DB-bound, but the derived values it snapshots are pure
functions: UTC day attribution, duration/queue-wait/final-idle timing math,
output-size measurement, and the NULL-safe graph-dimension derivation. These
tests pin that logic without a database — the integration suite covers the
write path itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

from modulo.core.analytics import (
    _derive_graph_dimensions,
    _fact_duration_ms,
    _fact_final_idle_ms,
    _fact_output_bytes,
    _fact_queue_wait_ms,
    _fact_run_date,
    _fact_total_queue_wait_ms,
)


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
    def test_output_bytes_is_json_dumps_length(self) -> None:
        run = _run(outputs_json={"node_a": {"result": "ok"}})
        assert _fact_output_bytes(run) == len('{"node_a": {"result": "ok"}}')

    def test_none_outputs_returns_none(self) -> None:
        assert _fact_output_bytes(_run(outputs_json=None)) is None

    def test_non_serialisable_outputs_returns_none(self) -> None:
        run = _run(outputs_json={"node": object()})
        assert _fact_output_bytes(run) is None, "json.dumps failure must degrade to NULL, never raise"


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

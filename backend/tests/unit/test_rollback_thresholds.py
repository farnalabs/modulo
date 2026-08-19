"""Unit tests for rollback_thresholds (FAR-296 Phase 5b) — volume-gated
anomaly detection for script-mode sandbox_agent runs."""

from __future__ import annotations

import uuid
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.rollback_thresholds import (
    _graph_has_script_mode_node,
    _node_config_has_budget,
    evaluate_rollback_thresholds,
)

ORG = uuid.uuid4()


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return _MockBegin()

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        return bind

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        s = str(stmt)
        if "set_config" in s:
            return MagicMock()
        if not self._results:
            return MagicMock()
        return self._results.pop(0)


def _org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _count_result(n: int) -> MagicMock:
    r = MagicMock()
    r.scalar_one.return_value = n
    return r


def _graph_result(graph_jsons: list[Any]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = graph_jsons
    return r


def _row_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _make_graph(*, mode: str = "script") -> dict[str, Any]:
    return {"nodes": [{"node_type": "sandbox_agent", "mode": mode}]}


def _make_graph_no_timeout(*, mode: str = "script") -> dict[str, Any]:
    return {"nodes": [{"node_type": "sandbox_agent", "mode": mode}]}


def _make_graph_with_timeout(*, mode: str = "script") -> dict[str, Any]:
    return {
        "nodes": [
            {
                "node_type": "sandbox_agent",
                "mode": mode,
                "timeout_seconds": 300,
            }
        ]
    }


def _make_classification(*, value: str, work_intact: bool) -> dict[str, Any]:
    return {"value": value, "work_intact": work_intact}


class TestGraphNodeHelpers:
    def test_graph_has_script_mode_node_true(self) -> None:
        assert _graph_has_script_mode_node(_make_graph()) is True

    def test_graph_has_script_mode_node_wrong_mode(self) -> None:
        assert _graph_has_script_mode_node(_make_graph(mode="interactive")) is False

    def test_graph_has_script_mode_node_none(self) -> None:
        assert _graph_has_script_mode_node(None) is False

    def test_graph_has_script_mode_node_empty(self) -> None:
        assert _graph_has_script_mode_node({}) is False

    def test_graph_has_script_mode_node_no_nodes(self) -> None:
        assert _graph_has_script_mode_node({"nodes": []}) is False

    def test_node_config_no_budget(self) -> None:
        assert _node_config_has_budget({"node_type": "sandbox_agent"}) is False

    def test_node_config_with_timeout(self) -> None:
        assert _node_config_has_budget({"timeout_seconds": 300}) is True

    def test_node_config_with_wallclock(self) -> None:
        assert _node_config_has_budget({"wallclock_budget_seconds": 600}) is True

    def test_node_config_none(self) -> None:
        assert _node_config_has_budget(None) is False


class TestEvaluateRollbackThresholds:
    @pytest.mark.asyncio
    async def test_threshold_below_min_runs_skips(self) -> None:
        """Org with 5 terminal runs (< 30 min_runs) -> orgs_checked = 0."""
        factory = MagicMock()
        # Session 1: org selection; Session 2: per-org check
        session_org = _MockSession([_org_result([ORG])])
        session_check = _MockSession(
            [
                _graph_result([_make_graph()] * 5),  # 5 total script runs
            ]
        )
        factory.side_effect = [session_org, session_check]

        result = await evaluate_rollback_thresholds(factory, min_runs=30)
        assert result["orgs_checked"] == 0
        assert result["anomalies_found"] == 0
        assert not result["flagged_orgs"]

    @pytest.mark.asyncio
    async def test_claim_without_marker_detected(self) -> None:
        """Org with 35 terminal script runs, 3 with sandbox_dispatch_state
        set and error_code='script.side_effect_unknown' -> flagged."""
        factory = MagicMock()
        session_org = _MockSession([_org_result([ORG])])
        session_check = _MockSession(
            [
                _graph_result([_make_graph()] * 35),  # 35 total script runs
                _count_result(3),  # claim_without_marker count
                _count_result(0),  # contract_violation count
                _count_result(0),  # unexpected_side_effect count
            ]
        )
        factory.side_effect = [session_org, session_check]

        result = await evaluate_rollback_thresholds(factory, min_runs=30)
        assert result["orgs_checked"] == 1
        assert result["anomalies_found"] == 1
        assert str(ORG) in result["flagged_orgs"]

    @pytest.mark.asyncio
    async def test_contract_violation_delivered_detected(self) -> None:
        """Org with 35 runs, 1 with RunClassification(value='delivered',
        work_intact=False) -> flagged."""
        factory = MagicMock()
        session_org = _MockSession([_org_result([ORG])])
        session_check = _MockSession(
            [
                _graph_result([_make_graph()] * 35),  # 35 total script runs
                _count_result(0),  # claim_without_marker count
                _count_result(1),  # contract_violation count
                _count_result(0),  # unexpected_side_effect count
            ]
        )
        factory.side_effect = [session_org, session_check]

        result = await evaluate_rollback_thresholds(factory, min_runs=30)
        assert result["orgs_checked"] == 1
        assert result["anomalies_found"] == 1
        assert str(ORG) in result["flagged_orgs"]

    @pytest.mark.asyncio
    async def test_no_anomalies_clean_org(self) -> None:
        """Org with 35 terminal runs, all clean -> anomalies_found = 0."""
        factory = MagicMock()
        session_org = _MockSession([_org_result([ORG])])
        session_check = _MockSession(
            [
                _graph_result([_make_graph()] * 35),  # 35 total script runs
                _count_result(0),  # claim_without_marker count
                _count_result(0),  # contract_violation count
                _count_result(0),  # unexpected_side_effect count
            ]
        )
        factory.side_effect = [session_org, session_check]

        result = await evaluate_rollback_thresholds(factory, min_runs=30)
        assert result["orgs_checked"] == 1
        assert result["anomalies_found"] == 0
        assert not result["flagged_orgs"]

    @pytest.mark.asyncio
    async def test_never_raises_on_db_error(self) -> None:
        """Session raises on query -> function returns clean result instead of raising."""
        factory = MagicMock()
        session_org = _MockSession([_org_result([ORG])])
        session_check = _MockSession([])
        session_check.execute = AsyncMock(side_effect=RuntimeError("db error"))
        factory.side_effect = [session_org, session_check]

        result = await evaluate_rollback_thresholds(factory, min_runs=30)
        assert result["orgs_checked"] == 0
        assert result["anomalies_found"] == 0
        assert not result["flagged_orgs"]

    @pytest.mark.asyncio
    async def test_passes_org_ids_directly(self) -> None:
        """When org_ids is provided, skips the org-selection query."""
        factory = MagicMock()
        session_check = _MockSession(
            [
                _graph_result([_make_graph()] * 35),
                _count_result(0),
                _count_result(0),
                _count_result(0),
            ]
        )
        factory.return_value = session_check

        result = await evaluate_rollback_thresholds(factory, org_ids=[ORG], min_runs=30)
        assert result["orgs_checked"] == 1
        assert result["anomalies_found"] == 0

    @pytest.mark.asyncio
    async def test_unexpected_side_effect_unknown_detected(self) -> None:
        """Org with 35 runs, 2 side_effect_unknown with no timeout configured -> flagged."""
        factory = MagicMock()
        session_org = _MockSession([_org_result([ORG])])
        # For the unexpected_side_effect query, return rows with graph_json
        graph_rows = [
            (uuid.uuid4(), _make_graph_no_timeout()),
            (uuid.uuid4(), _make_graph_no_timeout()),
        ]
        session_check = _MockSession(
            [
                _graph_result([_make_graph()] * 35),  # 35 total script runs
                _count_result(0),  # claim_without_marker count
                _count_result(0),  # contract_violation count
                _row_result(graph_rows),  # unexpected_side_effect rows
            ]
        )
        factory.side_effect = [session_org, session_check]

        result = await evaluate_rollback_thresholds(factory, min_runs=30)
        assert result["orgs_checked"] == 1
        assert result["anomalies_found"] == 1
        assert str(ORG) in result["flagged_orgs"]

    @pytest.mark.asyncio
    async def test_side_effect_unknown_with_timeout_not_flagged(self) -> None:
        """side_effect_unknown runs WITH timeout configured are NOT flagged."""
        factory = MagicMock()
        session_org = _MockSession([_org_result([ORG])])
        graph_rows = [
            (uuid.uuid4(), _make_graph_with_timeout()),
        ]
        session_check = _MockSession(
            [
                _graph_result([_make_graph()] * 35),
                _count_result(0),
                _count_result(0),
                _row_result(graph_rows),
            ]
        )
        factory.side_effect = [session_org, session_check]

        result = await evaluate_rollback_thresholds(factory, min_runs=30)
        assert result["orgs_checked"] == 1
        assert result["anomalies_found"] == 0

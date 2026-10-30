"""Additional boundary coverage for eval and pipeline impact helpers."""

from __future__ import annotations

import asyncio
import math
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError

from modulo.core.eval_engine.coverage_gap import (
    CoverageGapSummary,
    _canonical_json,
    _coverage_data_point_run_ids,
    _eval_values_by_variant,
    _load_eval_names,
    _load_runs,
    _variant_key,
    compute_eval_differentiation,
    compute_variant_divergence,
    evaluate_coverage_gap,
)
from modulo.core.eval_engine.execute_suite_run import (
    SuiteJudgingBudgetExhaustedError,
    SuiteRunExecutionError,
    _budget_exhausted,
    _budget_exhausted_result,
    _case_result_row,
    _case_results_async,
    _case_tally,
    _evaluate_within_budget,
    _exhausted_case_results,
    _resolve_judging_budget,
    _resolve_suite_ceiling,
    _sql_error_status,
    _synthesise_output,
    build_suite_run_tuple,
    exclude_eval_families,
    is_eval_trigger,
)
from modulo.core.eval_engine.typed_judge import JudgeErrorCode
from modulo.core.pipeline_engine.errors import (
    NodeMissingModelBackendError,
    RouterNoMatchError,
)
from modulo.core.pipeline_engine.node_runner import ConnectorNodeFailedError
from modulo.core.pipeline_impact import (
    _check_edge_repoint_breaking,
    check_port_change_breaking,
    compute_port_change_impact,
    diff_edge_ports,
    diff_node_ports,
    node_port_signature,
    normalise_edge_port_delta,
)
from modulo.db.models.eval_suite_run import SuiteRunState


class TestCoverageGapBoundaries:
    def test_serialisation_fallback_and_variant_key_fallbacks(self) -> None:
        class _Unserialisable:
            def __str__(self) -> str:
                return "converted"

        assert _canonical_json(_Unserialisable()) == '"converted"'
        assert _variant_key(SimpleNamespace(variant_config_snapshot={"variant_name": 7})) == "7"
        assert not _variant_key(SimpleNamespace(variant_config_snapshot={}))
        assert _variant_key(SimpleNamespace(variant_config_snapshot=[])) is None

    def test_differentiation_filters_none_and_handles_non_finite_input(self) -> None:
        assert compute_eval_differentiation([None, 0.5, 0.5]) == 0.0  # type: ignore[list-item]
        assert compute_variant_divergence([None, {"answer": "x"}]) > 0.0

    def test_data_helpers_drop_nonterminal_and_nonrepresentative_rows(self) -> None:
        first = uuid.uuid4()
        second = uuid.uuid4()
        runs = [
            SimpleNamespace(id=first, status="complete", variant_config_snapshot={"variant_id": "a"}),
            SimpleNamespace(id=second, status="running", variant_config_snapshot={"variant_id": "b"}),
        ]
        eval_id = uuid.uuid4()
        results = [
            SimpleNamespace(run_id=first, eval_id=eval_id, score=0.5, passed=True),
            SimpleNamespace(run_id=second, eval_id=eval_id, score=0.5, passed=True),
            SimpleNamespace(run_id=uuid.uuid4(), eval_id=eval_id, score=0.5, passed=True),
        ]
        points = _coverage_data_point_run_ids(runs, results)
        assert points == {first}
        by_eval = _eval_values_by_variant(results, points, {first: "a"}, {"a": SimpleNamespace(id=first)})
        assert by_eval[eval_id] == {"a": 0.5}

    async def test_eval_name_loader_empty_set_short_circuits(self) -> None:
        session = AsyncMock()
        assert not await _load_eval_names(session, org_id=uuid.uuid4(), eval_ids=set())
        session.execute.assert_not_awaited()

    def test_invalid_thresholds_and_summary_round_trip(self) -> None:
        with pytest.raises(ValueError, match="divergence_threshold"):
            evaluate_coverage_gap([], [], outputs_by_run={}, divergence_threshold=1.1)
        with pytest.raises(ValueError, match="differentiation_threshold"):
            evaluate_coverage_gap([], [], outputs_by_run={}, differentiation_threshold=-0.1)

        summary = CoverageGapSummary(
            status="insufficient_data",
            batch_id=uuid.uuid4(),
            variant_group_id=None,
            run_count=0,
            min_runs=1,
            variant_divergence=0.0,
            divergence_threshold=0.15,
            differentiation_threshold=0.05,
        )
        data = summary.to_dict()
        assert data["batch_id"] == str(summary.batch_id)
        assert data["variant_group_id"] is None
        assert not data["evals"]

    async def test_group_loader_uses_variant_group_query(self) -> None:
        session = AsyncMock()
        row = SimpleNamespace(id=uuid.uuid4())
        scalars = MagicMock()
        scalars.all.return_value = [row]
        result = MagicMock()
        result.scalars.return_value = scalars
        session.execute.return_value = result

        loaded = await _load_runs(session, org_id=uuid.uuid4(), batch_id=None, variant_group_id=uuid.uuid4())
        assert loaded == [row]
        session.execute.assert_awaited_once()


class TestExecuteSuiteRunBoundaries:
    def test_case_helpers_cover_error_and_budget_rows(self) -> None:
        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4())
        definition = SimpleNamespace(
            id=uuid.uuid4(),
            org_id=run.organisation_id,
            node_id=None,
            pipeline_id=None,
            name="eval",
            eval_type="regex",
            config={},
            pass_threshold=None,
            suite_id=None,
        )
        assert _synthesise_output(SimpleNamespace(input_payload={"x": 1}), {"locale": "en"}) == {
            "output": {"x": 1},
            "scenario": {"locale": "en"},
        }
        exhausted = _budget_exhausted_result(definition, run)
        row = _case_result_row(run, definition, exhausted, eval_definition_version=4)
        assert row.outcome == "error"
        assert row.error_code == "judge.timeout"
        rows, count = _exhausted_case_results(run, [definition], eval_definition_version=4)
        assert len(rows) == 1
        assert count == 1
        assert _resolve_suite_ceiling(SimpleNamespace(extra={"suite_ceiling": 1.25}), None) == 1.25
        assert _resolve_suite_ceiling(SimpleNamespace(extra={"suite_ceiling": "2.50"}), None) == 2.5

    async def test_async_case_results_catches_engine_error(self) -> None:
        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4())
        case = SimpleNamespace(input_payload={"category": "billing"})
        definition = SimpleNamespace(
            id=uuid.uuid4(),
            org_id=run.organisation_id,
            node_id=None,
            pipeline_id=None,
            name="eval",
            eval_type="regex",
            config={"pattern": "billing", "field": "output.category"},
            pass_threshold=None,
            suite_id=None,
        )

        class _Engine:
            async def evaluate_result_async(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("judge failed")

        rows, errored = await _case_results_async(
            run,
            case,
            [definition],
            _Engine(),
            None,
            None,
            eval_definition_version=1,
        )
        assert len(rows) == 1
        assert errored == 1
        assert rows[0].error_code == "judge.unavailable"

    async def test_evaluate_within_budget_without_deadline(self) -> None:
        expected = object()

        class _Engine:
            def evaluate_result_async(self, *args: object, **kwargs: object) -> object:
                async def _result() -> object:
                    return expected

                return _result()

        result = await _evaluate_within_budget(_Engine(), {}, object(), SimpleNamespace(id=uuid.uuid4()), None, None)
        assert result is expected

    def test_trigger_and_family_guards_are_total(self) -> None:
        assert is_eval_trigger(SimpleNamespace(run_kind="run", eval_suite_id=uuid.uuid4())) is True
        assert is_eval_trigger(SimpleNamespace()) is False
        assert exclude_eval_families(["", "run_failed", "feedback"]) == {"run_failed"}

    def test_budget_resolution_clamps_invalid_and_nonfinite_values(self) -> None:
        run = SimpleNamespace(extra={"judging_budget_seconds": "not-a-number"})
        assert _resolve_judging_budget(run, None) == 200.0
        assert _resolve_judging_budget(SimpleNamespace(extra={"judging_budget_seconds": math.inf}), None) == 200.0
        assert _resolve_judging_budget(SimpleNamespace(extra={"judging_budget_seconds": 10}), 300.0) == 200.0
        assert _resolve_judging_budget(SimpleNamespace(extra={"judging_budget_seconds": 10}), 0.0) == 200.0

    def test_budget_and_tally_boundaries(self) -> None:
        assert _budget_exhausted(None) is False
        assert _case_tally([], 1) == (0, 0, 1)
        assert _case_tally([SimpleNamespace(passed=True, outcome="passed")], 0) == (1, 0, 0)
        assert _case_tally([SimpleNamespace(passed=False, outcome="failed")], 0) == (0, 1, 0)
        assert _case_tally([SimpleNamespace(passed=True, outcome="error")], 0) == (0, 1, 0)

    def test_sql_error_status_order_is_specific_before_base(self) -> None:
        assert _sql_error_status(IntegrityError("x", {}, Exception())) == 409
        assert _sql_error_status(ProgrammingError("x", {}, Exception())) == 501
        assert _sql_error_status(OperationalError("x", {}, Exception())) == 503
        assert _sql_error_status(SQLAlchemyError("x")) == 503
        assert _sql_error_status(RuntimeError("x")) == 500

    def test_suite_tuple_with_scenario_is_deterministic(self) -> None:
        suite_id = uuid.uuid4()
        dataset_id = uuid.uuid4()
        backend_id = uuid.uuid4()
        definition_id = uuid.uuid4()
        baseline, checksum, signature = build_suite_run_tuple(
            suite_id=suite_id,
            dataset_id=dataset_id,
            dataset_version=2,
            definition_snapshots=[{"id": str(definition_id), "eval_type": "regex", "config_json": {}}],
            model_backend_id=backend_id,
            scenario_inputs={"locale": "en-GB"},
        )
        assert baseline["dataset_version"] == 2
        assert checksum
        assert signature

    async def test_async_budget_exhaustion_exception_is_catchable(self) -> None:
        with pytest.raises(SuiteJudgingBudgetExhaustedError):
            raise SuiteJudgingBudgetExhaustedError
        await asyncio.sleep(0)


class TestPipelineImpactBoundaries:
    def test_port_shapes_and_invalid_entries_are_normalised(self) -> None:
        node = {"id": "n", "inputs": {"in": "schema"}, "outputs": ["out", {"name": "bad"}, {}]}
        assert node_port_signature(node) == {"input": {"in": "schema"}, "output": {"out": None, "bad": None}}
        assert node_port_signature({"id": "n", "inputs": "invalid", "outputs": None}) == {
            "input": {},
            "output": {},
        }
        assert diff_node_ports({"id": "n", "inputs": [{"name": "gone", "schema_id": "a"}]}, {"id": "n"})

    def test_edge_repoint_and_legacy_endpoint_shapes(self) -> None:
        edge = {"source_node_id": "a", "target_node_id": "b"}
        delta = diff_edge_ports({"source_port": "old"}, {"source_port": "new", "target_port": "in"})
        changes = normalise_edge_port_delta(edge, delta)
        assert len(changes) == 2
        assert changes[0]["node_id"] == "a"
        assert changes[1]["node_id"] == "b"

    def test_impact_skips_malformed_edges_and_cycles(self) -> None:
        graph = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [{"source": "a", "target": "b"}, {"source": "b"}, {"target": "a"}, {"source": "a", "target": "a"}],
        }
        assert compute_port_change_impact(graph, [{"node_id": "a"}, None, ""]) == {"a", "b"}

    def test_repoint_breaking_checks_missing_and_changed_ports(self) -> None:
        graph = {
            "nodes": [{"id": "a", "outputs": [{"name": "new", "schema_ref": "s2"}]}],
            "edges": [{"source": "a", "target": "b", "source_port": "new"}],
        }
        missing = check_port_change_breaking(
            graph,
            [
                {
                    "node_id": "a",
                    "direction": "output",
                    "port": "missing",
                    "change": "edge_source_port_repoint",
                    "old": "old",
                    "new": "missing",
                    "edge": {"source": "a", "target": "b"},
                }
            ],
        )
        assert missing[0]["severity"] == "block"
        assert not check_port_change_breaking(graph, [{"node_id": "unknown", "direction": "output", "port": "x"}])
        assert not _check_edge_repoint_breaking({"node_id": "a", "direction": "sideways"}, graph)
        warning = _check_edge_repoint_breaking(
            {
                "node_id": "a",
                "direction": "output",
                "port": "new",
                "old": "old",
                "edge": {"source": "a", "target": "b"},
            },
            {
                "nodes": [
                    {
                        "id": "a",
                        "outputs": [
                            {"name": "old", "schema_ref": "s1"},
                            {"name": "new", "schema_ref": "s2"},
                        ],
                    }
                ]
            },
        )
        assert warning[0]["severity"] == "warning"


class TestPipelineEngineErrors:
    def test_error_messages_and_payload_fields(self) -> None:
        router = RouterNoMatchError()
        assert str(router) == "Router node found no matching rule and no default target"
        detailed = RouterNoMatchError("router", "classifier returned no route")
        assert detailed.node_id == "router"
        assert "classifier returned no route" in str(detailed)

        missing = NodeMissingModelBackendError("node", "agent")
        assert missing.node_id == "node"
        assert missing.agent_id == "agent"
        connector = ConnectorNodeFailedError(
            node_id="connector",
            envelope={"connector_failure": True},
            cause=RuntimeError("upstream rejected request"),
        )
        assert connector.node_id == "connector"
        assert connector.envelope == {"connector_failure": True}
        assert "upstream rejected request" in str(connector)


class TestCoverageGapRemainingBranches:
    def test_canonical_json_falls_back_when_encoder_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise_type_error(*args: object, **kwargs: object) -> str:
            raise TypeError("bad")

        monkeypatch.setattr("modulo.core.eval_engine.coverage_gap.json.dumps", _raise_type_error)
        assert _canonical_json({"value": 1}) == "{'value': 1}"

    def test_duplicate_variant_and_incomplete_results_are_ignored(self) -> None:
        from modulo.core.eval_engine.coverage_gap import _eval_values_by_variant, _variant_representatives

        first = uuid.uuid4()
        duplicate = uuid.uuid4()
        missing_variant = uuid.uuid4()
        eval_id = uuid.uuid4()
        runs = [
            SimpleNamespace(id=first, variant_config_snapshot={"variant_id": "v"}),
            SimpleNamespace(id=duplicate, variant_config_snapshot={"variant_id": "v"}),
            SimpleNamespace(id=missing_variant, variant_config_snapshot={"variant_id": "other"}),
        ]
        representatives, keys = _variant_representatives(runs, {first, duplicate})
        assert list(representatives) == ["v"]
        results = [
            SimpleNamespace(run_id=duplicate, eval_id=eval_id, score=0.1, passed=False),
            SimpleNamespace(run_id=first, eval_id=None, score=0.9, passed=True),
            SimpleNamespace(run_id=uuid.uuid4(), eval_id=eval_id, score=0.5, passed=True),
        ]
        assert not _eval_values_by_variant(results, {first, duplicate}, keys, representatives)

    async def test_compute_coverage_gap_requires_scope_and_handles_empty_batch(self) -> None:
        from modulo.core.eval_engine.coverage_gap import compute_coverage_gap

        session = AsyncMock()
        with pytest.raises(ValueError, match="provide batch_id or variant_group_id"):
            await compute_coverage_gap(session, org_id=uuid.uuid4())

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(
                "modulo.core.eval_engine.coverage_gap._load_runs",
                AsyncMock(return_value=[]),
            )
            summary = await compute_coverage_gap(session, org_id=uuid.uuid4(), batch_id=uuid.uuid4())
        assert summary.status == "insufficient_data"
        assert summary.run_count == 0

    async def test_batch_loader_and_output_reader_are_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modulo.core.eval_engine import coverage_gap

        run = SimpleNamespace(id=uuid.uuid4(), status="complete", variant_config_snapshot={"variant_id": "v"})
        batch_loader = AsyncMock(return_value=[run])
        monkeypatch.setattr(coverage_gap, "get_batch_runs", batch_loader)
        session = AsyncMock()
        loaded = await coverage_gap._load_runs(
            session,
            org_id=uuid.uuid4(),
            batch_id=uuid.uuid4(),
            variant_group_id=None,
        )
        assert loaded == [run]
        batch_loader.assert_awaited_once()


class TestExecuteSuiteRunRemainingBranches:
    async def test_transition_rejects_unpersisted_and_illegal_runs(self) -> None:
        from modulo.core.eval_engine.execute_suite_run import _suite_run_transition
        from modulo.db.models.eval_suite_run import IllegalStateTransitionError

        session = AsyncMock()
        with pytest.raises(ValueError, match="persisted"):
            await _suite_run_transition(session, SimpleNamespace(id=None), SuiteRunState.RUNNING)
        run = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
            state=SuiteRunState.COMPLETED.value,
            version=0,
        )
        with pytest.raises(IllegalStateTransitionError):
            await _suite_run_transition(session, run, SuiteRunState.RUNNING)

    async def test_transition_detects_optimistic_lock_loss(self) -> None:
        from modulo.core.eval_engine.execute_suite_run import _suite_run_transition
        from modulo.db.models.eval_suite_run import OptimisticLockError

        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session = AsyncMock()
        session.execute.return_value = result
        run = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
            state=SuiteRunState.PENDING.value,
            version=0,
        )
        with pytest.raises(OptimisticLockError):
            await _suite_run_transition(session, run, SuiteRunState.RUNNING)

    async def test_loader_not_found_errors_are_typed(self) -> None:
        from modulo.core.eval_engine.execute_suite_run import load_dataset, load_model_backend, load_suite

        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session = AsyncMock()
        session.execute.return_value = result
        org_id = uuid.uuid4()
        with pytest.raises(SuiteRunExecutionError, match="EvalSuite"):
            await load_suite(session, org_id, uuid.uuid4())
        with pytest.raises(SuiteRunExecutionError, match="EvalDataset"):
            await load_dataset(session, org_id, uuid.uuid4())
        with pytest.raises(SuiteRunExecutionError, match="ModelBackend"):
            await load_model_backend(session, org_id, uuid.uuid4())

    async def test_deadline_timeout_and_budget_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = SimpleNamespace(id=uuid.uuid4())
        definition = SimpleNamespace(id=uuid.uuid4(), node_id=None)

        class _Engine:
            def evaluate_result_async(self, *args: object, **kwargs: object) -> object:
                async def _result() -> object:
                    return object()

                return _result()

        monkeypatch.setattr("modulo.core.eval_engine.execute_suite_run.time.monotonic", lambda: 10.0)
        with pytest.raises(SuiteJudgingBudgetExhaustedError):
            await _evaluate_within_budget(_Engine(), {}, definition, run, None, 9.0)

        async def _raise_timeout(awaitable: object, **kwargs: object) -> object:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError

        monkeypatch.setattr("modulo.core.eval_engine.execute_suite_run.time.monotonic", lambda: 0.0)
        monkeypatch.setattr("modulo.core.eval_engine.execute_suite_run.asyncio.wait_for", _raise_timeout)
        with pytest.raises(SuiteJudgingBudgetExhaustedError):
            await _evaluate_within_budget(_Engine(), {}, definition, run, None, 1.0)
        exhausted = _budget_exhausted_result(definition, run)
        assert exhausted.judge_verdict.error_code is JudgeErrorCode.JUDGE_TIMEOUT

    async def test_claimed_cost_and_sync_judge_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modulo.core.eval_engine.execute_suite_run import _score_case

        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4(), claimed_cost=None)
        case = SimpleNamespace(input_payload={"x": 1})
        definition = SimpleNamespace(
            id=uuid.uuid4(),
            org_id=run.organisation_id,
            node_id=None,
            pipeline_id=None,
            name="regex",
            eval_type="regex",
            config={"pattern": "1", "field": "output.x"},
            pass_threshold=None,
            suite_id=None,
        )
        session = AsyncMock()
        claimed = AsyncMock()
        monkeypatch.setattr("modulo.core.eval_engine.execute_suite_run._claim_case_cost", claimed)
        results, errored = await _score_case(
            session,
            run,
            case,
            suite_defs=[definition],
            engine=type(
                "E",
                (),
                {
                    "evaluate_result": lambda self, *args, **kwargs: SimpleNamespace(
                        outcome=type("O", (), {"value": "passed"})(),
                        passed=True,
                        score=1.0,
                        detail="ok",
                        judge_verdict=None,
                    )
                },
            )(),
            model_judge_callable=object(),
            scenario_inputs=None,
            eval_definition_version=1,
            claims_llm=True,
            suite_ceiling=Decimal(1),
            cost_per_llm_case=Decimal("0.1"),
            judging_deadline=None,
        )
        assert len(results) == 1
        assert errored == 0
        claimed.assert_awaited_once()

    def test_resolvers_and_tally_cover_remaining_boundaries(self) -> None:
        from modulo.core.eval_engine.execute_suite_run import _failure_detail, _resolve_suite_ceiling

        assert _resolve_suite_ceiling(SimpleNamespace(extra={"suite_ceiling": 2}), None) == Decimal(2)
        assert _resolve_suite_ceiling(SimpleNamespace(extra={"suite_ceiling": None}), Decimal(3)) == Decimal(3)
        assert _failure_detail(SuiteRunExecutionError("typed")) == "typed"
        assert _failure_detail(RuntimeError("raw")) == "500: raw"


class TestPipelineImpactRemainingBranches:
    def test_schema_refs_and_port_delta_edges(self) -> None:
        from modulo.core.pipeline_impact import _normalise_port_entry, _port_schema_ref, _ports_list

        assert _port_schema_ref({"name": "p", "ref": 4}) == "4"
        assert _port_schema_ref({"name": "p"}) is None
        assert not _ports_list({"p": 4})
        assert _normalise_port_entry({"direction": "output"}) is None
        assert _normalise_port_entry(("node", "ignored")) == ("node", "", None, None)

    def test_diff_and_impact_cover_modified_ports_and_duplicate_nodes(self) -> None:
        old = {
            "id": "n",
            "inputs": [{"name": "in", "schema_ref": "a"}],
            "outputs": [{"name": "out", "schema_ref": "a"}],
        }
        new = {
            "id": "n",
            "inputs": [{"name": "in", "schema_ref": "b"}],
            "outputs": [{"name": "out", "schema_ref": "a"}, {"name": "extra"}],
        }
        changes = diff_node_ports(old, new)
        assert {c["change"] for c in changes} == {"modified", "added"}
        graph = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [{"source": "a", "target": "b"}],
        }
        assert compute_port_change_impact(graph, [{"node_id": "a"}, {"node_id": "a"}]) == {"a", "b"}

    def test_port_breaking_checks_removed_modified_and_default_edges(self) -> None:
        graph = {
            "nodes": [{"id": "a", "outputs": [{"name": "kept", "schema_ref": "new"}]}],
            "edges": [
                {"source": "a", "target": "b", "source_port": "removed"},
                {"source": "a", "target": "c"},
                {"source": "a", "target": "d", "source_port": "kept"},
            ],
        }
        findings = check_port_change_breaking(
            graph,
            [
                {"node_id": "a", "direction": "output", "port": "removed", "change": "removed"},
                {"node_id": "a", "direction": "output", "port": "kept", "change": "modified"},
            ],
        )
        assert any(f["severity"] == "block" for f in findings)
        assert any(f["severity"] == "warning" for f in findings)
        assert not _check_edge_repoint_breaking(
            {"node_id": "missing", "direction": "output", "port": "x", "edge": {}}, graph
        )

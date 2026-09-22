"""Chunk-3 read-cutover acceptance tests (FAR-1100, spec §8 criteria 11/12/13/18).

Pure-unit coverage of the executor's eval-definition read path after the
cutover to ``evals`` + ``policy_gates``:

* **Criterion 11** — ``_load_eval_defs_for_pipeline`` issues its SELECT against
  the ``evals`` table (joined to ``policy_gates``), NOT ``eval_definitions``.
* **Criterion 12** — ``_build_eval_defs_by_node`` constructs the
  ``EvalDefDTO`` from an ``(Eval, PolicyGate)`` row pair: ``failure_behaviour``
  from ``PolicyGate.action``, ``eval_type`` from ``Eval.eval_type``, ``config``
  from ``Eval.config_json``.
* **Criterion 13** — ``EvalEngine.evaluate()`` honours the policy action
  carried on the DTO: ``block`` raises ``EvalBlockedError`` on failure,
  ``warn`` does not.
* **Criterion 18** — the backfill violation-inventory mechanism is exercised
  via ``validate_binding``: candidate rows passing the migration's filter
  (live, node-scoped, non-guardrail) validate clean, and each excluded shape
  raises with the expected exclusion name (collected, never short-circuited).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from modulo.core.eval_engine import EvalBlockedError, EvalEngine
from modulo.core.eval_engine.policy_gate import PolicyGateBindingViolationError, validate_binding
from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO
from modulo.db.models.eval import Eval
from modulo.db.models.policy_gate import PolicyGate

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_eval_row(
    *,
    eval_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    name: str = "node-eval",
    eval_type: str = "regex",
    config_json: dict | None = None,
    pass_threshold: float | None = None,
    suite_id: str | None = None,
    version: int = 1,
    pipeline_id: uuid.UUID | None = None,
) -> MagicMock:
    """A mock ``Eval`` ORM row exposing the attributes the executor reads."""
    row = MagicMock()
    row.id = eval_id or uuid4()
    row.node_id = node_id
    row.name = name
    row.eval_type = eval_type
    row.config_json = config_json if config_json is not None else {"pattern": "x", "field": "out"}
    row.pass_threshold = pass_threshold
    row.suite_id = suite_id
    row.version = version
    row.pipeline_id = pipeline_id or uuid4()
    row.organisation_id = uuid4()
    return row


def _make_policy_gate_row(*, action: str = "warn", node_id: uuid.UUID | None = None) -> MagicMock:
    """A mock ``PolicyGate`` ORM row exposing the attributes the executor reads."""
    gate = MagicMock()
    gate.action = action
    gate.node_id = node_id or uuid4()
    gate.deleted_at = None
    return gate


# ---------------------------------------------------------------------------
# Criterion 11 — the loader reads evals + policy_gates, not eval_definitions
# ---------------------------------------------------------------------------


class TestLoadEvalDefsReadsEvals:
    async def test_loader_selects_from_evals_and_policy_gates(self) -> None:
        """The loading query's FROM clause is ``evals LEFT OUTER JOIN
        policy_gates`` — ``eval_definitions`` must not appear anywhere in the
        compiled statement (that table is dead data after the cutover)."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        executor = PipelineExecutor(MagicMock())
        await executor._load_eval_defs_for_pipeline(session, uuid4())

        stmt = session.execute.call_args[0][0]
        compiled = str(stmt)
        assert "evals" in compiled, f"loading query must read from evals, got: {compiled}"
        assert "policy_gates" in compiled, f"loading query must outer-join policy_gates, got: {compiled}"
        assert "eval_definitions" not in compiled, (
            f"loading query must NOT reference eval_definitions after the cutover, got: {compiled}"
        )

    async def test_loader_selects_eval_and_policy_gate_entities(self) -> None:
        """The SELECT projects ``(Eval, PolicyGate)`` entity pairs — the row
        shape ``_build_eval_defs_by_node`` consumes."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        executor = PipelineExecutor(MagicMock())
        await executor._load_eval_defs_for_pipeline(session, uuid4())

        stmt: select = session.execute.call_args[0][0]
        selected_types = {d["type"] for d in stmt.column_descriptions}
        assert selected_types == {Eval, PolicyGate}

    async def test_loader_filters_node_scoped_live_evals(self) -> None:
        """The WHERE clause keeps the live node-scoped filters: node_id
        IS NOT NULL and deleted_at IS NULL (soft-deleted Evals never load)."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        executor = PipelineExecutor(MagicMock())
        await executor._load_eval_defs_for_pipeline(session, uuid4())

        stmt = session.execute.call_args[0][0]
        where_sql = str(stmt.whereclause) if stmt.whereclause is not None else ""
        assert "node_id" in where_sql, f"loading query must filter on node_id, got: {where_sql}"
        assert "IS NOT NULL" in where_sql, f"loading query must filter node_id IS NOT NULL, got: {where_sql}"
        assert "deleted_at" in where_sql, f"loading query must filter deleted_at IS NULL, got: {where_sql}"

    async def test_loader_returns_row_pairs_unchanged(self) -> None:
        """The loader returns the raw ``(Eval, PolicyGate | None)`` tuples —
        the gate is None when no live gate exists (outer join)."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        eval_row = _make_eval_row(node_id=uuid4())
        gate_row = _make_policy_gate_row(action="block")
        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = [(eval_row, gate_row)]
        session.execute = AsyncMock(return_value=result)

        executor = PipelineExecutor(MagicMock())
        rows = await executor._load_eval_defs_for_pipeline(session, uuid4())

        assert rows == [(eval_row, gate_row)]


# ---------------------------------------------------------------------------
# Criterion 12 — DTO built from Eval + PolicyGate
# ---------------------------------------------------------------------------


class TestBuildEvalDefsByNodeFromEvalAndGate:
    def test_failure_behaviour_comes_from_policy_gate_action(self) -> None:
        """Criterion 12: ``failure_behaviour`` on the DTO is populated from
        ``PolicyGate.action`` — a ``block`` gate yields a ``block`` DTO."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        node_id = uuid4()
        rows = [(_make_eval_row(node_id=node_id), _make_policy_gate_row(action="block"))]

        by_node = PipelineExecutor._build_eval_defs_by_node(rows, uuid4(), uuid4())

        assert by_node[str(node_id)][0].failure_behaviour == "block"

    def test_eval_type_and_config_come_from_eval_row(self) -> None:
        """Criterion 12: ``eval_type`` comes from ``Eval.eval_type`` and
        ``config`` from ``Eval.config_json``."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        node_id = uuid4()
        eval_id = uuid4()
        config = {"pattern": "approved", "field": "summary"}
        rows = [
            (
                _make_eval_row(eval_id=eval_id, node_id=node_id, eval_type="regex", config_json=config),
                _make_policy_gate_row(action="warn"),
            )
        ]

        by_node = PipelineExecutor._build_eval_defs_by_node(rows, uuid4(), uuid4())

        dto = by_node[str(node_id)][0]
        assert dto.eval_type == "regex"
        assert dto.config == config
        assert dto.id == eval_id
        assert dto.node_id == str(node_id)

    def test_gate_none_guardrail_eval_defaults_to_warn(self) -> None:
        """A guardrail-typed Eval has no PolicyGate by design — the DTO keeps
        warn semantics (guardrail block behaviour is guardrail-owned)."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        node_id = uuid4()
        rows = [(_make_eval_row(node_id=node_id, eval_type="guardrail"), None)]

        by_node = PipelineExecutor._build_eval_defs_by_node(rows, uuid4(), uuid4())

        assert by_node[str(node_id)][0].failure_behaviour == "warn"

    def test_gate_none_non_guardrail_eval_downgrades_to_warn(self) -> None:
        """A node-scoped non-guardrail Eval without a gate (backfill binding
        rejected) degrades to warn — never crashes, never blocks."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        node_id = uuid4()
        rows = [(_make_eval_row(node_id=node_id, eval_type="regex"), None)]

        by_node = PipelineExecutor._build_eval_defs_by_node(rows, uuid4(), uuid4())

        assert by_node[str(node_id)][0].failure_behaviour == "warn"

    def test_eval_scoped_to_correct_node_key(self) -> None:
        """The DTO lands under the Eval's node id key — gate nodes look evals
        up by the graph node id string."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        node_a = uuid4()
        node_b = uuid4()
        rows = [
            (_make_eval_row(node_id=node_a, name="a-eval"), _make_policy_gate_row()),
            (_make_eval_row(node_id=node_b, name="b-eval"), _make_policy_gate_row()),
        ]

        by_node = PipelineExecutor._build_eval_defs_by_node(rows, uuid4(), uuid4())

        assert sorted(by_node.keys()) == sorted([str(node_a), str(node_b)])
        assert by_node[str(node_b)][0].name == "b-eval"


# ---------------------------------------------------------------------------
# Criterion 13 — EvalEngine honours the policy action carried on the DTO
# ---------------------------------------------------------------------------

_FAILING_OUTPUT = {"greeting": "Hello, World!"}
_FAILING_CONFIG = {"pattern": "zzz-never-matches", "field": "greeting"}


class TestEvalEngineHonoursPolicyAction:
    def test_block_action_raises_eval_blocked_error(self) -> None:
        """Criterion 13: a DTO carrying ``failure_behaviour='block'`` (sourced
        from ``PolicyGate.action``) makes ``evaluate()`` raise
        ``EvalBlockedError`` when the eval fails."""
        dto = EvalDefDTO(
            id=uuid4(),
            org_id=uuid4(),
            node_id="n1",
            name="block-eval",
            eval_type="regex",
            config=_FAILING_CONFIG,
            failure_behaviour="block",
        )

        with pytest.raises(EvalBlockedError):
            EvalEngine().evaluate(_FAILING_OUTPUT, dto)

    def test_warn_action_does_not_raise(self) -> None:
        """Criterion 13: the same failing eval with ``failure_behaviour='warn'``
        returns the result instead of raising."""
        dto = EvalDefDTO(
            id=uuid4(),
            org_id=uuid4(),
            node_id="n1",
            name="warn-eval",
            eval_type="regex",
            config=_FAILING_CONFIG,
            failure_behaviour="warn",
        )

        result = EvalEngine().evaluate(_FAILING_OUTPUT, dto)

        assert result.passed is False
        assert result.eval_id == dto.id

    def test_block_action_passing_eval_does_not_raise(self) -> None:
        """A ``block`` eval whose regex matches passes silently — block only
        fires on failure."""
        dto = EvalDefDTO(
            id=uuid4(),
            org_id=uuid4(),
            node_id="n1",
            name="block-eval-pass",
            eval_type="regex",
            config={"pattern": "Hello", "field": "greeting"},
            failure_behaviour="block",
        )

        result = EvalEngine().evaluate(_FAILING_OUTPUT, dto)

        assert result.passed is True


# ---------------------------------------------------------------------------
# Criterion 18 — violation inventory mechanism (validate_binding contract)
# ---------------------------------------------------------------------------


class TestViolationInventoryMechanism:
    @staticmethod
    def _candidate_fields() -> tuple[dict, dict]:
        """PolicyGate/Eval field maps matching the migration's candidate filter
        (live, node-scoped, non-guardrail, same org, same node)."""
        org_id = uuid4()
        node_id = uuid4()
        eval_id = uuid4()
        policy_gate_fields = {
            "id": uuid4(),
            "organisation_id": org_id,
            "node_id": node_id,
        }
        eval_fields = {
            "id": eval_id,
            "organisation_id": org_id,
            "node_id": node_id,
            "eval_type": "regex",
            "deleted_at": None,
        }
        return policy_gate_fields, eval_fields

    def test_filtered_candidate_rows_pass_validation(self) -> None:
        """Criterion 18 (filter-first contract): every row passing the
        migration's candidate filter validates clean — the recorded inventory
        for a well-formed backfill is empty."""
        policy_gate_fields, eval_fields = self._candidate_fields()

        outcome = validate_binding(policy_gate_fields, eval_fields)

        assert outcome is None

    def test_guardrail_typed_eval_violates(self) -> None:
        """A guardrail-typed eval can never carry a PolicyGate — the
        ``guardrail_eval`` exclusion is what the inventory would record."""
        policy_gate_fields, eval_fields = self._candidate_fields()
        eval_fields["eval_type"] = "guardrail"

        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(policy_gate_fields, eval_fields)

        exclusions = [v["exclusion"] for v in exc_info.value.violations]
        assert exclusions == ["guardrail_eval"]

    def test_suite_scoped_eval_violates(self) -> None:
        """A suite-scoped eval (node_id NULL) can never carry a PolicyGate."""
        policy_gate_fields, eval_fields = self._candidate_fields()
        eval_fields["node_id"] = None

        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(policy_gate_fields, eval_fields)

        exclusions = [v["exclusion"] for v in exc_info.value.violations]
        assert exclusions == ["suite_scoped_eval"]

    def test_cross_tenancy_violates(self) -> None:
        """A gate and eval from different organisations must never bind."""
        policy_gate_fields, eval_fields = self._candidate_fields()
        policy_gate_fields["organisation_id"] = uuid4()

        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(policy_gate_fields, eval_fields)

        exclusions = [v["exclusion"] for v in exc_info.value.violations]
        assert exclusions == ["cross_tenancy"]

    def test_node_mismatch_violates(self) -> None:
        """A gate bound to a different node than the eval violates the
        node-consistency exclusion."""
        policy_gate_fields, eval_fields = self._candidate_fields()
        policy_gate_fields["node_id"] = uuid4()

        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(policy_gate_fields, eval_fields)

        exclusions = [v["exclusion"] for v in exc_info.value.violations]
        assert exclusions == ["node_id_mismatch"]

    def test_all_violations_collected_without_short_circuit(self) -> None:
        """validate_binding collects EVERY violated exclusion in one raise —
        the inventory records the full rejection reason, not the first."""
        policy_gate_fields, eval_fields = self._candidate_fields()
        policy_gate_fields["organisation_id"] = uuid4()
        policy_gate_fields["node_id"] = uuid4()

        with pytest.raises(PolicyGateBindingViolationError) as exc_info:
            validate_binding(policy_gate_fields, eval_fields)

        exclusions = {v["exclusion"] for v in exc_info.value.violations}
        assert exclusions == {"cross_tenancy", "node_id_mismatch"}

"""Pure-unit tests for FAR-1102 chunk 4 decision-record fail-open persistence.

Covers acceptance criteria 4-7 from the chunk-04 decision-records spec:
  4. Persistence failure does not raise out of the wrapper.
  5. Persistence failure logs a structured event with failure_class
     (transient→WARNING, referential→ERROR with constraint_name + eval_id).
  6. A persistence failure does not block a ``continue`` outcome.
  7. ``CancelledError`` propagates (not swallowed by the wrapper).

Target: backend/tests/unit/core/test_eval_persist_order_failopen.py
All tests use mock sessions — no database required.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from modulo.core.eval_engine.policy_gate import (
    EvalPolicySnapshot,
    EvalResultView,
    EvalView,
    Outcome,
    PolicyGateView,
    resolve_policy_gate,
)
from modulo.core.pipeline_engine import eval_persist_order
from modulo.core.pipeline_engine.eval_persist_order import (
    EvalDefDTO,
    _classify_persistence_failure,
    _persist_decision_row,
    _record_decision_persist_failure,
    run_evals_persist_before_decide,
)

run_id = uuid.uuid4()
org_id = uuid.uuid4()
gate_id = uuid.uuid4()

_MODULE_LOGGER = "modulo.core.pipeline_engine.eval_persist_order"


def _snapshot(with_result: bool = True, *, matching_node: bool = True) -> EvalPolicySnapshot:
    node_id = uuid.uuid4()
    return EvalPolicySnapshot(
        policy_gate=PolicyGateView(
            id=gate_id,
            organisation_id=org_id,
            version=1,
            node_id=node_id,
            action="continue",
        ),
        eval=EvalView(
            id=uuid.uuid4(),
            organisation_id=org_id,
            node_id=node_id if matching_node else None,
            eval_type="regex",
            deleted_at=None,
        ),
        eval_result=EvalResultView(id=uuid.uuid4(), passed=True) if with_result else None,
    )


def _outcome(action: str = "continue") -> Outcome:
    return Outcome(
        result=True,
        action=action,
        eval_result_id=uuid.uuid4(),
        error=None,
    )


def _cm(raise_on_enter: BaseException | None = None) -> MagicMock:
    cm = MagicMock()
    if raise_on_enter is not None:
        cm.__aenter__ = MagicMock(side_effect=raise_on_enter)
    else:
        cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _session_factory(session: MagicMock):
    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


def _failing_savepoint_session(failure: BaseException) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=_cm(raise_on_enter=failure))
    return session


def _ok_session() -> MagicMock:
    """A session whose transactions/savepoints all enter and exit cleanly."""
    session = MagicMock()
    session.execute = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=_cm())
    session.begin_nested = MagicMock(return_value=_cm())
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


class TestClassifyPersistenceFailure:
    """``_classify_persistence_failure`` separates referential from transient."""

    def test_exact_constraint_is_referential(self) -> None:
        exc = IntegrityError("stmt", {}, Exception("fk violation"))
        exc.constraint_name = "fk_policy_gate_decisions_gate_org"
        assert _classify_persistence_failure(exc) == "referential"

    def test_substring_match_is_referential(self) -> None:
        # A driver that reports the name in a different case/spelling but still
        # names the policy_gate_decisions table.
        exc = IntegrityError("stmt", {}, Exception("fk violation"))
        exc.constraint_name = "RS_POLICY_GATE_DECISIONS_EVAL_ORG"
        assert _classify_persistence_failure(exc) == "referential"

    def test_unrelated_constraint_is_transient(self) -> None:
        exc = IntegrityError("stmt", {}, Exception("fk violation"))
        exc.constraint_name = "fk_eval_results_run_org"
        assert _classify_persistence_failure(exc) == "transient"

    def test_non_integrity_error_is_transient(self) -> None:
        assert _classify_persistence_failure(RuntimeError("connection reset")) == "transient"


class TestRecordDecisionPersistFailureMetrics:
    """``_record_decision_persist_failure`` is best-effort and never raises."""

    def test_counter_incremented_when_available(self) -> None:
        counter = MagicMock()
        with patch.object(eval_persist_order, "_decision_record_persist_failures_total", counter):
            _record_decision_persist_failure(resolved_action="continue", failure_class="transient")

        counter.add.assert_called_once_with(
            1,
            {"resolved_action": "continue", "failure_class": "transient"},
        )

    def test_no_counter_increment_when_unavailable(self) -> None:
        ensure_metrics = MagicMock()
        with (
            patch.object(eval_persist_order, "_ensure_metrics", ensure_metrics),
            patch.object(eval_persist_order, "_decision_record_persist_failures_total", None),
        ):
            # Must be a no-op (and never raise) when metrics are unavailable.
            result = _record_decision_persist_failure(resolved_action="continue", failure_class="transient")

        assert result is None
        ensure_metrics.assert_called_once()

    def test_metrics_error_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch.object(eval_persist_order, "_ensure_metrics", MagicMock(side_effect=RuntimeError("otel down"))),
            caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER),
        ):
            _record_decision_persist_failure(resolved_action="block", failure_class="referential")

        records = [r for r in caplog.records if r.message == "eval_persist_order.metrics_unavailable"]
        assert len(records) == 1


class TestDecisionRecordPersistSuccessPath:
    """The happy path persists the decision row after setting RLS context."""

    @pytest.mark.asyncio
    async def test_sets_rls_and_adds_decision_row(self) -> None:
        session = _ok_session()
        with (
            patch(
                "modulo.core.pipeline_engine.eval_persist_order.set_rls_org",
                AsyncMock(),
            ) as set_org,
            patch(
                "modulo.core.pipeline_engine.eval_persist_order.set_rls_execution_context",
                AsyncMock(),
            ) as set_ctx,
        ):
            await _persist_decision_row(
                _snapshot(),
                _outcome(),
                run_id,
                session_factory=_session_factory(session),
                org_id=org_id,
            )

        set_org.assert_awaited_once_with(session, org_id)
        set_ctx.assert_awaited_once_with(session)
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_warns_when_policy_gate_node_id_missing(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _ok_session()
        eval_def = EvalDefDTO(
            id=uuid.uuid4(),
            org_id=org_id,
            name="gate-eval",
            eval_type="regex",
            config={"pattern": "pass", "field": "text"},
            failure_behaviour="warn",
            node_id=str(uuid.uuid4()),
            policy_gate_id=gate_id,
            policy_gate_version=1,
            policy_gate_node_id=None,
        )

        with (
            patch(
                "modulo.core.pipeline_engine.eval_persist_order.set_rls_org",
                AsyncMock(),
            ),
            patch(
                "modulo.core.pipeline_engine.eval_persist_order.set_rls_execution_context",
                AsyncMock(),
            ),
            caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER),
        ):
            results = await run_evals_persist_before_decide(
                eval_defs=[eval_def],
                resolve_eval_target=lambda ed: {"text": "pass"},
                run_id=run_id,
                org_id=org_id,
                session_factory=_session_factory(session),
                node_id="node-1",
            )

        assert results["gate-eval"].passed is True
        records = [r for r in caplog.records if r.message == "eval_persist_order.policy_gate_node_id_missing"]
        assert len(records) == 1


class TestFailOpenDoesNotPropagate:
    """Criterion 4: a persistence failure must not raise out of the wrapper."""

    @pytest.mark.asyncio
    async def test_transient_failure_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _failing_savepoint_session(RuntimeError("connection refused"))

        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            result = await _persist_decision_row(
                _snapshot(),
                _outcome(),
                run_id,
                session_factory=_session_factory(session),
                org_id=org_id,
            )

        # The wrapper returned normally (no raise) and handled the failure by
        # classifying it as transient and logging the structured event.
        assert result is None
        records = [r for r in caplog.records if r.message == "policy_gate_decision.persist_failed"]
        assert len(records) == 1
        assert records[0].failure_class == "transient"

    @pytest.mark.asyncio
    async def test_fk_violation_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        exc = IntegrityError("stmt", {}, Exception("fk violation"))
        exc.constraint_name = "fk_policy_gate_decisions_gate_org"
        session = _failing_savepoint_session(exc)

        with caplog.at_level(logging.ERROR, logger=_MODULE_LOGGER):
            result = await _persist_decision_row(
                _snapshot(),
                _outcome("warn"),
                run_id,
                session_factory=_session_factory(session),
                org_id=org_id,
            )

        # The wrapper returned normally (no raise) and handled the referential
        # failure by logging the structured error event.
        assert result is None
        records = [r for r in caplog.records if r.message == "policy_gate_decision.persist_failed_referential"]
        assert len(records) == 1
        assert records[0].failure_class == "referential"

    @pytest.mark.asyncio
    async def test_session_factory_raising_is_swallowed(self) -> None:
        @asynccontextmanager
        async def _broken_factory():
            raise RuntimeError("pool exhausted")
            yield None  # pragma: no cover

        await _persist_decision_row(
            _snapshot(),
            _outcome(),
            run_id,
            session_factory=_broken_factory,
            org_id=org_id,
        )


class TestFailOpenStructuredLog:
    """Criterion 5: structured events carry failure_class and context."""

    @pytest.mark.asyncio
    async def test_transient_failure_logs_warning_with_failure_class(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _failing_savepoint_session(RuntimeError("pool exhausted"))
        outcome = _outcome("continue")

        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _persist_decision_row(
                _snapshot(),
                outcome,
                run_id,
                session_factory=_session_factory(session),
                org_id=org_id,
            )

        records = [r for r in caplog.records if r.message == "policy_gate_decision.persist_failed"]
        assert len(records) == 1
        record = records[0]
        assert record.failure_class == "transient"
        assert record.policy_gate_id == str(gate_id)
        assert record.run_id == str(run_id)
        assert record.resolved_action == "continue"

    @pytest.mark.asyncio
    async def test_fk_violation_logs_error_with_constraint_and_eval_id(self, caplog: pytest.LogCaptureFixture) -> None:
        exc = IntegrityError("stmt", {}, Exception("fk violation"))
        exc.constraint_name = "fk_policy_gate_decisions_gate_org"
        session = _failing_savepoint_session(exc)
        snapshot = _snapshot()
        outcome = resolve_policy_gate(snapshot)
        assert outcome.action == "continue"

        with caplog.at_level(logging.ERROR, logger=_MODULE_LOGGER):
            await _persist_decision_row(
                snapshot,
                outcome,
                run_id,
                session_factory=_session_factory(session),
                org_id=org_id,
            )

        records = [r for r in caplog.records if r.message == "policy_gate_decision.persist_failed_referential"]
        assert len(records) == 1
        record = records[0]
        assert record.failure_class == "referential"
        assert record.constraint_name == "fk_policy_gate_decisions_gate_org"
        assert record.eval_id == str(snapshot.eval.id)
        assert record.resolved_action == "continue"


class TestFailOpenDoesNotBlockContinue:
    """Criterion 6: a persistence failure does not block a continue outcome."""

    @pytest.mark.asyncio
    async def test_run_continues_when_decision_persistence_fails(self) -> None:
        # First session (EvalResult persist) succeeds; second session
        # (decision-record savepoint) raises on begin_nested().
        ok_session = MagicMock()
        ok_session.execute = AsyncMock()
        ok_session.__aenter__ = AsyncMock(return_value=ok_session)
        ok_session.__aexit__ = AsyncMock(return_value=False)
        ok_session.begin = MagicMock(return_value=_cm())
        ok_session.add = MagicMock()
        ok_session.flush = AsyncMock()

        eval_out = {"text": "pass"}
        eval_def = EvalDefDTO(
            id=uuid.uuid4(),
            org_id=org_id,
            name="gate-eval",
            eval_type="regex",
            config={"pattern": "pass", "field": "text"},
            failure_behaviour="warn",
            node_id=str(uuid.uuid4()),
            policy_gate_id=gate_id,
            policy_gate_version=1,
            policy_gate_node_id=uuid.uuid4(),
        )

        sessions = [ok_session, _failing_savepoint_session(RuntimeError("savepoint boom"))]
        call_idx = 0

        @asynccontextmanager
        async def _factory():
            nonlocal call_idx
            idx = call_idx
            call_idx += 1
            yield sessions[idx]

        results = await run_evals_persist_before_decide(
            eval_defs=[eval_def],
            resolve_eval_target=lambda ed: eval_out,
            run_id=run_id,
            org_id=org_id,
            session_factory=_factory,
            node_id="node-1",
        )

        assert results["gate-eval"].passed is True
        assert "gate-eval" in results
        assert len(results) == 1


class TestCancelledErrorPropagates:
    """Criterion 7: CancelledError escapes the wrapper."""

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self) -> None:
        session = _failing_savepoint_session(asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await _persist_decision_row(
                _snapshot(),
                _outcome(),
                run_id,
                session_factory=_session_factory(session),
                org_id=org_id,
            )


class TestDecisionStateUnchangedAfterPersistenceFailure:
    """Criterion 16: the fail-open wrapper never mutates or reverses the
    decision when persistence fails.

    Captures the decision's in-memory state BEFORE the persistence attempt,
    forces a persistence failure, then asserts the state is IDENTICAL after.
    """

    @pytest.mark.asyncio
    async def test_outcome_identical_before_and_after(self) -> None:
        snapshot = _snapshot()
        # Resolve the outcome BEFORE persistence — this is the authoritative decision
        outcome_before = resolve_policy_gate(snapshot)
        assert outcome_before.action == "continue"
        assert outcome_before.result is True
        assert outcome_before.error is None

        session = _failing_savepoint_session(RuntimeError("connection pool exhausted"))

        # The persistence attempt must NOT raise
        await _persist_decision_row(
            snapshot,
            outcome_before,
            run_id,
            session_factory=_session_factory(session),
            org_id=org_id,
        )

        # Re-resolve the outcome AFTER persistence — the snapshot is immutable,
        # so the outcome must be identical
        outcome_after = resolve_policy_gate(snapshot)
        assert outcome_after.action == outcome_before.action
        assert outcome_after.result == outcome_before.result
        assert outcome_after.eval_result_id == outcome_before.eval_result_id
        assert outcome_after.error == outcome_before.error

    @pytest.mark.asyncio
    async def test_block_outcome_not_reversed_by_persistence_failure(self) -> None:
        """A 'block' decision must NOT be reversed to 'continue' when persistence fails."""
        snapshot = _snapshot()
        outcome_before = resolve_policy_gate(snapshot)
        # Override to simulate a block outcome
        block_outcome = Outcome(
            result=outcome_before.result,
            action="block",
            eval_result_id=outcome_before.eval_result_id,
            error=outcome_before.error,
        )

        session = _failing_savepoint_session(IntegrityError("stmt", {}, Exception("fk")))

        await _persist_decision_row(
            snapshot,
            block_outcome,
            run_id,
            session_factory=_session_factory(session),
            org_id=org_id,
        )

        # The outcome object was not mutated
        assert block_outcome.action == "block"
        assert block_outcome.result == outcome_before.result
        assert block_outcome.error == outcome_before.error

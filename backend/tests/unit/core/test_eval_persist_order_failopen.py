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
from unittest.mock import AsyncMock, MagicMock

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
from modulo.core.pipeline_engine.eval_persist_order import (
    EvalDefDTO,
    _persist_decision_row,
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


class TestFailOpenDoesNotPropagate:
    """Criterion 4: a persistence failure must not raise out of the wrapper."""

    @pytest.mark.asyncio
    async def test_transient_failure_is_swallowed(self) -> None:
        session = _failing_savepoint_session(RuntimeError("connection refused"))

        await _persist_decision_row(
            _snapshot(),
            _outcome(),
            run_id,
            session_factory=_session_factory(session),
            org_id=org_id,
        )

    @pytest.mark.asyncio
    async def test_fk_violation_is_swallowed(self) -> None:
        exc = IntegrityError("stmt", {}, Exception("fk violation"))
        exc.constraint_name = "fk_policy_gate_decisions_gate_org"
        session = _failing_savepoint_session(exc)

        await _persist_decision_row(
            _snapshot(),
            _outcome("warn"),
            run_id,
            session_factory=_session_factory(session),
            org_id=org_id,
        )

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

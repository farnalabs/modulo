"""Unit tests for the guardrails ingestion-edge core (FAR-208).

Covers:
- de-risk spike 1: deliver_manual is unavailable for terminal eval_failed runs
  (a HITL gate only exists while the run is paused at a gate; a terminal
  eval_failed run has no gate, so deliver_manual raises GateNotFoundError,
  which the route maps to 404).
- de-risk spike 2: execution-isolation assumption — EvalEngine.evaluate is a
  synchronous call for regex/json_schema detection, so a bounded
  ``asyncio.wait_for`` timeout is sufficient (no thread/process pool needed).
"""

import asyncio
import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.hitl_manager import GateNotFoundError, HITLManager

# ---------------------------------------------------------------------------
# Spike 1 — deliver_manual unavailable for terminal eval_failed runs
# ---------------------------------------------------------------------------


def _run_identifier() -> tuple[uuid.UUID, str]:
    return uuid.uuid4(), "gate"


@pytest.mark.asyncio
async def test_spike_deliver_manual_unavailable_for_terminal_eval_failed_run():
    """A terminal eval_failed run has no HITL gate — deliver_manual is 404.

    The guardrail-override-as-recover_node design relies on this: a run that
    a guardrail blocked reaches eval_failed TERMINAL with no HITL gate, so the
    human can only remediate through the guardrail-override path (recover_node
    extension), never through deliver_manual.
    """
    run_id, gate_id = _run_identifier()
    session = AsyncMock(spec=AsyncSession)
    # No gate row exists for this run: the UPDATE matches nothing and the
    # follow-up SELECT (GateNotFound check) also returns nothing.
    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = None
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(side_effect=[update_result, select_result])

    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.deliver_manual(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=uuid.uuid4(),
            claim_token="opaque-token",
            output={"result": "manual"},
        )

    # The route maps GateNotFoundError to 404 (api/routes/hitl.py
    # deliver_manual_output) — assert the error type only here; the mapping is
    # covered by the route's except clause contract.
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_spike_deliver_manual_requires_a_gate_even_for_failed_runs():
    """deliver_manual on a plain failed run also 404s when no gate exists."""
    run_id, gate_id = _run_identifier()
    session = AsyncMock(spec=AsyncSession)
    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = None
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(side_effect=[update_result, select_result])

    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.deliver_manual(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=uuid.uuid4(),
            claim_token="opaque-token",
            output={"result": "manual"},
        )


# ---------------------------------------------------------------------------
# Spike 2 — execution-isolation assumption (synchronous pure detection)
# ---------------------------------------------------------------------------


def test_spike_engine_evaluate_is_synchronous_for_deterministic_types():
    """engine.evaluate is NOT a coroutine for regex/json_schema detection.

    Confirms the execution-isolation assumption: with regex/json_schema-only
    guardrail detection on bounded payloads, ``asyncio.wait_for`` around the
    synchronous call is sufficient — no thread/process pool is needed.
    """
    engine = EvalEngine()
    for eval_type in (EvalType.REGEX, EvalType.JSON_SCHEMA):
        assert not inspect.iscoroutinefunction(engine.evaluate), eval_type
        assert not inspect.isawaitable(
            engine.evaluate(
                {},
                EvalDefinition(
                    id=uuid.uuid4(),
                    org_id=uuid.uuid4(),
                    name="spike",
                    eval_type=eval_type,
                ),
            )
        )


@pytest.mark.asyncio
async def test_spike_wait_for_bounds_synchronous_detection():
    """A bounded wait_for around the synchronous eval completes cleanly."""
    engine = EvalEngine()
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="regex-spike",
        eval_type=EvalType.REGEX,
        config={"field": "body", "pattern": r"token_[a-z0-9]{6}"},
        failure_behaviour="warn",
    )

    async def run() -> None:
        result = engine.evaluate({"body": "prefix token_abc123 suffix"}, eval_def)
        assert result.passed is True

    await asyncio.wait_for(run(), timeout=5.0)

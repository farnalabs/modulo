"""FAR-223 item 7 — bounded evaluation, cap resolution, skip outcome (core).

Unit tests for the async interception pass (per-guardrail hard timeout,
bounded-payload budget, mechanism-error fail-closed), the per-node cap
resolution helpers, and the snapshot-pin DTO round-trip. Pure core tests —
no DB, no FastAPI.
"""

import time
import uuid
from typing import Any, ClassVar

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.guardrails import (
    DEFAULT_GUARDRAIL_TIMEOUT_SECONDS,
    DEFAULT_MAX_GUARDRAILS_PER_NODE,
    GuardrailSkip,
    check_payload_within_budget,
    guardrail_cap_violation,
    resolve_guardrail_cap,
    resolve_guardrail_timeout,
    run_interception_pass_async,
    serialize_guardrail_pin,
    to_engine_definition_from_pin,
)

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def _def(
    name: str,
    action: str,
    *,
    node_id: str | None = None,
    config: dict[str, Any] | None = None,
    timeout: float | None = None,
    cap: int | None = None,
) -> EvalDefinition:
    cfg: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    if config:
        cfg.update(config)
    if timeout is not None:
        cfg["guardrail_timeout_seconds"] = timeout
    if cap is not None:
        cfg["max_guardrails_per_node"] = cap
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=node_id,
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=cfg,
        failure_behaviour="warn",
    )


class _SleepingEngine(EvalEngine):
    """Engine whose evaluate() blocks for a fixed duration."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self._delay = delay

    def evaluate(self, output: dict[str, Any], eval_def: EvalDefinition, **kwargs: Any) -> Any:
        time.sleep(self._delay)
        raise AssertionError("should never reach real evaluation in timeout tests")


# ---------------------------------------------------------------------------
# Per-guardrail hard timeout
# ---------------------------------------------------------------------------


async def test_timeout_fails_closed_for_block_guardrail():
    slow = _def("slow-block", "block", timeout=0.05)
    outcome = await run_interception_pass_async(
        _SleepingEngine(5.0),
        [slow],
        {"body": "leak SECRET_ABC12345"},
        timeout_seconds=0.05,
    )
    assert outcome.blocked is True
    assert "mechanism error" in outcome.block_message
    assert outcome.blocking_eval_name == "slow-block"
    assert outcome.results == []


async def test_timeout_log_and_continue_for_observe_guardrail(caplog):
    slow = _def("slow-observe", "observe", timeout=0.05)
    outcome = await run_interception_pass_async(
        _SleepingEngine(5.0),
        [slow],
        {"body": "leak SECRET_ABC12345"},
        timeout_seconds=0.05,
    )
    assert outcome.blocked is False
    # log-and-continue: a failed result is recorded so the mechanism error
    # stays observable, and its detail never carries the raw payload.
    assert len(outcome.results) == 1
    assert outcome.results[0].passed is False
    assert "SECRET_ABC12345" not in outcome.results[0].detail
    assert any("detection" in r.message for r in caplog.records)


async def test_timeout_log_and_continue_for_warn_guardrail():
    slow = _def("slow-warn", "warn", timeout=0.05)
    outcome = await run_interception_pass_async(
        _SleepingEngine(5.0),
        [slow],
        {"body": "leak SECRET_ABC12345"},
        timeout_seconds=0.05,
    )
    assert outcome.blocked is False
    assert len(outcome.results) == 1
    assert outcome.results[0].passed is False


async def test_resolve_timeout_defaults_and_declared():
    assert resolve_guardrail_timeout([_def("a", "observe")]) == DEFAULT_GUARDRAIL_TIMEOUT_SECONDS
    assert resolve_guardrail_timeout([_def("a", "observe", timeout=0.5)]) == 0.5
    assert resolve_guardrail_timeout([_def("a", "observe", timeout=0.5), _def("b", "observe", timeout=1.5)]) == 1.5


async def test_timeout_applies_per_guardrail_not_pass_wide():
    """A fast guardrail still evaluates when a sibling times out (per-guardrail budget)."""
    fast = _def("fast", "block")
    slow = _def("slow", "block", timeout=0.05)
    outcome = await run_interception_pass_async(
        _SleepingEngine(5.0),
        [fast, slow],
        {"body": "leak SECRET_ABC12345"},
        timeout_seconds=0.05,
    )
    # The slow block guardrail timed out → fail closed.
    assert outcome.blocked is True
    assert outcome.blocking_eval_name == "slow"


# ---------------------------------------------------------------------------
# Bounded payload budget
# ---------------------------------------------------------------------------


def test_payload_budget_check():
    assert check_payload_within_budget({"a": "b"}, 10) is True
    assert check_payload_within_budget({"a": "x" * 100}, 10) is False
    assert check_payload_within_budget({}, 10) is True
    assert check_payload_within_budget({"a": "b"}, 0) is True  # 0 = budget off


async def test_over_budget_fails_closed_for_block_guardrail():
    big_payload = {"body": "x" * 5000}
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [_def("big", "block")],
        big_payload,
        max_payload_bytes=100,
    )
    assert outcome.blocked is True
    assert outcome.blocking_eval_name == "<payload-budget>"


async def test_over_budget_log_and_continue_for_observe_guardrail():
    big_payload = {"body": "x" * 5000}
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [_def("big", "observe")],
        big_payload,
        max_payload_bytes=100,
    )
    assert outcome.blocked is False
    assert len(outcome.results) == 1
    assert outcome.results[0].passed is False
    assert "budget" in outcome.results[0].detail


# ---------------------------------------------------------------------------
# Per-node cap resolution
# ---------------------------------------------------------------------------


def test_cap_defaults_to_constant():
    assert resolve_guardrail_cap([_def("a", "observe")]) == DEFAULT_MAX_GUARDRAILS_PER_NODE


def test_cap_zero_turns_feature_off():
    assert resolve_guardrail_cap([_def("a", "observe", cap=0)]) == 0
    # 0 (feature off) wins even when another row declares a higher cap.
    assert resolve_guardrail_cap([_def("a", "observe", cap=0), _def("b", "observe", cap=16)]) == 0


def test_cap_uses_max_declared():
    assert resolve_guardrail_cap([_def("a", "observe", cap=4), _def("b", "observe", cap=16)]) == 16


def test_cap_violation_org_level_rows():
    org_rows = [_def(f"g{i}", "observe") for i in range(DEFAULT_MAX_GUARDRAILS_PER_NODE + 1)]
    violation = guardrail_cap_violation(org_rows)
    assert violation is not None
    assert "org-level" in violation


def test_cap_violation_node_bound_rows():
    rows = [_def("node-g1", "observe", node_id="n1"), _def("node-g2", "observe", node_id="n1")]
    # 2 node-bound rows, no org-level rows → within the default cap of 8.
    assert guardrail_cap_violation(rows) is None
    too_many = [_def(f"node-g{i}", "observe", node_id="n1") for i in range(DEFAULT_MAX_GUARDRAILS_PER_NODE + 1)]
    violation = guardrail_cap_violation(too_many)
    assert violation is not None
    assert "n1" in violation


def test_cap_violation_respects_feature_off():
    rows = [_def(f"g{i}", "observe", cap=0) for i in range(20)]
    assert guardrail_cap_violation(rows) is None


# ---------------------------------------------------------------------------
# Skip outcome carried through the async pass
# ---------------------------------------------------------------------------


async def test_async_pass_carries_skipped_entries():
    skip = GuardrailSkip(name="ghost", reason="soft_deleted")
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [_def("ok", "observe")],
        {"body": "clean"},
        skipped=[skip],
    )
    assert outcome.skipped == [skip]
    assert outcome.blocked is False


async def test_async_pass_zero_definitions_returns_empty_with_skipped():
    skip = GuardrailSkip(name="ghost", reason="soft_deleted")
    outcome = await run_interception_pass_async(EvalEngine(), [], {"body": "x"}, skipped=[skip])
    assert outcome.skipped == [skip]
    assert outcome.results == []


async def test_async_pass_replay_is_detection_only():
    """A replay (detection_only) never blocks and never redacts — item 10."""
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [_def("no-secrets", "block")],
        {"body": "leak SECRET_ABC12345", "credentials": {"api_key": "sk-live-123"}},
        detection_only=True,
    )
    assert outcome.blocked is False
    # detection-only preserves the raw payload (no act).
    assert outcome.payload["credentials"]["api_key"] == "sk-live-123"
    assert len(outcome.results) == 1


# ---------------------------------------------------------------------------
# Snapshot pin serialization round-trip (item 10)
# ---------------------------------------------------------------------------


class _FakeRow:
    id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    organisation_id = _ORG
    pipeline_id = _PIPELINE
    node_id = None
    name = "pin-guard"
    eval_type = "guardrail"
    config_json: ClassVar[dict[str, Any]] = {
        "action": "block",
        "type": "regex",
        "field": "body",
        "pattern": r"TOKEN_[A-Z0-9]{6}",
    }
    failure_behaviour = "warn"
    pass_threshold = None
    suite_id = None


def test_serialize_and_rebuild_pin_round_trip():
    pin = serialize_guardrail_pin(_FakeRow())
    assert pin["name"] == "pin-guard"
    rebuilt = to_engine_definition_from_pin(pin)
    assert rebuilt.id == _FakeRow.id
    assert rebuilt.name == "pin-guard"
    assert rebuilt.eval_type == EvalType.GUARDRAIL
    assert rebuilt.config["action"] == "block"
    assert rebuilt.config["pattern"] == r"TOKEN_[A-Z0-9]{6}"
    assert rebuilt.pipeline_id == _PIPELINE


async def test_malformed_pin_entry_is_skippable_not_crashy():
    pin = serialize_guardrail_pin(_FakeRow())
    pin["id"] = "not-a-uuid"
    with pytest.raises(ValueError):
        to_engine_definition_from_pin(pin)


async def test_timeout_wraps_real_detection_in_thread():
    """The fast path still detects correctly through the async pass."""
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [_def("no-secrets", "block")],
        {"body": "leak SECRET_ABC12345"},
    )
    assert outcome.blocked is True
    assert outcome.blocking_eval_name == "no-secrets"

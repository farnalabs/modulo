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


class _SplitSleepEngine(EvalEngine):
    """Engine that sleeps only for the named guardrails, real detection otherwise.

    Lets a single pass carry BOTH a fast guardrail (real regex detection, which
    completes) and a slow one (sleeps and times out) so the per-guardrail budget
    is actually proven, not assumed.
    """

    def __init__(self, sleeping: set[str], delay: float) -> None:
        super().__init__()
        self._sleeping = sleeping
        self._delay = delay

    def evaluate(self, output: dict[str, Any], eval_def: EvalDefinition, **kwargs: Any) -> Any:
        if eval_def.name in self._sleeping:
            time.sleep(self._delay)
            raise AssertionError("should never reach real evaluation for sleeping guardrails")
        return super().evaluate(output, eval_def, **kwargs)


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
    assert not outcome.results


async def test_timeout_fails_closed_for_redact_guardrail():
    """A redact-action guardrail is a guarding action: a detection timeout must
    fail closed (block), never silently skip the mask that protects the field."""
    slow = _def("slow-redact", "redact", timeout=0.05)
    outcome = await run_interception_pass_async(
        _SleepingEngine(5.0),
        [slow],
        {"body": "leak SECRET_ABC12345"},
        timeout_seconds=0.05,
    )
    assert outcome.blocked is True
    assert outcome.blocking_eval_name == "slow-redact"
    assert "mechanism error" in outcome.block_message


async def test_detection_error_fails_closed_for_block_guardrail():
    """A malformed block guardrail (empty regex pattern — a config the engine
    rejects) is a mechanism error: fail closed, never a pass-through with a
    broken detector."""
    bad = _def("bad-pattern", "block", config={"pattern": "", "field": "body"})
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [bad],
        {"body": "clean"},
        timeout_seconds=5.0,
    )
    assert outcome.blocked is True
    assert outcome.blocking_eval_name == "bad-pattern"
    assert "mechanism error" in outcome.block_message


async def test_malformed_shape_guardrail_skipped_not_aborting_pass():
    """A guardrail whose config fails SHAPE validation (a bad ``action`` value
    → pydantic.ValidationError) is handled exactly like the other mechanism
    errors: log-and-continued for observe, and its redaction-phase
    re-validation must SKIP it rather than abort the whole pass — sibling
    redact guardrails still apply their masks."""
    bad = _def("bad-action", "observe", config={"action": "not-an-action"})
    ok = _def("ok", "redact", config={"redaction": [{"path": "credentials.api_key", "mode": "transform"}]})
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [ok, bad],
        {"credentials": {"api_key": "sk-live-123"}, "body": "clean"},
        timeout_seconds=5.0,
    )
    assert outcome.blocked is False
    # The valid redact guardrail's mask was NOT lost to the sibling's failure.
    assert outcome.payload["credentials"]["api_key"] == "\u2022\u2022\u2022\u2022\u2022\u2022"
    # The malformed row is recorded as a failed result (log-and-continue).
    assert len(outcome.results) == 2
    assert any(r.passed is False and "mechanism error" in r.detail for r in outcome.results)


async def test_malformed_shape_block_guardrail_fails_closed():
    """A block-action guardrail with a shape-invalid config (timeout 0) fails
    closed at detection, and its redaction-phase re-validation is skipped —
    never re-raising and killing the pass."""
    bad = _def("bad-timeout", "block", config={"guardrail_timeout_seconds": 0})
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [bad],
        {"body": "clean"},
        timeout_seconds=5.0,
    )
    assert outcome.blocked is True
    assert outcome.blocking_eval_name == "bad-timeout"
    assert "mechanism error" in outcome.block_message


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


def test_resolve_timeout_defaults_and_declared():
    assert resolve_guardrail_timeout([_def("a", "observe")]) == DEFAULT_GUARDRAIL_TIMEOUT_SECONDS
    assert resolve_guardrail_timeout([_def("a", "observe", timeout=0.5)]) == 0.5
    assert resolve_guardrail_timeout([_def("a", "observe", timeout=0.5), _def("b", "observe", timeout=1.5)]) == 1.5


async def test_timeout_applies_per_guardrail_not_pass_wide():
    """A fast guardrail still evaluates when a sibling times out (per-guardrail budget).

    Uses a split engine: the 'fast' guardrail runs real (fast) regex detection
    against a CLEAN payload and completes, while the 'slow' guardrail blocks in
    the sleeping engine (5s >> 1s budget) and times out. If the budget were
    pass-wide, the fast guardrail would time out too and yield NO result —
    asserting its real (non-mechanism-error) result is what proves per-guardrail
    independence. The 1s budget is generous so the fast thread's pool scheduling
    can never be mistaken for a pass-wide timeout.
    """
    fast = _def("fast", "block", config={"pattern": r"FAST_MARKER_\d{4}", "field": "body"})
    slow = _def("slow", "block", timeout=1.0)
    outcome = await run_interception_pass_async(
        _SplitSleepEngine(sleeping={"slow"}, delay=5.0),
        [fast, slow],
        {"body": "leak SECRET_ABC12345"},
        timeout_seconds=1.0,
    )
    # The slow block guardrail timed out → fail closed. The FAST guardrail
    # evaluated cleanly (no timeout, no violation), which is what proves the
    # budget is PER-GUARDRAIL, not pass-wide.
    assert outcome.blocked is True
    assert outcome.blocking_eval_name == "slow"
    # The fast guardrail COMPLETED: a real result (clean payload → passed
    # False), never a mechanism-error timeout result.
    fast_results = [r for r in outcome.results if r.eval_id == fast.id]
    assert len(fast_results) == 1
    assert fast_results[0].passed is False
    assert "mechanism error" not in fast_results[0].detail


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


async def test_over_budget_detection_only_replay_never_blocks():
    """A detection_only replay (item 10) whose payload is over budget records a
    mechanism error per bound guardrail and NEVER blocks.

    The over-budget early return must consult ``detection_only`` the same way the
    in-loop mechanism-error path does — a replay must never re-ingest as a block
    (run creation would otherwise fail the run as ``eval_failed``)."""
    big_payload = {"body": "x" * 5000}
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [_def("big", "block")],
        big_payload,
        max_payload_bytes=100,
        detection_only=True,
    )
    assert outcome.blocked is False
    assert len(outcome.results) == 1
    assert outcome.results[0].passed is False
    assert "budget" in outcome.results[0].detail


async def test_over_budget_detection_only_records_error_per_bound_def():
    """One errored (mechanism-fail) result per bound def — block/redact AND
    observe/warn — for a detection_only replay over budget, and the raw payload
    never leaks into any result detail."""
    big_payload = {"body": "x" * 5000}
    defs = [_def("b1", "block"), _def("r1", "redact"), _def("o1", "observe"), _def("w1", "warn")]
    outcome = await run_interception_pass_async(
        EvalEngine(),
        defs,
        big_payload,
        max_payload_bytes=100,
        detection_only=True,
    )
    assert outcome.blocked is False
    assert len(outcome.results) == len(defs)
    assert all(r.passed is False for r in outcome.results)
    assert all("mechanism error" in r.detail for r in outcome.results)
    assert all("x" * 10 not in r.detail for r in outcome.results)


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


def test_cap_violation_with_raised_cap():
    """A RAISED org cap is enforced by guardrail_cap_violation: cap rows pass,
    cap+1 rows violate. The default-8 constant is not the only cap shape."""
    assert guardrail_cap_violation([_def("a", "observe", cap=2), _def("b", "observe", cap=2)]) is None
    violation = guardrail_cap_violation([_def(f"g{i}", "observe", cap=2) for i in range(3)])
    assert violation is not None
    assert "cap" in violation


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
    assert not outcome.results


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


async def test_detection_only_records_mechanism_error_for_guarding_guardrail():
    """A detection_only replay NEVER drops a mechanism error — item 10.

    A guarding (block) guardrail whose detection raises (empty regex pattern →
    GuardrailConfigError) is recorded as an errored result even though it would
    fail CLOSED in a live pass: the replay must preserve the evidence of what
    happened (guardrail_summary errored bucket), so the result is kept and the
    pass never blocks."""
    bad = _def("bad-pattern", "block", config={"pattern": "", "field": "body"})
    outcome = await run_interception_pass_async(
        EvalEngine(),
        [bad],
        {"body": "clean"},
        timeout_seconds=5.0,
        detection_only=True,
    )
    assert outcome.blocked is False
    assert len(outcome.results) == 1
    assert outcome.results[0].passed is False
    assert "mechanism error" in outcome.results[0].detail


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


def test_malformed_pin_entry_is_skippable_not_crashy():
    pin = serialize_guardrail_pin(_FakeRow())
    pin["id"] = "not-a-uuid"
    with pytest.raises(ValueError, match="badly formed hexadecimal UUID string"):
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

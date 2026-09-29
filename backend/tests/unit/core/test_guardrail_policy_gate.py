"""Chunk-8 acceptance coverage: guardrail → Policy Gate mapping (FAR-1107).

Section 5.2 makes :func:`resolve_policy_gate` the enforcement authority for
any guardrail whose eval definition carries a *live* Policy Gate row (§3.1
mapping: ``observe``/``redact`` → no gate; ``warn`` → warn; ``block`` →
block). Agentic detection still runs through the pure eval helpers, but the
**decision about what a violation means is the resolver's**, not the engine's
and not the raw ``config_json.action`` fallback.

These tests pin the resolver-authority contract on the core module surface:

* criterion 1 — a bound block guardrail raises ``GuardrailBlockedError``
  (exact type, name preserved);
* criterion 2 — a bound warn guardrail never raises (a failing result comes
  back as a warn violation), and the bound gate overrides a ``block`` config;
* criterion 3 — the mirror's ``failure_behaviour`` pin stays cosmetic: with
  the pin removed the enforcement outcome is unchanged because the wiring
  evaluates through compute-only paths and decides via the resolver;
* criterion 5 — the non-raising interception-pass contract is preserved for
  both chunk-1 (config fallback) and chunk-8 (resolver) enforcement;
* criterion 6 — guardrail definitions must never carry
  ``failure_behaviour='retry'`` (validated, fail-closed);
* criterion 9 (unit half) — the guardrail evidence-write authorisation
  allowlist (``connector_*`` / ``capability_*`` under ``system_state``).

json_schema detection is used deliberately for resolver-enforced paths: a
json_schema violation yields ``passed=False`` (the violation IS the failing
result), which also matches the wire-level result semantics the resolver
consumes. The known regex asymmetry (regex violation yields ``passed=True``)
must not be papered over here — see the delivery report.

Spec: chunk-08 §6 (criteria 1-12).
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalDefinition,
    EvalEngine,
    EvalType,
)
from modulo.core.guardrails import (
    GuardrailBlockedError,
    GuardrailConfigError,
    _resolve_detection,
    _sanitise_guardrail_detail,
    _validate_guardrail_definition,
    evaluate_guardrails,
    run_interception_pass_async,
)

_ORG_ID = uuid.uuid4()
_NODE_ID = uuid.uuid4()

_VIOLATION_PAYLOAD: dict[str, Any] = {"name": 123}
_CLEAN_PAYLOAD: dict[str, Any] = {"name": "alice"}

_JS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


@dataclass
class _GateRow:
    """Policy Gate row stand-in: the attrs the wiring reads off the row."""

    id: uuid.UUID
    organisation_id: uuid.UUID
    version: int
    node_id: uuid.UUID | None
    action: str


def _guardrail(
    *,
    name: str = "gr",
    action: str = "block",
    schema: dict[str, Any] | None = None,
    failure_behaviour: str = "block",
    node_id: str | None = str(_NODE_ID),
) -> EvalDefinition:
    config: dict[str, Any] = {"action": action, "interception_point": "input"}
    if schema is None:
        schema = dict(_JS_SCHEMA)
    config["type"] = "json_schema"
    config["schema"] = schema
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        node_id=node_id,
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour=failure_behaviour,
    )


def _gate(action: str) -> _GateRow:
    return _GateRow(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        version=1,
        node_id=_NODE_ID,
        action=action,
    )


# ---------------------------------------------------------------------------
# Criterion 1 — bound block guardrail raises GuardrailBlockedError
# ---------------------------------------------------------------------------


class TestC1ResolverWiredBlock:
    """A bound block guardrail's violation is terminal through the resolver."""

    def test_block_gate_blocks_violating_payload(self: Any) -> None:
        engine = EvalEngine()
        d = _guardrail(name="block-gr", action="block")
        with pytest.raises(GuardrailBlockedError) as excinfo:
            evaluate_guardrails(
                engine,
                [d],
                dict(_VIOLATION_PAYLOAD),
                policy_gates={d.id: _gate("block")},
            )
        # Exact type matters: GuardrailBlockedError subclasses EvalBlockedError,
        # so callers that catch the parent keep working, but only the subclass
        # proves the enforcement went through resolver wiring (not a stray
        # engine-side raise).
        assert type(excinfo.value) is GuardrailBlockedError
        assert isinstance(excinfo.value, EvalBlockedError)
        assert excinfo.value.eval_name == "block-gr"

    def test_unwired_block_config_fallback_still_blocks(self: Any) -> None:
        # Chunk-1 contract preserved: a guardrail with NO gate row falls back
        # to the direct config_json.action check and still blocks.
        engine = EvalEngine()
        d = _guardrail(name="block-gr", action="block")
        with pytest.raises(GuardrailBlockedError) as excinfo:
            evaluate_guardrails(engine, [d], dict(_VIOLATION_PAYLOAD), policy_gates={})
        assert type(excinfo.value) is GuardrailBlockedError
        assert excinfo.value.eval_name == "block-gr"

    def test_passing_payload_does_not_raise(self: Any) -> None:
        engine = EvalEngine()
        d = _guardrail(name="block-gr", action="block")
        results = evaluate_guardrails(
            engine,
            [d],
            dict(_CLEAN_PAYLOAD),
            policy_gates={d.id: _gate("block")},
        )
        assert len(results) == 1
        assert results[0].passed is True


# ---------------------------------------------------------------------------
# Criterion 2 — bound warn guardrail never raises
# ---------------------------------------------------------------------------


class TestC2ResolverWiredWarn:
    """A bound warn guardrail surfaces violations without terminating."""

    def test_warn_gate_does_not_raise_on_violation(self: Any) -> None:
        engine = EvalEngine()
        d = _guardrail(name="warn-gr", action="block")
        results = evaluate_guardrails(
            engine,
            [d],
            dict(_VIOLATION_PAYLOAD),
            policy_gates={d.id: _gate("warn")},
        )
        assert len(results) == 1
        assert results[0].passed is False

    def test_resolver_warn_overrides_blocking_config(self: Any) -> None:
        # The resolver is the enforcement authority: when a gate row exists its
        # resolved action wins over the raw config action. wired = no raise;
        # an unwired config-fallback implementation would raise here.
        engine = EvalEngine()
        d = _guardrail(name="warn-gr", action="block")
        results = evaluate_guardrails(
            engine,
            [d],
            dict(_VIOLATION_PAYLOAD),
            policy_gates={d.id: _gate("warn")},
        )
        assert len(results) == 1
        assert results[0].passed is False

    def test_unwired_warn_config_never_raises(self: Any) -> None:
        # Chunk-1 contract preserved: un-wired warn guardrails are advisory.
        engine = EvalEngine()
        d = _guardrail(name="warn-gr", action="warn")
        results = evaluate_guardrails(engine, [d], dict(_VIOLATION_PAYLOAD), policy_gates={})
        assert len(results) == 1
        assert results[0].passed is False


# ---------------------------------------------------------------------------
# Criterion 3 — the mirror failure_behaviour pin stays cosmetic
# ---------------------------------------------------------------------------


def _pinless_detect_one(
    engine: EvalEngine,
    pre_act: dict[str, Any],
    eval_def: EvalDefinition,
) -> Any:
    """Reproduce ``_detect_one`` WITHOUT overwriting ``failure_behaviour``.

    The production pin transplants the mirror's ``failure_behaviour`` to
    ``warn`` so the engine can never raise ``EvalBlockedError`` from inside
    detection. This stand-in passes the ORIGINAL behaviour straight through
    (block semantics stay engine-armed) — criterion 3 checks that the
    resolver's decision is unaffected by that arming.
    """
    _validate_guardrail_definition(eval_def)
    detection_type, effective_config = _resolve_detection(eval_def)
    mirrored = eval_def.model_copy(
        update={
            "eval_type": EvalType(detection_type),
            "failure_behaviour": eval_def.failure_behaviour,
            "config": effective_config,
        }
    )
    result = engine.evaluate_result(pre_act, mirrored)
    return _sanitise_guardrail_detail(detection_type, effective_config, result)


def _assert_mirror_is_actually_engine_armed(engine: EvalEngine, eval_def: EvalDefinition) -> None:
    """Prove the passthrough mirror is genuinely armed (counterfactual half).

    Constructs the same passthrough mirror ``_pinless_detect_one`` would and
    asserts the engine's RAISING path would block it — establishing that the
    pin-less wire-up below really did give up the engine-side protection and
    the resolver is what produced the observed outcome.
    """
    detection_type, effective_config = _resolve_detection(eval_def)
    armed = eval_def.model_copy(
        update={
            "eval_type": EvalType(detection_type),
            "failure_behaviour": eval_def.failure_behaviour,
            "config": effective_config,
        }
    )
    with pytest.raises(EvalBlockedError):
        engine.evaluate(dict(_VIOLATION_PAYLOAD), armed)


class TestC3ResolverAuthorityRegardlessOfPin:
    """Enforcement outcome is invariant to the mirror's failure_behaviour pin."""

    def test_pinless_mirror_resolver_still_blocks(self: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr("modulo.core.guardrails._detect_one", _pinless_detect_one)
        engine = EvalEngine()
        d = _guardrail(name="block-gr", action="block")
        _assert_mirror_is_actually_engine_armed(engine, d)
        with pytest.raises(GuardrailBlockedError) as excinfo:
            evaluate_guardrails(
                engine,
                [d],
                dict(_VIOLATION_PAYLOAD),
                policy_gates={d.id: _gate("block")},
            )
        assert type(excinfo.value) is GuardrailBlockedError
        assert excinfo.value.eval_name == "block-gr"

    def test_pinless_mirror_warn_gate_still_never_raises(self: Any, monkeypatch: Any) -> None:
        # Discriminator: blocking config + WARN gate row. Only the wired
        # resolver yields "warn" here — an engine-path or config-fallback
        # implementation would raise instead.
        monkeypatch.setattr("modulo.core.guardrails._detect_one", _pinless_detect_one)
        engine = EvalEngine()
        d = _guardrail(name="warn-gr", action="block")
        results = evaluate_guardrails(
            engine,
            [d],
            dict(_VIOLATION_PAYLOAD),
            policy_gates={d.id: _gate("warn")},
        )
        assert len(results) == 1
        assert results[0].passed is False


# ---------------------------------------------------------------------------
# Criterion 5 — non-raising interception-pass contract preserved
# ---------------------------------------------------------------------------


class TestC5InterceptionPassNonRaising:
    """``run_interception_pass_async`` reports blocks, never raises them."""

    def test_block_guardrail_reports_blocked(self: Any) -> None:
        """The ingestion edge uses the direct config_json.action check."""
        engine = EvalEngine()
        d = _guardrail(name="block-gr", action="block")
        outcome = asyncio.run(
            run_interception_pass_async(
                engine,
                [d],
                dict(_VIOLATION_PAYLOAD),
            )
        )
        assert outcome.blocked is True
        assert outcome.blocking_eval_name == "block-gr"

    def test_passing_payload_reports_not_blocked(self: Any) -> None:
        engine = EvalEngine()
        d = _guardrail(name="block-gr", action="block")
        outcome = asyncio.run(
            run_interception_pass_async(
                engine,
                [d],
                dict(_CLEAN_PAYLOAD),
            )
        )
        assert outcome.blocked is False


# ---------------------------------------------------------------------------
# Criterion 6 — guardrails never carry failure_behaviour='retry'
# ---------------------------------------------------------------------------


class TestC6RetryForbidden:
    def test_retry_failure_behaviour_fails_closed_at_validation(self: Any) -> None:
        d = _guardrail(failure_behaviour="warn")
        # Pydantic rejects failure_behaviour='retry' at construction (Literal
        # ['warn','block']), so bypass the model to exercise the engine-level
        # validation guard (AGENTS.md eval-engine lesson).
        object.__setattr__(d, "failure_behaviour", "retry")
        with pytest.raises(GuardrailConfigError):
            evaluate_guardrails(EvalEngine(), [d], dict(_CLEAN_PAYLOAD))


# ---------------------------------------------------------------------------
# Criterion 9 (unit half) — evidence-write authorisation allowlist
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# F1 — regex guardrails wired to a block gate are never enforced
#
# A regex detection's violation records ``passed=True`` (hit semantics — a
# regex "hit" IS the violation, yet the underlying eval passes). The old
# snapshot wiring read ``result.passed`` directly, so the resolver saw
# ``passed=True`` → ``continue`` → no enforcement.  Only json_schema
# guardrails were enforced.  These tests FAIL before the fix and PASS after.
# ---------------------------------------------------------------------------

_REGEX_VIOLATION_PAYLOAD: dict[str, Any] = {"secret": "my-api-key-12345"}
_REGEX_CLEAN_PAYLOAD: dict[str, Any] = {"secret": "hello"}


def _regex_guardrail(
    *,
    name: str = "regex-gr",
    action: str = "block",
    pattern: str = r"api-key-\d+",
    field: str = "secret",
    failure_behaviour: str = "block",
    node_id: str | None = str(_NODE_ID),
) -> EvalDefinition:
    """Build a regex-detection guardrail EvalDefinition."""
    config: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "pattern": pattern,
        "field": field,
    }
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        node_id=node_id,
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour=failure_behaviour,
    )


class TestF1RegexBlockGateEnforced:
    """Regex guardrails wired to a block gate MUST raise GuardrailBlockedError.

    Before the fix, regex violations (``passed=True`` hit semantics) were read
    as PASSING by the resolver → ``outcome.action = "continue"`` → no
    enforcement.  After the fix, the snapshot's ``passed`` is derived from
    ``_interpret_violation`` so regex hits are correctly translated to
    ``passed=False`` in the snapshot.
    """

    def test_regex_block_gate_blocks_violating_payload(self: Any) -> None:
        engine = EvalEngine()
        d = _regex_guardrail(name="regex-block", action="block")
        with pytest.raises(GuardrailBlockedError) as excinfo:
            evaluate_guardrails(
                engine,
                [d],
                dict(_REGEX_VIOLATION_PAYLOAD),
                policy_gates={d.id: _gate("block")},
            )
        assert type(excinfo.value) is GuardrailBlockedError
        assert excinfo.value.eval_name == "regex-block"

    def test_regex_warn_gate_does_not_raise(self: Any) -> None:
        engine = EvalEngine()
        d = _regex_guardrail(name="regex-warn", action="block")
        # Gate says "warn" — resolver must resolve to warn, not block.
        results = evaluate_guardrails(
            engine,
            [d],
            dict(_REGEX_VIOLATION_PAYLOAD),
            policy_gates={d.id: _gate("warn")},
        )
        assert len(results) == 1
        # For regex, a hit means passed=True (the raw result).
        assert results[0].passed is True

    def test_json_schema_block_still_enforced(self: Any) -> None:
        """Regression guard: json_schema guardrails must remain enforced."""
        engine = EvalEngine()
        d = _guardrail(name="js-block", action="block")
        with pytest.raises(GuardrailBlockedError) as excinfo:
            evaluate_guardrails(
                engine,
                [d],
                dict(_VIOLATION_PAYLOAD),
                policy_gates={d.id: _gate("block")},
            )
        assert type(excinfo.value) is GuardrailBlockedError
        assert excinfo.value.eval_name == "js-block"

    def test_regex_interception_pass_reports_blocked(self: Any) -> None:
        """Non-raising interception path: regex block guardrail reports blocked."""
        engine = EvalEngine()
        d = _regex_guardrail(name="regex-block", action="block")
        outcome = asyncio.run(
            run_interception_pass_async(
                engine,
                [d],
                dict(_REGEX_VIOLATION_PAYLOAD),
            )
        )
        assert outcome.blocked is True
        assert outcome.blocking_eval_name == "regex-block"


# ---------------------------------------------------------------------------
# F2 — evidence-write authorisation (unit half)
#
# RESIDUAL (FAR-1107 chunk 8, F3): ``assert_guardrail_evidence_write_authorised``
# was removed because no production code path writes Evidence rows
# (``connector_*`` / ``capability_*`` keys) to the Evidence table.  The
# closest seam is ``_persist_guardrail_eval_results`` in ``db/crud/run.py``,
# which writes ``EvalResult`` rows — not Evidence rows.  Chunk 7's dormancy
# is NOT lifted by this chunk; the owning chunk for the first evidence-write
# call site is TBD.
# ---------------------------------------------------------------------------

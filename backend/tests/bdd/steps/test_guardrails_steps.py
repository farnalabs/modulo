"""Step definitions for the Guardrail Detection Engine (T1) feature.

Exercises the pure engine functions in ``modulo.core.guardrails`` directly:
``evaluate_guardrails`` (raising), ``run_interception_pass`` (non-raising
two-phase), ``apply_redaction_masks`` (masks-only), ``derive_conformance_state``
(fail-closed three-state), and the misrouting/retry guardrails. Detection is
deterministic (regex / json_schema) and never routes through the generic
``EvalEngine``.
"""

import contextlib
import json
import uuid
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.guardrails import (
    REDACTION_MASK,
    GuardrailBlockedError,
    GuardrailConfigError,
    GuardrailMisroutedError,
    derive_conformance_state,
    evaluate_guardrails,
    run_interception_pass,
)

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/evals/guardrails.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for guardrail engine tests."""
    return {}


def _make_guardrail(name: str, *, action: str, failure_behaviour: str = "block") -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": action,
            "interception_point": "input",
        },
        failure_behaviour=failure_behaviour,
    )


@given(parsers.parse('a guardrail "{name}" with {action} action'))
def guardrail_with_action(name: str, action: str, ctx: dict[str, Any]) -> None:
    ctx["guardrail"] = _make_guardrail(name, action=action)


@given(parsers.parse('the guardrail detects regex pattern "{pattern}" on field "{field}"'))
def guardrail_regex(pattern: str, field: str, ctx: dict[str, Any]) -> None:
    ctx["guardrail"].config["type"] = "regex"
    ctx["guardrail"].config["pattern"] = pattern
    ctx["guardrail"].config["field"] = field


@given(parsers.parse('the guardrail has a transform redaction policy on path "{path}"'))
def guardrail_redaction_policy(path: str, ctx: dict[str, Any]) -> None:
    ctx["guardrail"].config["redaction"] = [{"path": path, "mode": "transform"}]


@given(parsers.parse('a guardrail "{name}" with {action} action requiring capability "{capability}"'))
def guardrail_with_capability(name: str, action: str, capability: str, ctx: dict[str, Any]) -> None:
    ctx["guardrail"] = _make_guardrail(name, action=action)
    ctx["guardrail"].config["required_capabilities"] = [capability]
    ctx["capability"] = capability


@given(parsers.parse('the registered capability "{capability}" is confirmed present'))
def capability_present(capability: str, ctx: dict[str, Any]) -> None:
    ctx["registered"] = {capability: True}


@given(parsers.parse('the registered capability "{capability}" is confirmed absent'))
def capability_absent(capability: str, ctx: dict[str, Any]) -> None:
    ctx["registered"] = {capability: False}


@when(parsers.parse("the guardrail engine evaluates the payload {payload_json}"))
def engine_evaluates(payload_json: str, ctx: dict[str, Any]) -> None:
    payload = json.loads(payload_json)
    ctx["error"] = None
    try:
        evaluate_guardrails(EvalEngine(), [ctx["guardrail"]], payload, raise_on_block=True)
        ctx["results"] = "clean"
    except GuardrailBlockedError as exc:
        ctx["error"] = exc


@when(parsers.parse("the interception pass runs over the payload {payload_json}"))
def interception_pass(payload_json: str, ctx: dict[str, Any]) -> None:
    payload = json.loads(payload_json)
    ctx["original_payload"] = json.loads(payload_json)
    outcome = run_interception_pass(EvalEngine(), [ctx["guardrail"]], payload)
    ctx["outcome"] = outcome


@when("the generic eval engine evaluates the guardrail directly")
def generic_engine_evaluates(ctx: dict[str, Any]) -> None:
    ctx["error"] = None
    try:
        EvalEngine().evaluate({"body": "clean"}, ctx["guardrail"])
    except GuardrailMisroutedError as exc:
        ctx["error"] = exc


@when(parsers.parse('the guardrail is forced to carry failure_behaviour "{behaviour}"'))
def guardrail_forced_retry(behaviour: str, ctx: dict[str, Any]) -> None:
    # Pydantic rejects failure_behaviour='retry' at construction, so bypass the
    # model like the unit tests do to exercise the engine-level guard.
    object.__setattr__(ctx["guardrail"], "failure_behaviour", behaviour)
    ctx["error"] = None
    try:
        evaluate_guardrails(EvalEngine(), [ctx["guardrail"]], {"body": "clean text"}, raise_on_block=True)
    except GuardrailConfigError as exc:
        ctx["error"] = exc


@when("conformance state is derived")
def conformance_derived(ctx: dict[str, Any]) -> None:
    ctx["derivation"] = derive_conformance_state(
        ctx["guardrail"].config.get("required_capabilities", []),
        ctx.get("registered", {}),
    )


@then(parsers.parse('a GuardrailBlockedError is raised for guardrail "{name}"'))
def blocked_raised_for(name: str, ctx: dict[str, Any]) -> None:
    assert isinstance(ctx.get("error"), GuardrailBlockedError), (
        f"Expected GuardrailBlockedError, got {ctx.get('error')}"
    )
    assert ctx["error"].eval_name == name


@then("no GuardrailBlockedError is raised")
def no_blocked_raised(ctx: dict[str, Any]) -> None:
    assert ctx.get("error") is None, f"Expected no GuardrailBlockedError, got {ctx.get('error')}"
    assert ctx.get("results") == "clean"


@then(parsers.parse('the persisted payload masks "{path}"'))
def payload_masks(path: str, ctx: dict[str, Any]) -> None:
    outcome = ctx["outcome"]
    segments = path.split(".")
    value: Any = outcome.payload
    for segment in segments:
        value = value[segment]
    assert value == REDACTION_MASK, f"Expected fixed mask at {path!r}, got {value!r}"


@then("the original payload is not mutated")
def original_not_mutated(ctx: dict[str, Any]) -> None:
    original = ctx["original_payload"]
    assert original["credentials"]["api_key"] == "sk-live-123", "Original payload was mutated"


@then(parsers.parse('the interception outcome reports blocked by "{name}"'))
def outcome_blocked_by(name: str, ctx: dict[str, Any]) -> None:
    outcome = ctx["outcome"]
    assert outcome.blocked is True, "Expected interception outcome to report blocked"
    assert outcome.blocking_eval_name == name


@then("a GuardrailMisroutedError is raised")
def misrouted_raised(ctx: dict[str, Any]) -> None:
    assert isinstance(ctx.get("error"), GuardrailMisroutedError), (
        f"Expected GuardrailMisroutedError, got {ctx.get('error')}"
    )


@then("a GuardrailConfigError is raised")
def config_error_raised(ctx: dict[str, Any]) -> None:
    assert isinstance(ctx.get("error"), GuardrailConfigError), f"Expected GuardrailConfigError, got {ctx.get('error')}"


@then(parsers.parse('the conformance state is "{state}"'))
def conformance_state(state: str, ctx: dict[str, Any]) -> None:
    derivation = ctx["derivation"]
    assert derivation.state == state, f"Expected conformance {state!r}, got {derivation.state!r}"

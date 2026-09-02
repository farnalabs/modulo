"""Versioned, human-authored eval sets.

Why this module exists
----------------------
The built-in eval types are either shape-only or model-mediated:

* ``regex`` / ``json_schema`` only assert the *shape/structure* of an output —
  they cannot tell a *correct* answer from a confidently-wrong one.
* ``llm_judge`` is an LLM grading an LLM. It is a useful *soft* signal, but it
  is circular (LLM-judging-LLM) and vulnerable to eval injection: a payload in
  the agent output can instruct the judge to return a passing score. See the
  guardrail wrapping in ``eval_engine.__init__`` — that mitigates *instruction*
  leakage but cannot guarantee the judge's *correctness* assessment is right.

Human-authored eval sets are the trustworthy path. Each set is a fixed,
versioned artifact: a list of deterministic assertion functions written and
reviewed by a person. They assert *correctness properties* (business rules,
consistency, semantic invariants) that shape checks cannot, and they are not
model-mediated so they cannot be talked into a false pass.

A set is selected at eval time by name (+ optional ``version``) from the
registered ``HUMAN_EVAL_SETS`` registry. The ``eval_engine`` dispatches the
``human_set`` eval type to ``run_human_eval_set``.

Adding a new human-authored set
-------------------------------
1. Write the assertions as pure functions ``(output, config) -> dict`` returning
   ``{"passed": bool, "score": float|None, "detail": str}``. Mark each with a
   stable ``name``.
2. Register it via :func:`register_human_eval_set` (see ``DEMO_CLASSIFICATION_V1``
   below for the canonical example).
3. Bump the version whenever an assertion's semantics change — consumers pin a
   version so a set edit never silently changes a contract.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from modulo.core.eval_engine import EvalResult

# A single assertion: a deterministic, human-written check.
# The signature mirrors the callable contract used by custom_function / llm_judge
# evals so the engine can reuse its result-parsing helpers.
HumanAssertionFn = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class HumanAssertion:
    """One deterministic, human-authored assertion within a set."""

    name: str
    fn: HumanAssertionFn
    description: str = ""


@dataclass
class HumanEvalSet:
    """A versioned collection of human-authored correctness assertions."""

    name: str
    version: str
    description: str
    assertions: list[HumanAssertion] = field(default_factory=list)

    #: Set ``True`` for sets whose assertions are self-checks of this module's
    #: own behaviour (used by tests). Production sets are ``False``.
    internal: bool = False

    @property
    def id(self) -> str:
        """Stable identifier combining name + version."""
        return f"{self.name}@{self.version}"


# name -> HumanEvalSet. Only one version of a given name is active at a time;
# bumping ``version`` means registering a new set under a distinct id and
# pointing consumers at it.
HUMAN_EVAL_SETS: dict[str, HumanEvalSet] = {}


def register_human_eval_set(eval_set: HumanEvalSet) -> None:
    """Register a human-authored eval set so it can be selected by name."""
    if eval_set.name in HUMAN_EVAL_SETS:
        existing = HUMAN_EVAL_SETS[eval_set.name]
        raise ValueError(
            f"Human eval set {eval_set.name!r} already registered "
            f"(existing version {existing.version!r}); register a distinct name "
            f"or a new version under a new name to avoid clobbering."
        )
    HUMAN_EVAL_SETS[eval_set.name] = eval_set


def get_human_eval_set(name: str) -> HumanEvalSet | None:
    """Look up a registered human eval set by name."""
    _ensure_builtin_sets()
    return HUMAN_EVAL_SETS.get(name)


def list_human_eval_sets() -> list[HumanEvalSet]:
    """Return all registered human eval sets (for selection UIs / docs)."""
    _ensure_builtin_sets()
    return list(HUMAN_EVAL_SETS.values())


# ---------------------------------------------------------------------------
# Canonical shipped set: DEMO_CLASSIFICATION_V1
# ---------------------------------------------------------------------------
# A representative agent task: classify a support message into a ``category``
# and ``priority`` and emit JSON. The assertions below are written by a person
# and check *correctness*, not just shape:
#   * the output is valid JSON with the expected keys,
#   * the values are within the allowed enums,
#   * a business-consistency rule holds (a billing issue is never trivially
#     "low" priority, and a technical outage is always "high"),
#   * no hallucinated keys leak into the contract.
# A regex/json_schema eval could confirm the keys exist; only a human-authored
# set can encode the priority/category consistency rule.


def _demo_parse_json(output: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the configured field and parse it as a JSON object.

    Returns a dict with ``ok`` (bool), ``data`` (parsed object or None) and
    ``detail`` so subsequent assertions can assume a parsed payload.
    """
    from modulo.core.eval_engine import EvalEngine

    field_name = config.get("field", "output")
    found, raw = EvalEngine._resolve_output_field(output, field_name)
    if not found or raw is None:
        return {"passed": False, "score": 0.0, "detail": f"field {field_name!r} not found in output"}
    text = str(raw)
    import json

    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        return {"passed": False, "score": 0.0, "detail": f"output is not valid JSON: {exc}"}
    if not isinstance(data, dict):
        return {"passed": False, "score": 0.0, "detail": "parsed JSON is not an object"}
    return {"passed": True, "score": 1.0, "detail": "", "data": data}


def _demo_required_keys(output: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    parsed = _demo_parse_json(output, config)
    if not parsed["passed"]:
        return parsed
    data = parsed["data"]
    required = {"category", "priority"}
    missing = required - set(data.keys())
    if missing:
        return {"passed": False, "score": 0.0, "detail": f"missing required keys: {sorted(missing)}"}
    return {"passed": True, "score": 1.0, "detail": "all required keys present"}


def _demo_category_enum(output: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    parsed = _demo_parse_json(output, config)
    if not parsed["passed"]:
        return parsed
    allowed = {"billing", "technical", "general"}
    value = parsed["data"].get("category")
    if value not in allowed:
        return {
            "passed": False,
            "score": 0.0,
            "detail": f"category {value!r} not in allowed set {sorted(allowed)}",
        }
    return {"passed": True, "score": 1.0, "detail": f"category {value!r} valid"}


def _demo_priority_enum(output: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    parsed = _demo_parse_json(output, config)
    if not parsed["passed"]:
        return parsed
    allowed = {"low", "medium", "high"}
    value = parsed["data"].get("priority")
    if value not in allowed:
        return {
            "passed": False,
            "score": 0.0,
            "detail": f"priority {value!r} not in allowed set {sorted(allowed)}",
        }
    return {"passed": True, "score": 1.0, "detail": f"priority {value!r} valid"}


def _demo_consistency(output: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Human-authored business rule: billing issues are never trivially low,
    and a technical outage is always high priority."""
    parsed = _demo_parse_json(output, config)
    if not parsed["passed"]:
        return parsed
    data = parsed["data"]
    category = data.get("category")
    priority = data.get("priority")
    if category == "billing" and priority == "low":
        return {
            "passed": False,
            "score": 0.0,
            "detail": "consistency violation: billing issue must not be 'low' priority",
        }
    if category == "technical" and priority != "high":
        return {
            "passed": False,
            "score": 0.0,
            "detail": "consistency violation: technical outage must be 'high' priority",
        }
    return {"passed": True, "score": 1.0, "detail": "category/priority consistent"}


def _demo_no_hallucinated_keys(output: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    parsed = _demo_parse_json(output, config)
    if not parsed["passed"]:
        return parsed
    allowed = {"category", "priority", "confidence"}
    extra = set(parsed["data"].keys()) - allowed
    if extra:
        return {"passed": False, "score": 0.0, "detail": f"unexpected keys present: {sorted(extra)}"}
    return {"passed": True, "score": 1.0, "detail": "no hallucinated keys"}


DEMO_CLASSIFICATION_V1 = HumanEvalSet(
    name="demo_classification",
    version="v1",
    description=(
        "Human-authored correctness checks for a support-message classification "
        "agent that emits JSON with category/priority. Encodes semantic "
        "invariants (enum membership + business consistency) that shape-only "
        "checks (regex/json_schema) and injection-prone llm_judge cannot verify."
    ),
    assertions=[
        HumanAssertion("valid_json", _demo_parse_json, "Output field parses as a JSON object."),
        HumanAssertion("required_keys", _demo_required_keys, "Output has category + priority keys."),
        HumanAssertion("category_enum", _demo_category_enum, "category is in {billing, technical, general}."),
        HumanAssertion("priority_enum", _demo_priority_enum, "priority is in {low, medium, high}."),
        HumanAssertion("consistency", _demo_consistency, "Business rule: billing!=low, technical outage==high."),
        HumanAssertion("no_extra_keys", _demo_no_hallucinated_keys, "Only expected keys present."),
    ],
)


def _ensure_builtin_sets() -> None:
    """Register the shipped built-in eval sets on first use.

    Registration is deferred from module import to keep this module free of
    import-time side effects (the architecture test forbids module-level calls
    that execute on import). Calling it is idempotent.
    """
    if DEMO_CLASSIFICATION_V1.name not in HUMAN_EVAL_SETS:
        register_human_eval_set(DEMO_CLASSIFICATION_V1)


def run_human_eval_set(
    name: str,
    output: dict[str, Any],
    *,
    eval_def: Any,
    run_id: Any | None = None,
) -> EvalResult:
    """Run a registered human-authored eval set against *output*.

    Args:
        name: Registered set name (see ``HUMAN_EVAL_SETS``).
        output: Node output dict (same shape handed to other eval types).
        eval_def: The owning ``EvalDefinition`` DTO (supplies id/node/org).
        run_id: Pipeline run ID; a random UUID is generated if omitted.

    Returns:
        An ``EvalResult`` that passes only if *every* assertion in the set
        passes. The ``detail`` lists any failing assertion names.
    """
    from uuid import uuid4

    from modulo.core.eval_engine import _SCORE_FAIL, _SCORE_PASS

    run_id = run_id or uuid4()
    eval_set = get_human_eval_set(name)
    if eval_set is None:
        return EvalResult(
            run_id=run_id,
            node_id=eval_def.node_id or "",
            eval_id=eval_def.id,
            passed=False,
            score=_SCORE_FAIL,
            detail=f"human eval set {name!r} is not registered",
        )

    failures: list[str] = []
    for assertion in eval_set.assertions:
        try:
            raw = assertion.fn(output, getattr(eval_def, "config", {}) or {})
        except Exception as exc:  # a broken assertion must fail loudly, never pass silently
            failures.append(f"{assertion.name}: raised {type(exc).__name__}: {exc}")
            continue
        if not isinstance(raw, dict) or not bool(raw.get("passed", False)):
            detail = "" if not isinstance(raw, dict) else str(raw.get("detail") or "")
            failures.append(f"{assertion.name}: {detail}".rstrip(": "))

    if failures:
        return EvalResult(
            run_id=run_id,
            node_id=eval_def.node_id or "",
            eval_id=eval_def.id,
            passed=False,
            score=_SCORE_FAIL,
            detail=f"human set {eval_set.id} failed: " + "; ".join(failures),
        )
    return EvalResult(
        run_id=run_id,
        node_id=eval_def.node_id or "",
        eval_id=eval_def.id,
        passed=True,
        score=_SCORE_PASS,
        detail=f"human set {eval_set.id} passed ({len(eval_set.assertions)} assertions)",
    )


__all__ = [
    "DEMO_CLASSIFICATION_V1",
    "HUMAN_EVAL_SETS",
    "HumanAssertion",
    "HumanAssertionFn",
    "HumanEvalSet",
    "get_human_eval_set",
    "list_human_eval_sets",
    "register_human_eval_set",
    "run_human_eval_set",
]

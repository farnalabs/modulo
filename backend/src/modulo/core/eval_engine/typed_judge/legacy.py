"""Legacy judge dialect: readable and executable, never creatable (ADR 066 D8).

Before ADR 066 a judge definition was "a prompt in a free-form ``config_json``":
``{field, model_backend_id, instructions...}`` with no dialect marker. Those
rows stay readable and execute exactly as they do today, but they are frozen:

* **Create** requires the typed dialect — ``answer_contract ==
  "typed_questions"`` plus a structurally valid question set. A legacy config
  cannot be created at any of the eight write paths.
* **Update** accepts a legacy config only when it is byte-identical to the
  stored one (canonical JSON comparison). Editing a legacy config is a typed
  422 carrying ``judge_config_legacy_read_only`` and a migration hint.
* **No backfill.** Rewriting a legacy config would change every definition
  checksum and every suite baseline, and would make historical
  ``eval_definition_version`` lie.
* **Residual execution is counted** (``eval_judge.legacy_rows_count``) so the
  read path can be removed when the count reaches zero (hard backstop
  2027-03-31).

This module is API-framework free: it raises :class:`JudgeConfigWriteError`
with a stable ``code``; the HTTP boundary (``eval_definition_write``) translates
it into a 422.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

_log = logging.getLogger(__name__)

#: The dialect marker on a typed judge config.
TYPED_ANSWER_CONTRACT = "typed_questions"

#: Stable 422 codes. ``judge_config_legacy_read_only`` is the ADR-named one.
CODE_TYPED_REQUIRED = "judge_config_typed_required"
CODE_LEGACY_READ_ONLY = "judge_config_legacy_read_only"
CODE_INVALID = "judge_config_invalid"
CODE_VERDICT_KIND = "judge_verdict_kind_unsupported"

#: Remediation hint attached to every legacy-freeze rejection.
MIGRATE_HINT = (
    "Re-send the definition with a typed config_json (answer_contract: "
    '"typed_questions", a non-empty "field" or judge_whole_output: true, a '
    '"questions" list, a "backing" and a "pass_rule") to migrate it in place, or '
    "delete and recreate it. A legacy config cannot be edited as legacy; no "
    "backfill is performed because it would change every definition checksum and "
    "suite baseline (ADR 066 D8)."
)

_QUESTION_TYPES = frozenset({"noul", "choice", "score"})
_BACKING_KINDS = frozenset({"decision_model", "model_backend"})
_VERDICT_KINDS = frozenset({"assessment", "derivation"})

#: The typed dialect's COMPLETE key surface (extra="forbid"). A pasted
#: credential or an unlisted knob is rejected rather than silently stored in a
#: config_json that ``GET /evals/{id}`` returns verbatim.
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "field",
        "context_fields",
        "answer_contract",
        "backing",
        "questions",
        "pass_rule",
        "verdict_kind",
        "cascade",
        "timeouts",
        "judge_whole_output",
    }
)
_ALLOWED_BACKING = frozenset({"kind", "model_backend_id", "vendor", "model_class"})
_ALLOWED_QUESTION = frozenset(
    {
        "id",
        "type",
        "name",
        "instructions",
        "criteria",
        "weight",
        "required",
        "min_pass_confidence",
        "options",
        "passing_keys",
        "option_scores",
        "levels",
        "pass_from",
    }
)
_ALLOWED_OPTION = frozenset({"key", "description"})
_ALLOWED_LEVEL = frozenset({"score", "description"})

#: Any key that could carry a credential. The typed dialect has no credential
#: field at all (credentials live in the vault / model-backend rows, D16), so a
#: match is a rejection, not a redaction.
_CREDENTIAL_KEY = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|credential|bearer|private[_-]?key)",
    re.IGNORECASE,
)


class JudgeConfigWriteError(ValueError):
    """A judge write was rejected with a stable code and a remediation hint."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint or MIGRATE_HINT

    def detail(self) -> dict[str, str]:
        """The 422 ``detail`` payload: code, message and remediation hint."""
        return {"code": self.code, "message": self.message, "hint": self.hint}


_legacy_counter: Any = None


def _get_legacy_counter() -> Any:
    """Lazily create the process-wide legacy-execution counter (idempotent)."""
    global _legacy_counter
    if _legacy_counter is None:
        try:
            from opentelemetry import metrics

            meter = metrics.get_meter_provider().get_meter("modulo.eval_judge", version="0.1.0")
            _legacy_counter = meter.create_counter(
                name="modulo_eval_judge_legacy_rows_count",
                description="Executions of a legacy (pre-ADR-066) judge definition",
                unit="1",
            )
        except Exception:
            # Metrics are best-effort; the structured log below is the durable
            # signal and must never be skipped because OTel is unavailable.
            return None
    return _legacy_counter


def record_legacy_execution(*, eval_id: Any, eval_name: str = "") -> None:
    """Count one execution of a legacy-dialect judge definition."""
    _log.warning(
        "eval_judge.legacy_rows_count",
        extra={"eval_id": str(eval_id), "eval_name": eval_name, "legacy_rows_count": 1},
    )
    counter = _get_legacy_counter()
    if counter is not None:
        try:
            counter.add(1, {"eval_type": "model_judge"})
        except Exception:
            _log.warning("eval_judge.legacy_counter_unavailable", exc_info=True)


def is_typed_judge_config(config: Any) -> bool:
    """True when *config* declares the typed-questions dialect."""
    return isinstance(config, dict) and config.get("answer_contract") == TYPED_ANSWER_CONTRACT


def is_legacy_judge_config(config: Any) -> bool:
    """True when *config* is a judge config with no dialect marker."""
    return isinstance(config, dict) and config.get("answer_contract") != TYPED_ANSWER_CONTRACT


def canonical_json(config: Any) -> str:
    """Canonical serialisation used for the legacy byte-identical check."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)


def _invalid(message: str) -> JudgeConfigWriteError:
    return JudgeConfigWriteError(CODE_INVALID, message, hint="Fix config_json and retry.")


def _reject_unknown_keys(obj: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise _invalid(f"{where} has unsupported key(s) {unknown}; the typed dialect forbids extra keys")


def _reject_credential_keys(obj: Any, where: str) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and _CREDENTIAL_KEY.search(key):
                raise _invalid(
                    f"{where} carries a credential-like key {key!r}; credentials belong in the "
                    "model backend / vault, never in config_json (ADR 066 D16)"
                )
            _reject_credential_keys(value, f"{where}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _reject_credential_keys(value, f"{where}[{index}]")


def _validate_backing(config: dict[str, Any]) -> None:
    backing = config.get("backing")
    if not isinstance(backing, dict):
        raise _invalid("config_json.backing must be an object")
    _reject_unknown_keys(backing, _ALLOWED_BACKING, "config_json.backing")
    kind = backing.get("kind")
    if kind not in _BACKING_KINDS:
        raise _invalid(f"config_json.backing.kind must be one of {sorted(_BACKING_KINDS)} (got {kind!r})")
    if kind == "model_backend":
        # ``model_backend_id`` may be unset on a DRAFT definition (the
        # create-from-run stub has no backend yet). Execution then records a
        # coded ``judge.configuration`` error — never a verdict — so a draft
        # can never silently score.
        # ADR 066 appendix: the decision-model fields are rejected on the
        # model-backend variant.
        for forbidden in ("vendor", "model_class"):
            if forbidden in backing:
                raise _invalid(f"'backing.{forbidden}' is not valid for a model_backend backing")
    if "confidence_kind" in backing:
        raise _invalid("'confidence_kind' is derived at execution and is not a request field")
    if "confidence_kind" in config:
        raise _invalid("'confidence_kind' is derived at execution and is not a config field")


# S3776: one exhaustive typed-question schema validator; every branch is a
# distinct 422 rejection, so splitting it would scatter the schema.
def _validate_question(index: int, question: Any) -> None:  # NOSONAR
    if not isinstance(question, dict):
        raise _invalid(f"config_json.questions[{index}] must be an object")
    _reject_unknown_keys(question, _ALLOWED_QUESTION, f"config_json.questions[{index}]")
    qid = question.get("id")
    if not isinstance(qid, str) or not qid:
        raise _invalid(f"config_json.questions[{index}].id must be a non-empty string")
    qtype = question.get("type")
    if qtype not in _QUESTION_TYPES:
        raise _invalid(
            f"question {qid!r} has an unsupported type {qtype!r} (expected one of {sorted(_QUESTION_TYPES)})"
        )
    weight = question.get("weight", 1.0)
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        raise _invalid(f"question {qid!r} has an invalid weight")
    if not math.isfinite(float(weight)) or weight < 0:
        raise _invalid(f"question {qid!r} has an invalid weight")

    min_pass_confidence = question.get("min_pass_confidence")
    if min_pass_confidence is not None:
        if isinstance(min_pass_confidence, bool) or not isinstance(min_pass_confidence, (int, float)):
            raise _invalid(f"question {qid!r} min_pass_confidence must be a number in [0, 1]")
        if not math.isfinite(float(min_pass_confidence)) or not 0.0 <= float(min_pass_confidence) <= 1.0:
            raise _invalid(f"question {qid!r} min_pass_confidence must be a finite number in [0, 1]")
    if qtype == "choice":
        options = question.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise _invalid(f"choice question {qid!r} requires at least two options")
        for option in options:
            if not isinstance(option, dict):
                raise _invalid(f"choice question {qid!r} options must be objects")
            _reject_unknown_keys(option, _ALLOWED_OPTION, f"question {qid!r} option")
        keys = {opt.get("key") for opt in options}
        if len(keys) < 2 or None in keys:
            raise _invalid(f"choice question {qid!r} options must be objects with unique 'key' values")
        passing = question.get("passing_keys")
        if not isinstance(passing, list) or not set(passing).issubset(keys):
            raise _invalid(f"choice question {qid!r} passing_keys must be a subset of its option keys")
        option_scores = question.get("option_scores")
        if option_scores is not None:
            if not isinstance(option_scores, dict):
                raise _invalid(f"choice question {qid!r} option_scores must be an object")
            for key, value in option_scores.items():
                if key not in keys:
                    raise _invalid(f"choice question {qid!r} option_scores names an unknown option {key!r}")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise _invalid(f"choice question {qid!r} option score for {key!r} must be a number")
                if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                    raise _invalid(f"choice question {qid!r} option score for {key!r} must be in [0, 1]")
    if qtype == "score":
        levels = question.get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            raise _invalid(f"score question {qid!r} requires at least two ordered levels")
        for level in levels:
            if not isinstance(level, dict):
                raise _invalid(f"score question {qid!r} levels must be objects")
            _reject_unknown_keys(level, _ALLOWED_LEVEL, f"question {qid!r} level")
        numeric = [lv.get("score") for lv in levels]
        if len(numeric) < 2:
            raise _invalid(f"score question {qid!r} levels must be objects with numeric 'score' values")
        for n in numeric:
            if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(float(n)):
                raise _invalid(f"score question {qid!r} levels must have finite numeric 'score' values")
        pass_from = question.get("pass_from")
        if pass_from is None:
            raise _invalid(f"score question {qid!r} must declare 'pass_from'")
        if (
            isinstance(pass_from, bool)
            or not isinstance(pass_from, (int, float))
            or not math.isfinite(float(pass_from))
            or not min(float(n) for n in numeric) <= float(pass_from) <= max(float(n) for n in numeric)
        ):
            raise _invalid(f"score question {qid!r} pass_from must be one of its declared level scores")


def _derivation_error() -> JudgeConfigWriteError:
    """Build the D17 rejection for a derivation question set."""
    return JudgeConfigWriteError(
        CODE_VERDICT_KIND,
        "verdict_kind='derivation' is not supported by typed judgment in v1 "
        "(ADR 066 D17): maths, code and logic correctness must use a deterministic "
        "check, custom_function or human_set.",
        hint="Use a deterministic eval (regex/json_schema/custom_function/human_set) for derivation checks.",
    )


# S3776: the single write-chokepoint validator; its ordered rejections ARE the
# documented contract and must stay in one place.
def validate_typed_judge_config(config: Any) -> None:  # NOSONAR
    """Validate the typed dialect's shape at the write chokepoint.

    S1 validates the structural envelope (dialect marker, questions, backing,
    pass rule, verdict kind) so a newly authored definition is executable and
    cannot silently degrade to a prompt blob. S2 layers the full
    discriminated-union schema and builder UI on top; this validator is the
    gate that must not be bypassed in the meantime.
    """
    if not isinstance(config, dict):
        raise _invalid("a model_judge eval requires a config_json object")
    if config.get("answer_contract") != TYPED_ANSWER_CONTRACT:
        # The dialect marker is checked FIRST so a legacy blob is reported as
        # "migrate it" rather than as a list of forbidden extra keys.
        raise JudgeConfigWriteError(
            CODE_TYPED_REQUIRED,
            "new model_judge definitions require the typed-questions dialect "
            f'("answer_contract": "{TYPED_ANSWER_CONTRACT}")',
        )
    _reject_unknown_keys(config, _ALLOWED_TOP_LEVEL, "config_json")
    _reject_credential_keys(config, "config_json")
    if "model_backend_id" in config:
        raise _invalid(
            "config_json.model_backend_id is not a typed-dialect key; put the judge backend on "
            "'backing.model_backend_id' (the only source both call sites read)"
        )
    verdict_kind = config.get("verdict_kind")
    if verdict_kind not in _VERDICT_KINDS:
        raise _invalid(f"config_json.verdict_kind must be one of {sorted(_VERDICT_KINDS)} (got {verdict_kind!r})")
    if verdict_kind == "derivation":
        raise _derivation_error()

    _validate_backing(config)

    questions = config.get("questions")
    if not isinstance(questions, list) or not questions:
        raise _invalid("config_json.questions must be a non-empty list")
    seen: set[str] = set()
    for index, question in enumerate(questions):
        _validate_question(index, question)
        qid = question["id"]
        if qid in seen:
            raise _invalid(f"duplicate question id {qid!r}")
        seen.add(qid)

    pass_rule = config.get("pass_rule")
    if not isinstance(pass_rule, dict):
        raise _invalid("config_json.pass_rule must be an object")
    if pass_rule.get("kind") != "weighted_score_at_least":
        raise _invalid("config_json.pass_rule.kind must be 'weighted_score_at_least'")
    threshold = pass_rule.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise _invalid("config_json.pass_rule.threshold must be a number in [0, 1]")
    if not math.isfinite(float(threshold)) or not 0.0 <= float(threshold) <= 1.0:
        raise _invalid("config_json.pass_rule.threshold must be a finite number in [0, 1]")

    field = config.get("field", "")
    if not isinstance(field, str):
        raise _invalid("config_json.field must be a string")
    whole_output = config.get("judge_whole_output", False)
    if not isinstance(whole_output, bool):
        raise _invalid("config_json.judge_whole_output must be a boolean")
    if not field and not whole_output:
        raise _invalid(
            "config_json.field is required (a dotted path into the output). Whole-output judging "
            "must be explicit with 'judge_whole_output': true — otherwise the judged payload is empty"
        )


def reject_legacy_judge_write(
    *,
    config_json: Any,
    stored_config: Any = None,
    stored_eval_type: str | None = None,
    is_update: bool = False,
    new_eval_type: str = "model_judge",
) -> None:
    """Enforce the create-required / update-immutable legacy rule.

    ``stored_config`` / ``stored_eval_type`` describe the persisted row on an
    update (both ``None`` on a create). A legacy **judge** row may be re-sent
    byte-for-byte (a metadata edit) — including when the update changes the
    eval type away from ``model_judge``, because replacing its config would
    silently discard a frozen judge without a migration. Any write that lands
    ``model_judge`` must carry the typed dialect.
    """
    legacy_judge_row = is_update and stored_eval_type == "model_judge" and is_legacy_judge_config(stored_config)
    if legacy_judge_row:
        # The one legal in-place change is MIGRATING to the typed dialect. A
        # legacy-to-legacy edit is refused; a typed replacement is the repair
        # the remediation hint names.
        if is_typed_judge_config(config_json):
            validate_typed_judge_config(config_json)
            return
        if canonical_json(config_json) != canonical_json(stored_config):
            raise JudgeConfigWriteError(
                CODE_LEGACY_READ_ONLY,
                "this model_judge definition still uses the legacy (pre-ADR-066) "
                "config dialect, which is read-only as legacy",
            )
        return
    if new_eval_type == "model_judge":
        validate_typed_judge_config(config_json)


__all__ = [
    "CODE_INVALID",
    "CODE_LEGACY_READ_ONLY",
    "CODE_TYPED_REQUIRED",
    "CODE_VERDICT_KIND",
    "MIGRATE_HINT",
    "TYPED_ANSWER_CONTRACT",
    "JudgeConfigWriteError",
    "canonical_json",
    "is_legacy_judge_config",
    "is_typed_judge_config",
    "record_legacy_execution",
    "reject_legacy_judge_write",
    "validate_typed_judge_config",
]

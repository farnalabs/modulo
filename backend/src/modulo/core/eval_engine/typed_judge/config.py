"""Typed question set schema (ADR 066 D4) — the write-time contract.

A typed judge definition is a schema-versioned **question set**, not a prompt
blob. This module is the single structural definition of that set:

* a discriminated union on ``type`` for every question (``noul`` | ``choice`` |
  ``score``), with ``extra="forbid"`` at every level, so an unlisted knob or a
  pasted credential is rejected rather than silently stored in a ``config_json``
  that ``GET /evals/{id}`` returns verbatim;
* the ADR's bounds: ``choice`` 2-255 unique option keys, ``score`` 2-10 ordered
  levels, per-question ``weight > 0`` and a resolvable dotted ``field``;
* top level ``answer_contract``, ``field``, ``context_fields``, ``backing``,
  ``questions``, ``pass_rule``, ``verdict_kind``, optional ``cascade`` and
  ``timeouts``.

Two invariants are deliberately NOT schema concerns:

* ``Eval.pass_threshold`` is never read here. It is the OKR suite pass-rate
  alert threshold, not a per-eval score cut (ADR 066 D4); the verdict's cut is
  ``pass_rule.threshold``.
* Question ids are immutable because an update replaces the whole config and
  bumps ``version``; the schema only guarantees they are present and unique.

Validation is pure (it never mutates or normalises the stored dict) and raises
:class:`~modulo.core.eval_engine.typed_judge.errors.JudgeConfigWriteError` with
a stable code, so every write surface gets the same typed 422.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from modulo.core.eval_engine.typed_judge.errors import (
    CODE_TYPED_REQUIRED,
    CODE_VERDICT_KIND,
    TYPED_ANSWER_CONTRACT,
    JudgeConfigWriteError,
    invalid,
    reject_credential_keys,
)

#: Maximum option keys on a ``choice`` question (ADR 066 D4).
MAX_CHOICE_OPTIONS = 255
#: Maximum ordered levels on a ``score`` question (ADR 066 D4).
MAX_SCORE_LEVELS = 10
#: Minimum cardinality for both bounded question types.
MIN_ANSWERS = 2


def _reject_bool(value: Any) -> Any:
    """Reject a bool where a number is required (``True`` is not a weight)."""
    if isinstance(value, bool):
        raise ValueError("expected a number, got a boolean")
    return value


#: A bool is never a number in this schema (``True`` would otherwise coerce to
#: ``1.0`` and silently satisfy a threshold or a weight).
FiniteFloat = Annotated[float, BeforeValidator(_reject_bool), Field(allow_inf_nan=False)]
UnitFloat = Annotated[float, BeforeValidator(_reject_bool), Field(ge=0.0, le=1.0, allow_inf_nan=False)]
PositiveFloat = Annotated[float, BeforeValidator(_reject_bool), Field(gt=0.0, allow_inf_nan=False)]


class _StrictModel(BaseModel):
    """Every level of the question set forbids extra keys (ADR 066 D4)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class QuestionBase(_StrictModel):
    """Fields shared by every question type."""

    id: StrictStr = Field(min_length=1)
    instructions: StrictStr = ""
    criteria: list[StrictStr] = Field(default_factory=list)
    weight: PositiveFloat = 1.0
    required: StrictBool = False
    min_pass_confidence: UnitFloat = 0.5


class NoulQuestion(QuestionBase):
    """Yes/no: derived pass-confidence is ``p(yes)``."""

    type: Literal["noul"]


class ChoiceOption(_StrictModel):
    key: StrictStr = Field(min_length=1)
    description: StrictStr = ""


class ChoiceQuestion(QuestionBase):
    """Pick one declared option: derived pass-confidence is the mass on passing keys."""

    type: Literal["choice"]
    options: list[ChoiceOption] = Field(min_length=MIN_ANSWERS, max_length=MAX_CHOICE_OPTIONS)
    passing_keys: list[StrictStr] = Field(min_length=1)
    option_scores: dict[str, UnitFloat] | None = None

    @model_validator(mode="after")
    def _validate_answer_space(self) -> ChoiceQuestion:
        keys = [option.key for option in self.options]
        if len(set(keys)) != len(keys):
            raise ValueError("choice options must have unique keys")
        unknown_passing = sorted(set(self.passing_keys) - set(keys))
        if unknown_passing:
            raise ValueError(f"passing_keys names unknown option(s) {unknown_passing}")
        if len(set(self.passing_keys)) != len(self.passing_keys):
            raise ValueError("passing_keys must be unique")
        for key in self.option_scores or {}:
            if key not in keys:
                raise ValueError(f"option_scores names an unknown option {key!r}")
        return self


class ScoreLevel(_StrictModel):
    score: FiniteFloat
    description: StrictStr = ""


class ScoreQuestion(QuestionBase):
    """Ordered scale: derived pass-confidence is the mass at or above ``pass_from``."""

    type: Literal["score"]
    levels: list[ScoreLevel] = Field(min_length=MIN_ANSWERS, max_length=MAX_SCORE_LEVELS)
    pass_from: FiniteFloat

    @model_validator(mode="after")
    def _validate_scale(self) -> ScoreQuestion:
        values = [level.score for level in self.levels]
        if len(set(values)) != len(values):
            raise ValueError("score levels must be unique")
        if values != sorted(values):
            raise ValueError("score levels must be ordered ascending")
        if not values[0] <= self.pass_from <= values[-1]:
            raise ValueError("pass_from must be within the declared level scores' range")
        return self


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


class ModelBackendBacking(_StrictModel):
    """A judge backed by an existing model backend (credentials reused, D3).

    ``model_backend_id`` may be absent on a DRAFT definition (the create-from-run
    stub has no backend yet). Execution then records a coded
    ``judge.configuration`` error — never a verdict — so a draft can never
    silently score. The write-time resolution check only runs when it IS set.
    """

    kind: Literal["model_backend"]
    model_backend_id: str | None = None

    @field_validator("model_backend_id")
    @classmethod
    def _uuid_or_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        import uuid as _uuid

        try:
            return str(_uuid.UUID(value))
        except (ValueError, AttributeError) as exc:
            raise ValueError("model_backend_id must be a UUID") from exc


class DecisionModelBacking(_StrictModel):
    """A decision-model backing (registry lands in S3; shape accepted here)."""

    kind: Literal["decision_model"]
    vendor: str = Field(min_length=1)
    model_class: str | None = None


Backing = Annotated[ModelBackendBacking | DecisionModelBacking, Field(discriminator="kind")]


class PassRule(_StrictModel):
    """The aggregate cut, deliberately separate from ``Eval.pass_threshold``."""

    kind: Literal["weighted_score_at_least"] = "weighted_score_at_least"
    threshold: UnitFloat


class CascadeConfig(_StrictModel):
    """Shape validated here; cascade EXECUTION is a later slice (ADR 066 D7/S4)."""

    enabled: StrictBool = False
    on: Literal["derived_pass_confidence"] = "derived_pass_confidence"
    threshold: UnitFloat = 0.9
    escalate_to: ModelBackendBacking


class TimeoutsConfig(_StrictModel):
    """Per-question and per-definition judge deadlines (ADR 066 D10)."""

    per_question_seconds: PositiveFloat = 10.0
    per_definition_seconds: PositiveFloat = 45.0

    @model_validator(mode="after")
    def _definition_covers_question(self) -> TimeoutsConfig:
        if self.per_definition_seconds < self.per_question_seconds:
            raise ValueError("per_definition_seconds must be >= per_question_seconds")
        return self


class TypedJudgeConfig(_StrictModel):
    """The complete stored shape of a typed judge definition."""

    answer_contract: Literal["typed_questions"]
    field: StrictStr = ""
    context_fields: list[StrictStr] = Field(default_factory=list)
    judge_whole_output: StrictBool = False
    backing: Backing
    questions: list[Question] = Field(min_length=1)
    pass_rule: PassRule
    verdict_kind: Literal["assessment"] = "assessment"
    cascade: CascadeConfig | None = None
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)

    @field_validator("context_fields")
    @classmethod
    def _context_fields_shape(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("context_fields must be non-empty dotted paths")
        if len(set(value)) != len(value):
            raise ValueError("context_fields must be unique")
        return value

    @model_validator(mode="after")
    def _validate_judged_payload(self) -> TypedJudgeConfig:
        if self.field and not _is_dotted_path(self.field):
            raise ValueError("field must be a dotted path into the output")
        if not self.field and not self.judge_whole_output:
            raise ValueError(
                "field is required (a dotted path into the output). Whole-output judging must be "
                "explicit with 'judge_whole_output': true — otherwise the judged payload is empty"
            )
        if not self.judge_whole_output and self.field in self.context_fields:
            raise ValueError("context_fields must not repeat the primary 'field'")
        ids = [question.id for question in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate question id")
        return self


def _is_dotted_path(path: str) -> bool:
    """A dotted path is non-empty segments joined by ``.`` (exact-key lookup)."""
    return bool(path) and "" not in path.split(".")


def _derivation_error() -> JudgeConfigWriteError:
    """Build the D17 rejection for a derivation question set."""
    return JudgeConfigWriteError(
        CODE_VERDICT_KIND,
        "verdict_kind='derivation' is not supported by typed judgment in v1 "
        "(ADR 066 D17): maths, code and logic correctness must use a deterministic "
        "check, custom_function or human_set.",
        hint="Use a deterministic eval (regex/json_schema/custom_function/human_set) for derivation checks.",
    )


def _format_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic error list as a single actionable message."""
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "config_json"
        raw_message = error.get("msg")
        message = "invalid value" if raw_message is None else str(raw_message)
        message = message.removeprefix("Value error, ")
        parts.append(f"{location}: {message}")
    return "; ".join(parts)


def validate_typed_judge_config(config: Any) -> None:
    """Validate the typed dialect's complete shape at the write chokepoint.

    Raises :class:`JudgeConfigWriteError`. A missing dialect marker is reported
    as ``judge_config_typed_required`` (so a legacy blob is told to migrate
    rather than shown a list of forbidden keys); ``verdict_kind='derivation'``
    is reported with its own code and explanation (D17); everything else is
    ``judge_config_invalid`` with the failing field path.
    """
    if not isinstance(config, dict):
        raise invalid("a model_judge eval requires a config_json object")
    if config.get("answer_contract") != TYPED_ANSWER_CONTRACT:
        raise JudgeConfigWriteError(
            CODE_TYPED_REQUIRED,
            "new model_judge definitions require the typed-questions dialect "
            f'("answer_contract": "{TYPED_ANSWER_CONTRACT}")',
        )
    if config.get("verdict_kind") == "derivation":
        raise _derivation_error()
    # Credential scan runs before the schema so the rejection message says why
    # this is a security boundary rather than "extra key not permitted".
    reject_credential_keys(config, "config_json")
    try:
        TypedJudgeConfig.model_validate(config)
    except ValidationError as exc:
        raise invalid(_format_validation_error(exc)) from None


def parse_typed_judge_config(config: Any) -> TypedJudgeConfig:
    """Parse a validated config into its typed form (callers validate first)."""
    validate_typed_judge_config(config)
    return TypedJudgeConfig.model_validate(config)


def typed_config_questions(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the raw question dicts of a typed config (empty for a legacy one).

    Execution operates on the stored raw dict so a persisted definition is
    executed exactly as authored (no re-serialisation drift).
    """
    if config.get("answer_contract") != TYPED_ANSWER_CONTRACT:
        return []
    questions = config.get("questions")
    if not isinstance(questions, list):
        return []
    return [dict(question) for question in questions if isinstance(question, dict)]


def timeout_seconds(config: dict[str, Any]) -> tuple[float, float]:
    """Read ``(per_question, per_definition)`` deadlines, with safe defaults.

    The config is stored JSON, so a legacy or hand-edited value is tolerated:
    a non-finite or non-positive number falls back to the schema default rather
    than producing an unbounded deadline.
    """
    defaults = TimeoutsConfig()
    raw = config.get("timeouts")
    if not isinstance(raw, dict):
        return defaults.per_question_seconds, defaults.per_definition_seconds

    def _positive(key: str, fallback: float) -> float:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return fallback
        number = float(value)
        return number if math.isfinite(number) and number > 0 else fallback

    per_question = _positive("per_question_seconds", defaults.per_question_seconds)
    per_definition = _positive("per_definition_seconds", defaults.per_definition_seconds)
    return per_question, max(per_definition, per_question)


__all__ = [
    "MAX_CHOICE_OPTIONS",
    "MAX_SCORE_LEVELS",
    "Backing",
    "CascadeConfig",
    "ChoiceOption",
    "ChoiceQuestion",
    "DecisionModelBacking",
    "ModelBackendBacking",
    "NoulQuestion",
    "PassRule",
    "Question",
    "QuestionBase",
    "ScoreLevel",
    "ScoreQuestion",
    "TimeoutsConfig",
    "TypedJudgeConfig",
    "parse_typed_judge_config",
    "timeout_seconds",
    "typed_config_questions",
    "validate_typed_judge_config",
]

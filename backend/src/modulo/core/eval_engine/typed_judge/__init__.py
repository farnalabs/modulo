"""Typed judge machinery (ADR 066).

S1 exports the verdict contract (``envelope``), the display-composition module
(``naming``) and the legacy-dialect freeze/validation module (``legacy``).
S2 adds the typed question-set schema (``config``), our own model-backed
transport (``transport``) and the hardened sync bridge (``bridge``).
"""

from modulo.core.eval_engine.typed_judge.config import (
    ChoiceQuestion,
    DecisionModelBacking,
    ModelBackendBacking,
    NoulQuestion,
    ScoreQuestion,
    TypedJudgeConfig,
    parse_typed_judge_config,
)
from modulo.core.eval_engine.typed_judge.envelope import (
    JUDGE_VERDICT_SCHEMA_VERSION,
    JudgeConfidenceKind,
    JudgeErrorCode,
    JudgeExecutionError,
    JudgeIdentity,
    JudgeOutcome,
    JudgeVerdict,
    QuestionVerdict,
    aggregate_typed_answers,
    build_typed_judge_prompt,
    error_verdict,
    from_judge_callable_dict,
    verdict_from_typed_response,
)
from modulo.core.eval_engine.typed_judge.errors import (
    CODE_BACKEND_NOT_ACTIVE,
    CODE_BACKEND_NOT_FOUND,
    CODE_BACKEND_NOT_ORG_OWNED,
    CODE_INVALID,
    CODE_LEGACY_READ_ONLY,
    CODE_TYPED_REQUIRED,
    CODE_VERDICT_KIND,
    MIGRATE_HINT,
    TYPED_ANSWER_CONTRACT,
    JudgeConfigWriteError,
)
from modulo.core.eval_engine.typed_judge.legacy import (
    canonical_json,
    is_legacy_judge_config,
    is_typed_judge_config,
    record_legacy_execution,
    reject_legacy_judge_write,
    validate_typed_judge_config,
)
from modulo.core.eval_engine.typed_judge.naming import (
    SYSTEM_ONE_MODEL_LABEL,
    TYPED_QUESTIONS_MECHANISM,
    backing_display,
    confidence_label,
    legacy_judge_display,
    mechanism_label,
)

__all__ = [
    "CODE_BACKEND_NOT_ACTIVE",
    "CODE_BACKEND_NOT_FOUND",
    "CODE_BACKEND_NOT_ORG_OWNED",
    "CODE_INVALID",
    "CODE_LEGACY_READ_ONLY",
    "CODE_TYPED_REQUIRED",
    "CODE_VERDICT_KIND",
    "JUDGE_VERDICT_SCHEMA_VERSION",
    "MIGRATE_HINT",
    "SYSTEM_ONE_MODEL_LABEL",
    "TYPED_ANSWER_CONTRACT",
    "TYPED_QUESTIONS_MECHANISM",
    "ChoiceQuestion",
    "DecisionModelBacking",
    "JudgeConfidenceKind",
    "JudgeConfigWriteError",
    "JudgeErrorCode",
    "JudgeExecutionError",
    "JudgeIdentity",
    "JudgeOutcome",
    "JudgeVerdict",
    "ModelBackendBacking",
    "NoulQuestion",
    "QuestionVerdict",
    "ScoreQuestion",
    "TypedJudgeConfig",
    "aggregate_typed_answers",
    "backing_display",
    "build_typed_judge_prompt",
    "canonical_json",
    "confidence_label",
    "error_verdict",
    "from_judge_callable_dict",
    "is_legacy_judge_config",
    "is_typed_judge_config",
    "legacy_judge_display",
    "mechanism_label",
    "parse_typed_judge_config",
    "record_legacy_execution",
    "reject_legacy_judge_write",
    "validate_typed_judge_config",
    "verdict_from_typed_response",
]

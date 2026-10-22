"""Changed-lines coverage for the ADR 066 S1 typed-judge contract (FAR-2040).

Focused edge-case coverage for the pure validation/aggregation helpers the
feature PR introduced or moved. Each test targets a documented rejection or a
degenerate input; none of them weakens the contract they pin.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import modulo.core.eval_engine.typed_judge.legacy as legacy
from modulo.core.eval_engine.typed_judge import (
    JudgeConfidenceKind,
    JudgeErrorCode,
    JudgeExecutionError,
    JudgeIdentity,
    from_judge_callable_dict,
    verdict_from_typed_response,
)
from modulo.core.eval_engine.typed_judge.envelope import (
    _coerce_questions,
    _coerce_score,
    _finite_float,
)
from modulo.core.eval_engine.typed_judge.legacy import (
    CODE_INVALID,
    CODE_LEGACY_READ_ONLY,
    TYPED_ANSWER_CONTRACT,
    JudgeConfigWriteError,
    record_legacy_execution,
    reject_legacy_judge_write,
    validate_typed_judge_config,
)
from modulo.core.eval_engine.typed_judge.naming import confidence_label


def _base_config() -> dict[str, Any]:
    return {
        "field": "output",
        "answer_contract": TYPED_ANSWER_CONTRACT,
        "backing": {"kind": "model_backend", "model_backend_id": str(uuid.uuid4())},
        "questions": [{"id": "q", "type": "noul", "weight": 1.0, "required": True}],
        "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
        "verdict_kind": "assessment",
    }


class TestFiniteFloat:
    def test_none_is_absent(self) -> None:
        assert _finite_float(None, field="x") is None

    def test_a_non_numeric_is_a_coded_error(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            _finite_float("not-a-number", field="x")

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_non_finite_number_is_a_coded_error(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            _finite_float(float("inf"), field="x")

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestCoercionHelpers:
    def test_coerce_score_drops_a_non_number(self) -> None:
        assert _coerce_score("high") is None

    def test_coerce_questions_absent_is_empty(self) -> None:
        assert not _coerce_questions(None)

    def test_coerce_questions_non_list_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            _coerce_questions({"not": "a list"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_coerce_questions_invalid_item_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            _coerce_questions([{"question_id": "q", "confidence": 5.0}])

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestCallableDictEdges:
    def test_an_explicit_identity_suppresses_the_payload_identity(self) -> None:
        identity = JudgeIdentity(model="claude")

        verdict = from_judge_callable_dict(
            {"passed": True, "judge": {"mechanism": "ignored"}},
            judge=identity,
        )

        assert verdict.judge == identity

    def test_an_invalid_payload_identity_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"passed": True, "judge": {"unexpected": "x"}})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_unknown_outcome_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "maybe", "error_code": JudgeErrorCode.JUDGE_TIMEOUT.value})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_unknown_error_code_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "error", "error_code": "judge.made_up"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_unknown_confidence_kind_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "pass", "confidence_kind": "made_up"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_invalid_verdict_envelope_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "error"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestQuestionAnswerScore:
    def _verdict(self, questions: list[dict[str, Any]], answers: dict[str, Any]):
        return verdict_from_typed_response(
            __import__("json").dumps({"answers": answers}),
            questions=questions,
            pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
        )

    def test_a_choice_question_with_a_non_list_passing_keys_is_configuration(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict(
                [{"id": "q", "type": "choice", "passing_keys": "a", "weight": 1.0}],
                {"q": "a"},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_choice_option_score_is_honoured(self) -> None:
        verdict = self._verdict(
            [
                {
                    "id": "q",
                    "type": "choice",
                    "options": [{"key": "a"}, {"key": "b"}],
                    "passing_keys": ["a"],
                    "option_scores": {"a": 0.9, "b": 0.1},
                    "weight": 1.0,
                }
            ],
            {"q": "a"},
        )

        assert verdict.questions[0].confidence == pytest.approx(0.9)
        assert verdict.outcome.value == "pass"

    def test_a_choice_without_an_option_score_falls_back_to_the_binary_value(self) -> None:
        verdict = self._verdict(
            [
                {
                    "id": "q",
                    "type": "choice",
                    "options": [{"key": "a"}, {"key": "b"}],
                    "passing_keys": ["a"],
                    "option_scores": {"a": 0.9},
                    "weight": 1.0,
                }
            ],
            {"q": "b"},
        )

        assert verdict.questions[0].confidence == pytest.approx(0.0)

    def test_a_non_dict_level_is_ignored_and_can_leave_too_few(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict(
                [
                    {
                        "id": "q",
                        "type": "score",
                        "levels": [{"score": 1}, "not-a-level"],
                        "pass_from": 1,
                        "weight": 1.0,
                    }
                ],
                {"q": 1},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_level_without_a_score_is_skipped(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict(
                [
                    {
                        "id": "q",
                        "type": "score",
                        "levels": [{"score": 1}, {"description": "no score"}],
                        "pass_from": 1,
                        "weight": 1.0,
                    }
                ],
                {"q": 1},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_missing_score_answer_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict(
                [
                    {
                        "id": "q",
                        "type": "score",
                        "levels": [{"score": 1}, {"score": 3}],
                        "pass_from": 1,
                        "weight": 1.0,
                    }
                ],
                {"q": None},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_score_question_without_pass_from_is_configuration(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict(
                [
                    {
                        "id": "q",
                        "type": "score",
                        "levels": [{"score": 1}, {"score": 3}],
                        "weight": 1.0,
                    }
                ],
                {"q": 2},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_an_unsupported_question_type_is_configuration(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict([{"id": "q", "type": "essay", "weight": 1.0}], {"q": "x"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_question_without_an_id_is_configuration(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict([{"type": "noul", "weight": 1.0}], {})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_missing_optional_weight_is_configuration(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict([{"id": "q", "type": "noul", "weight": None}], {"q": True})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_negative_weight_is_configuration(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._verdict([{"id": "q", "type": "noul", "weight": -1.0}], {"q": True})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_zero_weight_question_is_excluded_from_the_mean(self) -> None:
        verdict = self._verdict(
            [{"id": "q", "type": "noul", "weight": 0.0}],
            {"q": True},
        )

        assert verdict.score is None


class TestTypedResponseShape:
    @pytest.mark.parametrize("raw_text", ["[]", '{"answers": 1}'])
    def test_a_response_without_an_answers_object_is_malformed(self, raw_text: str) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                raw_text,
                questions=[{"id": "q", "type": "noul"}],
                pass_rule={"threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestLegacyConfigValidationEdges:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda c: c.update(backing="nope"),
            lambda c: c.update(backing={"kind": "nope"}),
            lambda c: c.update(backing={"kind": "model_backend", "vendor": "Jev"}),
            lambda c: c["backing"].update(confidence_kind="model_self_reported"),
            lambda c: c.update(confidence_kind="model_self_reported"),
            lambda c: c.update(questions=["not-an-object"]),
            lambda c: c["questions"][0].update(id=""),
            lambda c: c["questions"][0].update(type="essay"),
            lambda c: c["questions"][0].update(weight=True),
            lambda c: c["questions"][0].update(weight=float("nan")),
            lambda c: c["questions"][0].update(weight=-1),
            lambda c: c["questions"][0].update(min_pass_confidence="high"),
            lambda c: c["questions"][0].update(min_pass_confidence=float("nan")),
            lambda c: c["questions"][0].update(type="choice", options=[{"key": "a"}], passing_keys=["a"]),
            lambda c: c["questions"][0].update(type="choice", options=[{"key": "a"}, "b"], passing_keys=["a"]),
            lambda c: c["questions"][0].update(type="choice", options=[{"key": "a"}, {"key": "a"}], passing_keys=["a"]),
            lambda c: c["questions"][0].update(type="choice", options=[{"key": "a"}, {"key": "b"}], passing_keys=["c"]),
            lambda c: c["questions"][0].update(
                type="choice",
                options=[{"key": "a"}, {"key": "b"}],
                passing_keys=["a"],
                option_scores="high",
            ),
            lambda c: c["questions"][0].update(
                type="choice",
                options=[{"key": "a"}, {"key": "b"}],
                passing_keys=["a"],
                option_scores={"c": 0.5},
            ),
            lambda c: c["questions"][0].update(
                type="choice",
                options=[{"key": "a"}, {"key": "b"}],
                passing_keys=["a"],
                option_scores={"a": "high"},
            ),
            lambda c: c["questions"][0].update(
                type="choice",
                options=[{"key": "a"}, {"key": "b"}],
                passing_keys=["a"],
                option_scores={"a": 2.0},
            ),
            lambda c: c["questions"][0].update(type="score", levels=[{"score": 1}]),
            lambda c: c["questions"][0].update(type="score", levels=[{"score": 1}, "not-a-level"], pass_from=1),
            lambda c: c["questions"][0].update(type="score", levels=[{"score": 1}, {"score": True}], pass_from=1),
            lambda c: c["questions"][0].update(type="score", levels=[{"score": 1}, {"score": 3}], pass_from="high"),
            lambda c: c.update(verdict_kind="maybe"),
            lambda c: c.update(pass_rule="nope"),
            lambda c: c.update(pass_rule={"kind": "weighted_score_at_least", "threshold": True}),
            lambda c: c.update(pass_rule={"kind": "weighted_score_at_least", "threshold": float("nan")}),
            lambda c: c.update(field=1),
            lambda c: c.update(judge_whole_output="yes"),
            lambda c: c.update(model_backend_id=str(uuid.uuid4())),
        ],
        ids=[
            "backing-not-object",
            "bad-backing-kind",
            "model-backend-forbidden-field",
            "confidence-kind-in-backing",
            "confidence-kind-in-config",
            "question-not-object",
            "empty-question-id",
            "bad-question-type",
            "bool-weight",
            "nan-weight",
            "negative-weight",
            "string-min-pass-confidence",
            "nan-min-pass-confidence",
            "choice-too-few-options",
            "choice-option-not-object",
            "choice-duplicate-keys",
            "choice-bad-passing-keys",
            "choice-option-scores-not-object",
            "choice-option-scores-unknown-key",
            "choice-option-score-not-number",
            "choice-option-score-out-of-range",
            "score-too-few-levels",
            "score-level-not-object",
            "score-level-bool",
            "score-pass-from-not-number",
            "bad-verdict-kind",
            "pass-rule-not-object",
            "bool-threshold",
            "nan-threshold",
            "field-not-string",
            "judge-whole-output-not-bool",
            "top-level-model-backend-id",
        ],
    )
    def test_invalid_shapes_are_rejected(self, mutate) -> None:
        config = _base_config()
        mutate(config)

        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(config)

        assert exc_info.value.code == CODE_INVALID

    def test_a_decision_model_backing_is_accepted(self) -> None:
        config = _base_config()
        config["backing"] = {"kind": "decision_model", "vendor": "Jev", "model_class": "system-1"}

        assert validate_typed_judge_config(config) is None

    def test_a_choice_question_without_option_scores_is_accepted(self) -> None:
        config = _base_config()
        config["questions"] = [
            {
                "id": "tone",
                "type": "choice",
                "options": [{"key": "a"}, {"key": "b"}],
                "passing_keys": ["a"],
            }
        ]

        assert validate_typed_judge_config(config) is None

    def test_choice_option_scores_may_be_empty(self) -> None:
        config = _base_config()
        config["questions"] = [
            {
                "id": "tone",
                "type": "choice",
                "options": [{"key": "a"}, {"key": "b"}],
                "passing_keys": ["a"],
                "option_scores": {},
            }
        ]

        assert validate_typed_judge_config(config) is None

    def test_a_choice_option_score_within_range_is_accepted(self) -> None:
        config = _base_config()
        config["questions"] = [
            {
                "id": "tone",
                "type": "choice",
                "options": [{"key": "a"}, {"key": "b"}],
                "passing_keys": ["a"],
                "option_scores": {"a": 0.75},
            }
        ]

        assert validate_typed_judge_config(config) is None

    def test_a_score_question_with_an_in_range_pass_from_is_accepted(self) -> None:
        config = _base_config()
        config["questions"] = [
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": 1}, {"score": 3}, {"score": 5}],
                "pass_from": 3,
                "weight": 1.0,
            }
        ]

        assert validate_typed_judge_config(config) is None

    def test_a_duplicate_question_id_is_rejected(self) -> None:
        config = _base_config()
        config["questions"] = [
            {"id": "q", "type": "noul"},
            {"id": "q", "type": "noul"},
        ]

        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(config)

        assert "duplicate" in exc_info.value.message

    def test_a_non_dict_config_is_rejected(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config("nope")


class TestLegacyWriteGuardEdges:
    def test_a_non_judge_write_does_not_validate(self) -> None:
        assert reject_legacy_judge_write(config_json={"legacy": True}, new_eval_type="regex") is None

    def test_a_typed_replacement_of_a_legacy_row_is_validated(self) -> None:
        legacy_config = {"field": "output", "model_backend_id": str(uuid.uuid4())}
        typed = _base_config()

        assert (
            reject_legacy_judge_write(
                config_json=typed,
                stored_config=legacy_config,
                stored_eval_type="model_judge",
                is_update=True,
            )
            is None
        )

    def test_an_edited_legacy_row_is_read_only(self) -> None:
        stored = {"field": "output", "model_backend_id": str(uuid.uuid4())}
        edited = {**stored, "instructions": "changed"}

        with pytest.raises(JudgeConfigWriteError) as exc_info:
            reject_legacy_judge_write(
                config_json=edited,
                stored_config=stored,
                stored_eval_type="model_judge",
                is_update=True,
            )

        assert exc_info.value.code == CODE_LEGACY_READ_ONLY


class TestLegacyCounterEdges:
    def test_unavailable_metrics_are_best_effort(self, monkeypatch) -> None:
        from opentelemetry import metrics

        def _boom() -> Any:
            raise RuntimeError("metrics down")

        monkeypatch.setattr(legacy, "_legacy_counter", None)
        monkeypatch.setattr(metrics, "get_meter_provider", _boom)

        record_legacy_execution(eval_id=uuid.uuid4())

    def test_a_failing_counter_is_logged_not_raised(self, monkeypatch) -> None:
        class _FailingCounter:
            def add(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("counter down")

        monkeypatch.setattr(legacy, "_legacy_counter", _FailingCounter())

        record_legacy_execution(eval_id=uuid.uuid4())


def test_an_unknown_confidence_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown judge confidence kind"):
        confidence_label("made-up")

    assert confidence_label("model_self_reported") == "Model self-reported"
    assert JudgeConfidenceKind.UNAVAILABLE.value == "unavailable"

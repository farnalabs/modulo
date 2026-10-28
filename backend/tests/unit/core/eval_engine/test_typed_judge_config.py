"""Typed question-set schema and write-chokepoint validation (ADR 066 S2).

Every invalid shape must be rejected with a stable typed 422 code, ``extra``
must be forbidden at every level, and a referenced judge backend must exist,
be org-owned and be active before a definition can land.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.eval_engine.eval_definition_write import EvalWriteOptions, create_or_update_eval
from modulo.core.eval_engine.typed_judge import (
    CODE_BACKEND_NOT_ACTIVE,
    CODE_BACKEND_NOT_FOUND,
    CODE_BACKEND_NOT_ORG_OWNED,
    CODE_INVALID,
    CODE_VERDICT_KIND,
    JudgeConfigWriteError,
    TypedJudgeConfig,
    parse_typed_judge_config,
    validate_typed_judge_config,
)
from modulo.core.eval_engine.typed_judge.config import (
    MAX_CHOICE_OPTIONS,
    MAX_SCORE_LEVELS,
    ChoiceQuestion,
    NoulQuestion,
    ScoreQuestion,
    timeout_seconds,
)
from modulo.db.models import Base
from modulo.db.models.eval import Eval
from modulo.db.models.model_backend import ModelBackend

BACKEND_ID = str(uuid.uuid4())


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "answer_contract": "typed_questions",
        "field": "output",
        "context_fields": ["scenario.input", "output.sources"],
        "backing": {"kind": "model_backend", "model_backend_id": BACKEND_ID},
        "questions": [
            {
                "id": "grounded",
                "type": "noul",
                "instructions": "Is the answer grounded?",
                "criteria": ["cites a source"],
                "weight": 1.0,
                "required": True,
                "min_pass_confidence": 0.6,
            }
        ],
        "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.6},
        "verdict_kind": "assessment",
    }
    config.update(overrides)
    return config


def _choice(**overrides: Any) -> dict[str, Any]:
    question: dict[str, Any] = {
        "id": "tone",
        "type": "choice",
        "options": [{"key": "neutral", "description": "calm"}, {"key": "informal"}],
        "passing_keys": ["neutral"],
        "option_scores": {"neutral": 1.0, "informal": 0.0},
        "weight": 0.5,
    }
    question.update(overrides)
    return question


def _score(**overrides: Any) -> dict[str, Any]:
    question: dict[str, Any] = {
        "id": "quality",
        "type": "score",
        "levels": [{"score": 1}, {"score": 3}, {"score": 5}],
        "pass_from": 3,
        "weight": 1.0,
    }
    question.update(overrides)
    return question


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Eval.__table__, ModelBackend.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_backend(
    session,
    *,
    org_id: uuid.UUID,
    backend_id: str = BACKEND_ID,
    status: str = "active",
    organisation_id: uuid.UUID | None = None,
) -> ModelBackend:
    row = ModelBackend(
        id=uuid.UUID(backend_id),
        organisation_id=organisation_id or org_id,
        name="judge-backend",
        display_name="Judge backend",
        provider="openai",
        model_id="gpt-4o-mini",
        credentials_ciphertext=b"ciphertext",
        status=status,
        account_id=org_id,
        visibility="org",
        tier="native",
        cost_tracking="enabled",
        currency="USD",
    )
    session.add(row)
    await session.flush()
    return row


class TestQuestionUnion:
    def test_each_question_type_parses_to_its_own_model(self) -> None:
        parsed = parse_typed_judge_config(_config(questions=[_choice(), _score(), _config()["questions"][0]]))

        assert isinstance(parsed.questions[0], ChoiceQuestion)
        assert isinstance(parsed.questions[1], ScoreQuestion)
        assert isinstance(parsed.questions[2], NoulQuestion)

    def test_a_question_needs_a_unique_non_empty_id(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(questions=[_config()["questions"][0], _config()["questions"][0]]))

        assert exc_info.value.code == CODE_INVALID
        assert "duplicate question id" in exc_info.value.message

    def test_a_weight_must_be_positive(self) -> None:
        question = {**_config()["questions"][0], "weight": 0}

        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(questions=[question]))

        assert exc_info.value.code == CODE_INVALID

    def test_a_resolvable_dotted_field_is_required_unless_whole_output(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(field=""))

        validate_typed_judge_config(_config(field="", judge_whole_output=True))

    def test_a_malformed_dotted_field_is_rejected(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(field="scenario..input"))

        assert exc_info.value.code == CODE_INVALID

    def test_context_fields_must_be_unique_and_not_repeat_the_primary_field(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(context_fields=["a", "a"]))
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(context_fields=["output"]))

    def test_extra_keys_are_forbidden_at_every_level(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(unexpected="x"))
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(backing={**_config()["backing"], "unexpected": "x"}))
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(questions=[{**_config()["questions"][0], "unexpected": "x"}]))

    def test_no_vendor_string_outside_the_decision_model_backing(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(vendor="jev"))
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(backing={**_config()["backing"], "vendor": "jev"}))

        validate_typed_judge_config(_config(backing={"kind": "decision_model", "vendor": "jev"}))

    def test_verdict_kind_derivation_is_rejected_with_the_reason(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(verdict_kind="derivation"))

        assert exc_info.value.code == CODE_VERDICT_KIND
        assert "maths, code and logic" in exc_info.value.message

    def test_pass_threshold_is_not_a_field_on_the_question_set(self) -> None:
        """The OKR alert threshold is never a per-eval score cut (D4)."""
        assert "pass_threshold" not in TypedJudgeConfig.model_fields
        with pytest.raises(ValidationError):
            TypedJudgeConfig.model_validate(_config(pass_threshold=0.9))


class TestChoiceBounds:
    def test_two_to_255_unique_keys_are_accepted(self) -> None:
        validate_typed_judge_config(_config(questions=[_choice()]))
        many = [{"key": f"k{index}"} for index in range(MAX_CHOICE_OPTIONS)]
        parsed = parse_typed_judge_config(
            _config(questions=[_choice(options=many, passing_keys=["k0"], option_scores=None)])
        )

        assert isinstance(parsed.questions[0], ChoiceQuestion)
        assert len(parsed.questions[0].options) == MAX_CHOICE_OPTIONS

    @pytest.mark.parametrize(
        "options",
        [
            [{"key": "only"}],
            [{"key": f"k{index}"} for index in range(MAX_CHOICE_OPTIONS + 1)],
            [{"key": "a"}, {"key": "a"}],
        ],
    )
    def test_invalid_option_counts_or_duplicate_keys_are_rejected(self, options: list[dict[str, Any]]) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(questions=[_choice(options=options, passing_keys=["a"])]))

        assert exc_info.value.code == CODE_INVALID

    def test_passing_keys_must_be_a_subset_of_the_options(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(questions=[_choice(passing_keys=["other"])]))

    def test_option_scores_must_name_known_options_and_stay_in_range(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(questions=[_choice(option_scores={"other": 0.5})]))
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(questions=[_choice(option_scores={"neutral": 2.0})]))

    def test_option_scores_are_optional(self) -> None:
        parsed = parse_typed_judge_config(_config(questions=[_choice(option_scores=None)]))

        assert isinstance(parsed.questions[0], ChoiceQuestion)
        assert parsed.questions[0].option_scores is None

    def test_duplicate_passing_keys_are_rejected(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(questions=[_choice(passing_keys=["neutral", "neutral"])]))

        assert "passing_keys must be unique" in exc_info.value.message

    def test_a_non_uuid_model_backend_id_is_rejected(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(backing={"kind": "model_backend", "model_backend_id": "not-a-uuid"}))

        assert exc_info.value.code == CODE_INVALID

    def test_context_fields_must_be_non_empty_strings(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(context_fields=[1]))

        assert exc_info.value.code == CODE_INVALID

    def test_a_blank_context_field_is_rejected(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(context_fields=[" "]))

        assert exc_info.value.code == CODE_INVALID


class TestScoreBounds:
    def test_two_to_ten_ordered_levels_are_accepted(self) -> None:
        validate_typed_judge_config(_config(questions=[_score()]))
        levels = [{"score": index} for index in range(MAX_SCORE_LEVELS)]
        parsed = parse_typed_judge_config(_config(questions=[_score(levels=levels, pass_from=3)]))

        assert isinstance(parsed.questions[0], ScoreQuestion)
        assert len(parsed.questions[0].levels) == MAX_SCORE_LEVELS

    @pytest.mark.parametrize(
        "levels",
        [
            [{"score": 1}],
            [{"score": index} for index in range(MAX_SCORE_LEVELS + 1)],
            [{"score": 1}, {"score": 1}],
            [{"score": 5}, {"score": 1}],
        ],
    )
    def test_invalid_level_counts_uniqueness_or_order_are_rejected(self, levels: list[dict[str, Any]]) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(_config(questions=[_score(levels=levels, pass_from=1)]))

        assert exc_info.value.code == CODE_INVALID

    def test_pass_from_must_be_within_the_level_range(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(questions=[_score(pass_from=99)]))


class TestOptionalSections:
    def test_cascade_and_timeouts_validate(self) -> None:
        config = _config(
            cascade={
                "enabled": True,
                "on": "derived_pass_confidence",
                "threshold": 0.9,
                "escalate_to": {"kind": "model_backend", "model_backend_id": BACKEND_ID},
            },
            timeouts={"per_question_seconds": 2, "per_definition_seconds": 10},
        )

        parsed = parse_typed_judge_config(config)

        assert parsed.cascade is not None
        assert parsed.timeouts.per_definition_seconds == 10

    def test_a_definition_deadline_must_cover_the_question_deadline(self) -> None:
        with pytest.raises(JudgeConfigWriteError):
            validate_typed_judge_config(_config(timeouts={"per_question_seconds": 30, "per_definition_seconds": 5}))

    def test_execution_reads_timeouts_defensively(self) -> None:
        assert timeout_seconds({}) == (10.0, 45.0)
        assert timeout_seconds({"timeouts": {"per_question_seconds": 1, "per_definition_seconds": 2}}) == (1.0, 2.0)
        assert timeout_seconds({"timeouts": {"per_question_seconds": float("nan")}}) == (10.0, 45.0)
        assert timeout_seconds({"timeouts": {"per_question_seconds": 5, "per_definition_seconds": 1}}) == (5.0, 5.0)


class TestBackendResolutionAtTheChokepoint:
    async def _write(self, session, org: uuid.UUID, config: dict[str, Any]) -> Any:
        return await create_or_update_eval(
            session,
            org_id=org,
            account_id=org,
            pipeline_id=uuid.uuid4(),
            node_id=None,
            name="judge",
            eval_type="model_judge",
            config_json=config,
            failure_behaviour="warn",
            pass_threshold=None,
            suite_id=None,
        )

    async def test_an_active_org_backend_is_accepted(self, session) -> None:
        org = uuid.uuid4()
        await _seed_backend(session, org_id=org)

        row = await self._write(session, org, _config())

        assert row.eval_type == "model_judge"

    async def test_a_missing_backend_is_a_typed_422(self, session) -> None:
        org = uuid.uuid4()

        with pytest.raises(HTTPException) as exc_info:
            await self._write(session, org, _config())

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"] == CODE_BACKEND_NOT_FOUND

    async def test_an_inactive_backend_is_a_typed_422(self, session) -> None:
        org = uuid.uuid4()
        await _seed_backend(session, org_id=org, status="disabled")

        with pytest.raises(HTTPException) as exc_info:
            await self._write(session, org, _config())

        assert exc_info.value.detail["error"] == CODE_BACKEND_NOT_ACTIVE

    async def test_another_orgs_backend_is_a_typed_422(self, session) -> None:
        org = uuid.uuid4()
        await _seed_backend(session, org_id=org, organisation_id=uuid.uuid4())

        with pytest.raises(HTTPException) as exc_info:
            await self._write(session, org, _config())

        assert exc_info.value.detail["error"] == CODE_BACKEND_NOT_ORG_OWNED

    async def test_a_cascade_target_is_resolved_too(self, session) -> None:
        org = uuid.uuid4()
        await _seed_backend(session, org_id=org)
        config = _config(
            cascade={
                "enabled": True,
                "escalate_to": {"kind": "model_backend", "model_backend_id": str(uuid.uuid4())},
            }
        )

        with pytest.raises(HTTPException) as exc_info:
            await self._write(session, org, config)

        assert exc_info.value.detail["error"] == CODE_BACKEND_NOT_FOUND

    async def test_a_draft_without_a_backend_is_accepted(self, session) -> None:
        org = uuid.uuid4()
        config = _config(backing={"kind": "model_backend", "model_backend_id": None})

        row = await self._write(session, org, config)

        assert row.eval_type == "model_judge"

    async def test_a_non_judge_write_skips_typed_resolution(self, session) -> None:
        org = uuid.uuid4()

        row = await create_or_update_eval(
            session,
            org_id=org,
            account_id=org,
            pipeline_id=uuid.uuid4(),
            node_id=None,
            name="regex",
            eval_type="regex",
            config_json={"pattern": "x", "field": "output"},
            failure_behaviour="warn",
            pass_threshold=None,
            suite_id=None,
        )

        assert row.eval_type == "regex"

    async def test_an_update_also_resolves_the_backing(self, session) -> None:
        org = uuid.uuid4()
        await _seed_backend(session, org_id=org)
        row = await self._write(session, org, _config())
        other_org_backend = str(uuid.uuid4())
        await _seed_backend(session, org_id=org, backend_id=other_org_backend, organisation_id=uuid.uuid4())

        with pytest.raises(HTTPException) as exc_info:
            await create_or_update_eval(
                session,
                org_id=org,
                account_id=org,
                pipeline_id=row.pipeline_id,
                node_id=None,
                name="judge",
                eval_type="model_judge",
                config_json=_config(backing={"kind": "model_backend", "model_backend_id": other_org_backend}),
                failure_behaviour="warn",
                pass_threshold=None,
                suite_id=None,
                write_options=EvalWriteOptions(existing_eval_id=row.id),
            )

        assert exc_info.value.detail["error"] == CODE_BACKEND_NOT_ORG_OWNED

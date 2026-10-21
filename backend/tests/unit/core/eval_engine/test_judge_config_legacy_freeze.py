"""Legacy judge dialect freeze at the single write chokepoint (ADR 066 D8).

Before ADR 066 a judge was a free-form prompt blob. Those rows stay readable
and executable, but they are frozen: a create requires the typed dialect, and
a stored legacy config is byte-identical-or-rejected on update. No backfill.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.eval_engine.eval_definition_write import EvalWriteOptions, create_or_update_eval
from modulo.core.eval_engine.typed_judge import (
    CODE_INVALID,
    CODE_LEGACY_READ_ONLY,
    CODE_TYPED_REQUIRED,
    JudgeConfigWriteError,
    canonical_json,
    is_legacy_judge_config,
    is_typed_judge_config,
    reject_legacy_judge_write,
    validate_typed_judge_config,
)
from modulo.db.models import Base
from modulo.db.models.eval import Eval

_LEGACY_CONFIG = {"field": "output", "model_backend_id": str(uuid.uuid4()), "instructions": "grade it"}


def _typed_config() -> dict:
    return {
        "field": "output",
        "answer_contract": "typed_questions",
        "backing": {"kind": "model_backend", "model_backend_id": str(uuid.uuid4())},
        "questions": [
            {
                "id": "verdict",
                "type": "noul",
                "instructions": "Is the output acceptable?",
                "criteria": [],
                "weight": 1.0,
                "required": True,
                "min_pass_confidence": 0.5,
            }
        ],
        "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
        "verdict_kind": "assessment",
    }


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Eval.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestDialectDetection:
    def test_dialect_is_the_marker_not_the_absence_of_a_question(self) -> None:
        assert is_typed_judge_config(_typed_config()) is True
        assert is_legacy_judge_config(_typed_config()) is False
        assert is_legacy_judge_config(_LEGACY_CONFIG) is True


class TestWriteGuard:
    def test_create_of_a_legacy_judge_is_rejected(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            reject_legacy_judge_write(config_json=_LEGACY_CONFIG, is_update=False)

        assert exc_info.value.code == CODE_TYPED_REQUIRED

    def test_create_of_a_typed_judge_is_accepted(self) -> None:
        config = _typed_config()

        reject_legacy_judge_write(config_json=config, is_update=False)

        assert is_typed_judge_config(config) is True

    def test_editing_a_stored_legacy_config_is_read_only(self) -> None:
        changed = {**_LEGACY_CONFIG, "instructions": "different rubric"}

        with pytest.raises(JudgeConfigWriteError) as exc_info:
            reject_legacy_judge_write(
                config_json=changed,
                stored_config=_LEGACY_CONFIG,
                stored_eval_type="model_judge",
                is_update=True,
            )

        assert exc_info.value.code == CODE_LEGACY_READ_ONLY
        assert exc_info.value.detail()["hint"]

    def test_resending_a_stored_legacy_config_byte_identically_is_allowed(self) -> None:
        """A metadata edit may re-send the config; key order does not matter."""
        reordered = dict(reversed(list(_LEGACY_CONFIG.items())))

        reject_legacy_judge_write(
            config_json=reordered,
            stored_config=_LEGACY_CONFIG,
            stored_eval_type="model_judge",
            is_update=True,
        )
        assert canonical_json(reordered) == canonical_json(_LEGACY_CONFIG)

    def test_changing_a_legacy_judge_away_from_the_judge_type_is_read_only(self) -> None:
        """Switching the type would discard the frozen config without a migration."""
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            reject_legacy_judge_write(
                config_json={"pattern": "x", "field": "output"},
                stored_config=_LEGACY_CONFIG,
                stored_eval_type="model_judge",
                is_update=True,
                new_eval_type="regex",
            )

        assert exc_info.value.code == CODE_LEGACY_READ_ONLY

    def test_turning_a_non_judge_eval_into_a_judge_requires_the_typed_dialect(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            reject_legacy_judge_write(
                config_json=_LEGACY_CONFIG,
                stored_config={"pattern": "x", "field": "output"},
                stored_eval_type="regex",
                is_update=True,
            )

        assert exc_info.value.code == CODE_TYPED_REQUIRED


class TestTypedShapeValidation:
    @pytest.mark.parametrize(
        ("mutate", "code"),
        [
            (lambda c: c.pop("answer_contract"), CODE_TYPED_REQUIRED),
            (lambda c: c.update(verdict_kind="derivation"), "judge_verdict_kind_unsupported"),
            (lambda c: c.update(questions=[]), CODE_INVALID),
            (lambda c: c.update(pass_rule={"kind": "nope", "threshold": 0.5}), CODE_INVALID),
            (lambda c: c["backing"].update(vendor="jev"), CODE_INVALID),
            (lambda c: c.update(confidence_kind="model_self_reported"), CODE_INVALID),
        ],
    )
    def test_invalid_typed_shapes_are_rejected(self, mutate, code: str) -> None:
        config = _typed_config()
        mutate(config)

        with pytest.raises(JudgeConfigWriteError) as exc_info:
            validate_typed_judge_config(config)

        assert exc_info.value.code == code


class TestChokepoint:
    async def test_create_path_returns_a_typed_422(self, session) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await create_or_update_eval(
                session,
                org_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                pipeline_id=uuid.uuid4(),
                node_id=None,
                name="judge",
                eval_type="model_judge",
                config_json=_LEGACY_CONFIG,
                failure_behaviour="warn",
                pass_threshold=None,
                suite_id=None,
            )

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == CODE_TYPED_REQUIRED

    async def test_update_path_returns_the_legacy_read_only_422(self, session) -> None:
        org = uuid.uuid4()
        row = Eval(
            organisation_id=org,
            pipeline_id=uuid.uuid4(),
            name="legacy-judge",
            eval_type="model_judge",
            config_json=_LEGACY_CONFIG,
            account_id=org,
            version=1,
        )
        session.add(row)
        await session.flush()

        with pytest.raises(HTTPException) as exc_info:
            await create_or_update_eval(
                session,
                org_id=org,
                account_id=org,
                pipeline_id=row.pipeline_id,
                node_id=None,
                name="legacy-judge",
                eval_type="model_judge",
                config_json={**_LEGACY_CONFIG, "instructions": "a new rubric"},
                failure_behaviour="warn",
                pass_threshold=None,
                suite_id=None,
                write_options=EvalWriteOptions(existing_eval_id=row.id),
            )

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == CODE_LEGACY_READ_ONLY

    async def test_update_path_allows_a_typed_config(self, session) -> None:
        org = uuid.uuid4()
        row = Eval(
            organisation_id=org,
            pipeline_id=uuid.uuid4(),
            name="judge",
            eval_type="model_judge",
            config_json=_typed_config(),
            account_id=org,
            version=1,
        )
        session.add(row)
        await session.flush()

        updated = await create_or_update_eval(
            session,
            org_id=org,
            account_id=org,
            pipeline_id=row.pipeline_id,
            node_id=None,
            name="judge",
            eval_type="model_judge",
            config_json=_typed_config(),
            failure_behaviour="warn",
            pass_threshold=None,
            suite_id=None,
            write_options=EvalWriteOptions(existing_eval_id=row.id),
        )

        assert updated.version == 2

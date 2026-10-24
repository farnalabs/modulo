"""Integration tests for guardrail finding resolution (FAR-2028).

These tests mirror the eval tests but focus on guardrail-specific scenarios.
"""

import uuid
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.guardrails.finding_resolver import (
    apply_mute_policy,
    compute_acceptance_rate,
    evaluate_mute_policy,
    resolve_finding,
)
from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult


class TestGuardrailFindingResolutionIntegration:
    """Integration tests for guardrail finding resolution with real database."""

    @pytest.mark.asyncio
    async def test_guardrail_finding_resolution_flow(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test the full guardrail finding resolution flow."""
        # Create a guardrail eval definition
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="SSN Guardrail",
            eval_type="guardrail",
            config_json={
                "action": "block",
                "detection": {"type": "regex", "pattern": "\\d{3}-\\d{2}-\\d{4}", "field": "ssn"},
            },
            account_id=str(test_user),
        )
        db_session.add(eval_def)

        # Create a guardrail finding
        finding = EvalResult(
            id=uuid4(),
            organisation_id=test_org,
            eval_id=eval_def.id,
            suite_run_id=test_suite_run,
            eval_definition_version=1,
            passed=True,  # For guardrail regex, passed=True means pattern matched
            outcome="fail",
            score=1.0,
            detail="SSN pattern matched in field 'ssn'",
            resolution="pending",
        )
        db_session.add(finding)
        await db_session.flush()

        # Resolve the finding
        resolved = await resolve_finding(
            db_session,
            str(finding.id),
            "accepted",
            "Valid SSN detection",
            str(test_user),
            test_org,
        )

        assert resolved.resolution == "accepted"
        assert resolved.resolution_reason == "Valid SSN detection"
        assert resolved.resolved_by == test_user

    @pytest.mark.asyncio
    async def test_guardrail_compute_acceptance_rate(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test acceptance rate computation for guardrail."""
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="API Key Guardrail",
            eval_type="guardrail",
            config_json={"action": "warn"},
            account_id=str(test_user),
        )
        db_session.add(eval_def)

        # Mix of accepted and dismissed
        for i in range(10):
            resolution = "accepted" if i < 3 else "dismissed"
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=(i < 3),
                outcome="pass" if i < 3 else "fail",
                score=0.9 if i < 3 else 0.1,
                detail=f"Finding {i}",
                resolution=resolution,
            )
            db_session.add(finding)
        await db_session.flush()

        rate, total = await compute_acceptance_rate(
            db_session,
            str(eval_def.id),
            test_org,
        )

        assert total == 10
        assert rate == 0.3

    @pytest.mark.asyncio
    async def test_guardrail_mute_policy(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test mute policy for guardrail."""
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Noisy Guardrail",
            eval_type="guardrail",
            config_json={"action": "warn"},
            account_id=str(test_user),
            mute_threshold=0.5,
            mute_min_outcomes=5,
        )
        db_session.add(eval_def)

        # Low acceptance rate
        for i in range(10):
            resolution = "accepted" if i < 1 else "dismissed"
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=(i < 1),
                outcome="pass" if i < 1 else "fail",
                score=0.9 if i < 1 else 0.1,
                detail=f"Finding {i}",
                resolution=resolution,
            )
            db_session.add(finding)
        await db_session.flush()

        mute_eval = await evaluate_mute_policy(
            db_session,
            str(eval_def.id),
            test_org,
        )

        assert mute_eval["should_mute"] is True
        assert mute_eval["acceptance_rate"] == 0.1
        assert mute_eval["total_outcomes"] == 10

        # Apply mute
        result = await apply_mute_policy(
            db_session,
            str(eval_def.id),
            test_org,
            muted_by=str(test_user),
        )

        assert result is not None
        assert eval_def.is_muted is True

    @pytest.mark.asyncio
    async def test_guardrail_superseded_resolution(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test superseded resolution for guardrail."""
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Legacy Guardrail",
            eval_type="guardrail",
            config_json={"action": "block"},
            account_id=str(test_user),
        )
        db_session.add(eval_def)

        finding = EvalResult(
            id=uuid4(),
            organisation_id=test_org,
            eval_id=eval_def.id,
            suite_run_id=test_suite_run,
            eval_definition_version=1,
            passed=True,
            outcome="fail",
            score=1.0,
            detail="Old pattern matched",
            resolution="pending",
        )
        db_session.add(finding)
        await db_session.flush()

        # Resolve as superseded (replaced by newer guardrail)
        resolved = await resolve_finding(
            db_session,
            str(finding.id),
            "superseded",
            "Replaced by improved guardrail v2",
            str(test_user),
            test_org,
        )

        assert resolved.resolution == "superseded"
        assert resolved.resolution_reason == "Replaced by improved guardrail v2"

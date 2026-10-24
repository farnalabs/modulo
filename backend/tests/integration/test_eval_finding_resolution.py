"""Integration tests for eval finding resolution (FAR-2028)."""

import uuid
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval.finding_resolver import (
    apply_mute_policy,
    compute_acceptance_rate,
    evaluate_mute_policy,
    resolve_finding,
    unmute_definition,
)
from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult


class TestEvalFindingResolutionIntegration:
    """Integration tests for eval finding resolution with real database."""

    @pytest.mark.asyncio
    async def test_resolve_finding_flow(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test the full finding resolution flow."""
        # Create an eval definition
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Test Eval",
            eval_type="model_judge",
            config_json={},
            account_id=str(test_user),
        )
        db_session.add(eval_def)

        # Create an eval result (finding)
        finding = EvalResult(
            id=uuid4(),
            organisation_id=test_org,
            eval_id=eval_def.id,
            suite_run_id=test_suite_run,
            eval_definition_version=1,
            passed=False,
            outcome="fail",
            score=0.3,
            detail="Test finding",
            resolution="pending",
        )
        db_session.add(finding)
        await db_session.flush()

        # Resolve the finding
        resolved = await resolve_finding(
            db_session,
            str(finding.id),
            "accepted",
            "Confirmed true positive",
            str(test_user),
            test_org,
        )

        assert resolved.resolution == "accepted"
        assert resolved.resolution_reason == "Confirmed true positive"
        assert resolved.resolved_by == test_user
        assert resolved.resolved_at is not None

    @pytest.mark.asyncio
    async def test_compute_acceptance_rate_integration(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test acceptance rate computation with real data."""
        # Create an eval definition
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Test Eval",
            eval_type="model_judge",
            config_json={},
            account_id=str(test_user),
        )
        db_session.add(eval_def)

        # Create multiple eval results with different resolutions
        for i in range(10):
            resolution = "accepted" if i < 7 else "dismissed"
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=(i < 7),
                outcome="pass" if i < 7 else "fail",
                score=0.8 if i < 7 else 0.2,
                detail=f"Finding {i}",
                resolution=resolution,
                resolution_reason="Test" if resolution != "pending" else None,
            )
            db_session.add(finding)
        await db_session.flush()

        # Compute acceptance rate
        rate, total = await compute_acceptance_rate(
            db_session,
            str(eval_def.id),
            test_org,
        )

        assert total == 10
        assert rate == pytest.approx(0.7)  # 7 accepted out of 10 resolved

    @pytest.mark.asyncio
    async def test_mute_policy_evaluation_integration(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test mute policy evaluation with real data."""
        # Create an eval definition with mute policy
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Test Guardrail",
            eval_type="guardrail",
            config_json={"action": "block"},
            account_id=str(test_user),
            mute_threshold=0.5,
            mute_min_outcomes=5,
        )
        db_session.add(eval_def)

        # Create eval results - low acceptance rate
        for i in range(10):
            resolution = "accepted" if i < 2 else "dismissed"  # 20% acceptance
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=(i < 2),
                outcome="pass" if i < 2 else "fail",
                score=0.9 if i < 2 else 0.1,
                detail=f"Guardrail finding {i}",
                resolution=resolution,
            )
            db_session.add(finding)
        await db_session.flush()

        # Evaluate mute policy
        mute_eval = await evaluate_mute_policy(
            db_session,
            str(eval_def.id),
            test_org,
        )

        assert mute_eval["should_mute"] is True
        assert mute_eval["acceptance_rate"] == pytest.approx(0.2)
        assert mute_eval["total_outcomes"] == 10
        assert mute_eval["threshold"] == 0.5
        assert mute_eval["min_outcomes"] == 5

    @pytest.mark.asyncio
    async def test_apply_mute_policy_integration(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test applying mute policy."""
        # Create an eval definition with mute policy
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Test Guardrail",
            eval_type="guardrail",
            config_json={"action": "block"},
            account_id=str(test_user),
            mute_threshold=0.5,
            mute_min_outcomes=5,
            is_muted=False,
        )
        db_session.add(eval_def)

        # Create eval results - low acceptance rate
        for i in range(10):
            resolution = "accepted" if i < 2 else "dismissed"
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=(i < 2),
                outcome="pass" if i < 2 else "fail",
                score=0.9 if i < 2 else 0.1,
                detail=f"Finding {i}",
                resolution=resolution,
            )
            db_session.add(finding)
        await db_session.flush()

        # Apply mute policy
        result = await apply_mute_policy(
            db_session,
            str(eval_def.id),
            test_org,
            muted_by=str(test_user),
        )

        assert result is not None
        assert eval_def.is_muted is True
        assert eval_def.muted_by == test_user
        assert eval_def.muted_at is not None
        assert eval_def.acceptance_rate == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_apply_mute_policy_insufficient_outcomes(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test mute policy not applied when insufficient outcomes."""
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Test Guardrail",
            eval_type="guardrail",
            config_json={"action": "block"},
            account_id=str(test_user),
            mute_threshold=0.5,
            mute_min_outcomes=20,  # Requires 20 outcomes
            is_muted=False,
        )
        db_session.add(eval_def)

        # Create only 5 findings
        for i in range(5):
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=False,
                outcome="fail",
                score=0.1,
                detail=f"Finding {i}",
                resolution="dismissed",
            )
            db_session.add(finding)
        await db_session.flush()

        result = await apply_mute_policy(
            db_session,
            str(eval_def.id),
            test_org,
        )

        assert result is None  # Not muted
        assert eval_def.is_muted is False

    @pytest.mark.asyncio
    async def test_unmute_definition_integration(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test manually unmuting a definition."""
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Test Guardrail",
            eval_type="guardrail",
            config_json={"action": "block"},
            account_id=str(test_user),
            mute_threshold=0.5,
            mute_min_outcomes=5,
            is_muted=True,
            muted_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            muted_by=str(test_user),
            acceptance_rate=0.2,
        )
        db_session.add(eval_def)

        # Create some new accepted findings
        for i in range(5):
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=True,
                outcome="pass",
                score=0.9,
                detail=f"New finding {i}",
                resolution="accepted",
            )
            db_session.add(finding)
        await db_session.flush()

        # Unmute
        result = await unmute_definition(db_session, str(eval_def.id), test_org)

        assert result is not None
        assert eval_def.is_muted is False
        assert eval_def.muted_at is None
        assert eval_def.muted_by is None
        assert eval_def.acceptance_rate > 0.5  # New acceptance rate


class TestGuardrailFindingResolutionIntegration:
    """Integration tests for guardrail finding resolution (same as eval)."""

    @pytest.mark.asyncio
    async def test_guardrail_finding_resolution(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test guardrail finding resolution flow."""
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

        # Create a guardrail finding (blocked run)
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

        # Resolve as accepted (true positive - SSN was actually present)
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

    @pytest.mark.asyncio
    async def test_guardrail_dismissed_false_positive(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test guardrail finding dismissed as false positive."""
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="API Key Guardrail",
            eval_type="guardrail",
            config_json={
                "action": "block",
                "detection": {"type": "regex", "pattern": "sk_live_[a-zA-Z0-9]{24}", "field": "api_key"},
            },
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
            detail="API key pattern matched",
            resolution="pending",
        )
        db_session.add(finding)
        await db_session.flush()

        # Dismiss as false positive (test key, not real)
        resolved = await resolve_finding(
            db_session,
            str(finding.id),
            "dismissed",
            "Test API key used in development",
            str(test_user),
            test_org,
        )

        assert resolved.resolution == "dismissed"
        assert resolved.resolution_reason == "Test API key used in development"

    @pytest.mark.asyncio
    async def test_guardrail_mute_policy_low_acceptance(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_suite_run: uuid.UUID,
    ):
        """Test guardrail auto-mute when acceptance rate is low."""
        eval_def = Eval(
            id=uuid4(),
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            name="Noisy Guardrail",
            eval_type="guardrail",
            config_json={"action": "warn"},
            account_id=str(test_user),
            mute_threshold=0.3,
            mute_min_outcomes=10,
        )
        db_session.add(eval_def)

        # Create many dismissed findings (low acceptance)
        for i in range(20):
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=False,
                outcome="fail",
                score=0.1,
                detail=f"Noisy finding {i}",
                resolution="dismissed",
            )
            db_session.add(finding)
        # Add a few accepted
        for i in range(2):
            finding = EvalResult(
                id=uuid4(),
                organisation_id=test_org,
                eval_id=eval_def.id,
                suite_run_id=test_suite_run,
                eval_definition_version=1,
                passed=True,
                outcome="pass",
                score=0.9,
                detail=f"Real finding {i}",
                resolution="accepted",
            )
            db_session.add(finding)
        await db_session.flush()

        # Apply mute policy
        result = await apply_mute_policy(
            db_session,
            str(eval_def.id),
            test_org,
            muted_by=str(test_user),
        )

        assert result is not None
        assert eval_def.is_muted is True
        assert eval_def.acceptance_rate == 2 / 22  # ~0.09

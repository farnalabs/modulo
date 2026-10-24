"""Unit tests for eval/guardrail finding resolver (FAR-2028)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.eval.finding_resolver import (
    FindingResolutionError,
    apply_mute_policy,
    compute_acceptance_rate,
    evaluate_mute_policy,
    resolve_finding,
    unmute_definition,
)


class TestResolveFinding:
    """Tests for resolve_finding function."""

    @pytest.mark.asyncio
    async def test_resolve_finding_accepted(self):
        """Test resolving a finding as accepted."""
        mock_session = AsyncMock()
        mock_finding = MagicMock()
        mock_finding.eval_id = "eval-123"
        mock_finding.organisation_id = "org-123"
        mock_finding.resolution = "pending"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_finding
        mock_session.execute.return_value = mock_result

        finding = await resolve_finding(
            mock_session,
            "finding-123",
            "accepted",
            "This is a true positive",
            "550e8400-e29b-41d4-a716-446655440000",  # valid UUID
            "org-123",
        )

        assert finding.resolution == "accepted"
        assert finding.resolution_reason == "This is a true positive"
        assert str(finding.resolved_by) == "550e8400-e29b-41d4-a716-446655440000"
        assert finding.resolved_at is not None
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_finding_dismissed(self):
        """Test resolving a finding as dismissed."""
        mock_session = AsyncMock()
        mock_finding = MagicMock()
        mock_finding.eval_id = "eval-123"
        mock_finding.organisation_id = "org-123"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_finding
        mock_session.execute.return_value = mock_result

        finding = await resolve_finding(
            mock_session,
            "finding-123",
            "dismissed",
            "False positive",
            "550e8400-e29b-41d4-a716-446655440000",
            "org-123",
        )

        assert finding.resolution == "dismissed"
        assert finding.resolution_reason == "False positive"

    @pytest.mark.asyncio
    async def test_resolve_finding_invalid_resolution(self):
        """Test that invalid resolution raises error."""
        mock_session = AsyncMock()

        with pytest.raises(FindingResolutionError) as exc_info:
            await resolve_finding(
                mock_session,
                "finding-123",
                "invalid",
                "Reason",
                "user-456",
                "org-123",
            )

        assert "Invalid resolution" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_resolve_finding_not_found(self):
        """Test that non-existent finding raises error."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(FindingResolutionError) as exc_info:
            await resolve_finding(
                mock_session,
                "finding-123",
                "accepted",
                "Reason",
                "user-456",
                "org-123",
            )

        assert "Finding not found" in str(exc_info.value)


class TestComputeAcceptanceRate:
    """Tests for compute_acceptance_rate function."""

    @pytest.mark.asyncio
    async def test_compute_acceptance_rate_basic(self):
        """Test basic acceptance rate computation."""
        mock_session = AsyncMock()

        # Mock total count
        total_result = MagicMock()
        total_result.scalar.return_value = 10

        # Mock accepted count
        accepted_result = MagicMock()
        accepted_result.scalar.return_value = 7

        mock_session.execute.side_effect = [total_result, accepted_result]

        rate, total = await compute_acceptance_rate(mock_session, "eval-123", "org-123")

        assert rate == pytest.approx(0.7)
        assert total == 10

    @pytest.mark.asyncio
    async def test_compute_acceptance_rate_zero_total(self):
        """Test acceptance rate when no resolved findings."""
        mock_session = AsyncMock()

        total_result = MagicMock()
        total_result.scalar.return_value = 0

        mock_session.execute.return_value = total_result

        rate, total = await compute_acceptance_rate(mock_session, "eval-123", "org-123")

        assert rate == 0.0
        assert total == 0

    @pytest.mark.asyncio
    async def test_compute_acceptance_rate_all_accepted(self):
        """Test acceptance rate when all findings accepted."""
        mock_session = AsyncMock()

        total_result = MagicMock()
        total_result.scalar.return_value = 5

        accepted_result = MagicMock()
        accepted_result.scalar.return_value = 5

        mock_session.execute.side_effect = [total_result, accepted_result]

        rate, total = await compute_acceptance_rate(mock_session, "eval-123", "org-123")

        assert rate == 1.0
        assert total == 5

    @pytest.mark.asyncio
    async def test_compute_acceptance_rate_none_accepted(self):
        """Test acceptance rate when no findings accepted."""
        mock_session = AsyncMock()

        total_result = MagicMock()
        total_result.scalar.return_value = 5

        accepted_result = MagicMock()
        accepted_result.scalar.return_value = 0

        mock_session.execute.side_effect = [total_result, accepted_result]

        rate, total = await compute_acceptance_rate(mock_session, "eval-123", "org-123")

        assert rate == 0.0
        assert total == 5


class TestEvaluateMutePolicy:
    """Tests for evaluate_mute_policy function."""

    @pytest.mark.asyncio
    async def test_evaluate_mute_policy_not_configured(self):
        """Test mute policy when not configured."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.mute_threshold = None
        mock_eval.mute_min_outcomes = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        result = await evaluate_mute_policy(mock_session, "eval-123", "org-123")

        assert result["should_mute"] is False
        assert result["reason"] == "Mute policy not configured"

    @pytest.mark.asyncio
    async def test_evaluate_mute_policy_insufficient_outcomes(self):
        """Test mute policy with insufficient outcomes."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.mute_threshold = 0.5
        mock_eval.mute_min_outcomes = 10

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        # Mock compute_acceptance_rate to return 3 outcomes
        from unittest.mock import patch

        with patch("modulo.core.eval.finding_resolver.compute_acceptance_rate") as mock_compute:
            mock_compute.return_value = (0.3, 3)  # 30% acceptance, 3 outcomes

            result = await evaluate_mute_policy(mock_session, "eval-123", "org-123")

        assert result["should_mute"] is False
        assert result["total_outcomes"] == 3
        assert "Insufficient outcomes" in result["reason"]

    @pytest.mark.asyncio
    async def test_evaluate_mute_policy_should_mute(self):
        """Test mute policy triggers mute."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.mute_threshold = 0.5
        mock_eval.mute_min_outcomes = 5

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        from unittest.mock import patch

        with patch("modulo.core.eval.finding_resolver.compute_acceptance_rate") as mock_compute:
            mock_compute.return_value = (0.3, 10)  # 30% acceptance, 10 outcomes

            result = await evaluate_mute_policy(mock_session, "eval-123", "org-123")

        assert result["should_mute"] is True
        assert result["acceptance_rate"] == pytest.approx(0.3)
        assert result["total_outcomes"] == 10
        assert "below threshold" in result["reason"]

    @pytest.mark.asyncio
    async def test_evaluate_mute_policy_should_not_mute_above_threshold(self):
        """Test mute policy does not mute when above threshold."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.mute_threshold = 0.5
        mock_eval.mute_min_outcomes = 5

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        from unittest.mock import patch

        with patch("modulo.core.eval.finding_resolver.compute_acceptance_rate") as mock_compute:
            mock_compute.return_value = (0.7, 10)  # 70% acceptance, 10 outcomes

            result = await evaluate_mute_policy(mock_session, "eval-123", "org-123")

        assert result["should_mute"] is False
        assert result["acceptance_rate"] == pytest.approx(0.7)
        assert "meets threshold" in result["reason"]


class TestApplyMutePolicy:
    """Tests for apply_mute_policy function."""

    @pytest.mark.asyncio
    async def test_apply_mute_policy_mutes(self):
        """Test applying mute policy mutes the definition."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.is_muted = False
        mock_eval.mute_threshold = 0.5
        mock_eval.mute_min_outcomes = 5

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        from unittest.mock import patch

        with patch("modulo.core.eval.finding_resolver.evaluate_mute_policy") as mock_eval_policy:
            mock_eval_policy.return_value = {
                "should_mute": True,
                "acceptance_rate": 0.3,
                "total_outcomes": 10,
            }

            result = await apply_mute_policy(
                mock_session, "eval-123", "org-123", "550e8400-e29b-41d4-a716-446655440000"
            )

        assert result is not None
        assert mock_eval.is_muted is True
        assert str(mock_eval.muted_by) == "550e8400-e29b-41d4-a716-446655440000"
        assert mock_eval.acceptance_rate == pytest.approx(0.3)
        statement = mock_session.execute.call_args.args[0]
        assert statement._for_update_arg is not None
        mock_session.flush.assert_called()

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_apply_mute_policy_automatic_records_system_audit(self):
        """Automatic transitions record their measured mute decision."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.is_muted = False
        mock_eval.mute_threshold = 0.5
        mock_eval.mute_min_outcomes = 5
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        with (
            patch("modulo.core.eval.finding_resolver.evaluate_mute_policy") as mock_eval_policy,
            patch("modulo.core.eval.mute_policy.record_mute_audit_event") as mock_audit,
        ):
            mock_eval_policy.return_value = {
                "should_mute": True,
                "acceptance_rate": 0.3,
                "total_outcomes": 10,
                "threshold": 0.5,
            }

            result = await apply_mute_policy(mock_session, "eval-123", "org-123", automatic=True)

        assert result is not None
        mock_audit.assert_awaited_once_with(
            mock_session,
            "eval-123",
            "org-123",
            "muted",
            acceptance_rate=0.3,
            threshold=0.5,
            total_outcomes=10,
            actor_source="system",
            automatic=True,
        )

    @pytest.mark.asyncio
    async def test_apply_mute_policy_already_muted(self):
        """Test applying mute policy when already muted."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.is_muted = True

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        result = await apply_mute_policy(mock_session, "eval-123", "org-123")

        assert result is None

    @pytest.mark.asyncio
    async def test_apply_mute_policy_updates_rate_even_when_not_muting(self):
        """Test that acceptance rate is updated even when not muting."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.is_muted = False
        mock_eval.mute_threshold = 0.5
        mock_eval.mute_min_outcomes = 5

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        from unittest.mock import patch

        with patch("modulo.core.eval.finding_resolver.evaluate_mute_policy") as mock_eval_policy:
            mock_eval_policy.return_value = {
                "should_mute": False,
                "acceptance_rate": 0.8,
                "total_outcomes": 10,
            }

            result = await apply_mute_policy(mock_session, "eval-123", "org-123")

        assert result is None
        assert mock_eval.acceptance_rate == pytest.approx(0.8)
        mock_session.flush.assert_called()


class TestUnmuteDefinition:
    """Tests for unmute_definition function."""

    @pytest.mark.asyncio
    async def test_unmute_definition(self):
        """Test unmuting a definition."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.is_muted = True
        mock_eval.acceptance_rate = 0.3

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        from unittest.mock import patch

        with patch("modulo.core.eval.finding_resolver.compute_acceptance_rate") as mock_compute:
            mock_compute.return_value = (0.5, 10)

            result = await unmute_definition(mock_session, "eval-123", "org-123")

        assert result is not None
        assert mock_eval.is_muted is False
        assert mock_eval.muted_at is None
        assert mock_eval.muted_by is None
        assert mock_eval.acceptance_rate == 0.5
        mock_session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_unmute_definition_not_muted(self):
        """Test unmuting a definition that's not muted."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.is_muted = False

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_eval
        mock_session.execute.return_value = mock_result

        result = await unmute_definition(mock_session, "eval-123", "org-123")

        assert result is None

    @pytest.mark.asyncio
    async def test_unmute_definition_not_found(self):
        """Test unmuting a non-existent definition."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await unmute_definition(mock_session, "eval-123", "org-123")

        assert result is None

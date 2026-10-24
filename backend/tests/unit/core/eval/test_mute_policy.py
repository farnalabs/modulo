"""Unit tests for eval/guardrail mute policy (FAR-2028)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.eval.mute_policy import (
    DEFAULT_MUTE_WINDOW_DAYS,
    evaluate_mute_policy_for_definition,
    get_definitions_near_mute_threshold,
    get_muted_definitions,
    record_mute_audit_event,
)


class TestEvaluateMutePolicyForDefinition:
    """Tests for evaluate_mute_policy_for_definition function."""

    @pytest.mark.asyncio
    async def test_evaluate_with_default_window(self):
        """Test evaluation uses default window when not specified."""
        mock_session = AsyncMock()

        with patch("modulo.core.eval.mute_policy.evaluate_mute_policy") as mock_eval:
            mock_eval.return_value = {
                "should_mute": False,
                "acceptance_rate": 0.5,
                "total_outcomes": 10,
            }

            await evaluate_mute_policy_for_definition(mock_session, "eval-123", "org-123")

            mock_eval.assert_called_once_with(mock_session, "eval-123", "org-123", DEFAULT_MUTE_WINDOW_DAYS)

    @pytest.mark.asyncio
    async def test_evaluate_with_custom_window(self):
        """Test evaluation with custom window."""
        mock_session = AsyncMock()

        with patch("modulo.core.eval.mute_policy.evaluate_mute_policy") as mock_eval:
            mock_eval.return_value = {
                "should_mute": False,
                "acceptance_rate": 0.5,
                "total_outcomes": 10,
            }

            await evaluate_mute_policy_for_definition(mock_session, "eval-123", "org-123", window_days=7)

            mock_eval.assert_called_once_with(mock_session, "eval-123", "org-123", 7)


class TestGetMutedDefinitions:
    """Tests for get_muted_definitions function."""

    @pytest.mark.asyncio
    async def test_get_muted_definitions(self):
        """Test getting all muted definitions."""
        mock_session = AsyncMock()
        mock_eval1 = MagicMock()
        mock_eval1.is_muted = True
        mock_eval1.deleted_at = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_eval1]
        mock_session.execute.return_value = mock_result

        result = await get_muted_definitions(mock_session, "org-123")

        assert len(result) == 1
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_muted_definitions_empty(self):
        """Test getting muted definitions when none exist."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        result = await get_muted_definitions(mock_session, "org-123")

        assert result == []


class TestGetDefinitionsNearMuteThreshold:
    """Tests for get_definitions_near_mute_threshold function."""

    @pytest.mark.asyncio
    async def test_get_near_threshold(self):
        """Test getting definitions near mute threshold."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.id = "eval-123"
        mock_eval.name = "Test Eval"
        mock_eval.eval_type = "guardrail"
        mock_eval.is_muted = False
        mock_eval.deleted_at = None
        mock_eval.mute_threshold = 0.5
        mock_eval.mute_min_outcomes = 10

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_eval]
        mock_session.execute.return_value = mock_result

        with patch("modulo.core.eval.mute_policy.evaluate_mute_policy") as mock_eval_policy:
            mock_eval_policy.return_value = {
                "should_mute": False,
                "acceptance_rate": 0.47,
                "total_outcomes": 15,
                "threshold": 0.5,
                "min_outcomes": 10,
            }

            result = await get_definitions_near_mute_threshold(mock_session, "org-123", margin=0.1)

        assert len(result) == 1
        assert result[0]["eval_id"] == "eval-123"
        assert result[0]["acceptance_rate"] == 0.47
        assert result[0]["threshold"] == 0.5
        assert result[0]["distance_from_threshold"] == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_get_near_threshold_excludes_far(self):
        """Test that definitions far from threshold are excluded."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.id = "eval-123"
        mock_eval.name = "Test Eval"
        mock_eval.eval_type = "guardrail"
        mock_eval.is_muted = False
        mock_eval.deleted_at = None
        mock_eval.mute_threshold = 0.5
        mock_eval.mute_min_outcomes = 10

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_eval]
        mock_session.execute.return_value = mock_result

        with patch("modulo.core.eval.mute_policy.evaluate_mute_policy") as mock_eval_policy:
            mock_eval_policy.return_value = {
                "should_mute": False,
                "acceptance_rate": 0.9,
                "total_outcomes": 15,
                "threshold": 0.5,
                "min_outcomes": 10,
            }

            result = await get_definitions_near_mute_threshold(mock_session, "org-123", margin=0.1)

        # 0.9 is far from 0.5 (margin of 0.1 * 0.5 = 0.05)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_near_threshold_excludes_no_policy(self):
        """Test that definitions without mute policy are excluded."""
        mock_session = AsyncMock()
        mock_eval = MagicMock()
        mock_eval.id = "eval-123"
        mock_eval.mute_threshold = None
        mock_eval.mute_min_outcomes = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []  # Query filters out None thresholds
        mock_session.execute.return_value = mock_result

        result = await get_definitions_near_mute_threshold(mock_session, "org-123")

        assert result == []


class TestRecordMuteAuditEvent:
    """Tests for record_mute_audit_event function."""

    @pytest.mark.asyncio
    async def test_record_mute_audit_event(self):
        """Test recording mute audit event."""
        mock_session = AsyncMock()

        with patch("modulo.core.audit_logger.append_audit_event") as mock_append:
            await record_mute_audit_event(
                mock_session,
                "550e8400-e29b-41d4-a716-446655440000",
                "550e8400-e29b-41d4-a716-446655440001",
                "muted",
                acceptance_rate=0.3,
                threshold=0.5,
                total_outcomes=10,
                actor_id="550e8400-e29b-41d4-a716-446655440002",
            )

            mock_append.assert_called_once()
            call_args = mock_append.call_args
            assert call_args.kwargs["event_type"] == "eval_definition_muted"
            assert call_args.kwargs["resource_type"] == "eval_definition"
            assert str(call_args.kwargs["resource_id"]) == "550e8400-e29b-41d4-a716-446655440000"
            assert str(call_args.kwargs["org_id"]) == "550e8400-e29b-41d4-a716-446655440001"
            assert str(call_args.kwargs["actor_user_id"]) == "550e8400-e29b-41d4-a716-446655440002"
            assert call_args.kwargs["payload_json"]["acceptance_rate"] == 0.3
            assert call_args.kwargs["payload_json"]["threshold"] == 0.5
            assert call_args.kwargs["payload_json"]["total_outcomes"] == 10
            assert call_args.kwargs["payload_json"]["action"] == "muted"

    @pytest.mark.asyncio
    async def test_record_unmute_audit_event(self):
        """Test recording unmute audit event."""
        mock_session = AsyncMock()

        with patch("modulo.core.audit_logger.append_audit_event") as mock_append:
            await record_mute_audit_event(
                mock_session,
                "550e8400-e29b-41d4-a716-446655440000",
                "550e8400-e29b-41d4-a716-446655440001",
                "unmuted",
                acceptance_rate=0.7,
                threshold=0.5,
                actor_id="550e8400-e29b-41d4-a716-446655440002",
            )

            call_args = mock_append.call_args
            assert call_args.kwargs["event_type"] == "eval_definition_unmuted"
            assert call_args.kwargs["payload_json"]["action"] == "unmuted"

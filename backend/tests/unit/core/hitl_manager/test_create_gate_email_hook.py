"""Unit tests: FAR-602 gate-fire email dispatch hook in ``HITLManager.create_gate``.

Acceptance: the hook schedules exactly on the fresh-insert path, carries the
gate id as the label, and a raising scheduler never breaks gate creation.
Also pins the explicit overdue-threshold default (30 minutes).
"""

import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.hitl_manager import DEFAULT_OVERDUE_THRESHOLD_MINUTES, HITLManager
from modulo.db.models.hitl_claim import HitlClaim

_ORG = uuid.uuid4()
_RUN = uuid.uuid4()
_PIPELINE = uuid.uuid4()
_GATE = "review-step"


def _session_for_create(existing: HitlClaim | MagicMock | None = None) -> AsyncMock:
    """Session double for create_gate: pre-check SELECT, savepoint flush."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=result)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


class TestCreateGateEmailHook:
    async def test_create_gate_schedules_dispatch_with_gate_label(self) -> None:
        with patch("modulo.core.hitl_email_alerts.schedule_hitl_email_dispatch") as mock_schedule:
            gate = await HITLManager().create_gate(
                _session_for_create(),
                run_id=_RUN,
                gate_id=_GATE,
                pipeline_id=_PIPELINE,
                org_id=_ORG,
            )
        mock_schedule.assert_called_once_with(org_id=_ORG, pipeline_id=_PIPELINE, run_id=_RUN, gate_label=_GATE)
        assert gate is not None

    async def test_idempotent_reentry_does_not_reschedule(self) -> None:
        existing = MagicMock(spec=HitlClaim)
        with patch("modulo.core.hitl_email_alerts.schedule_hitl_email_dispatch") as mock_schedule:
            gate = await HITLManager().create_gate(
                _session_for_create(existing),
                run_id=_RUN,
                gate_id=_GATE,
                pipeline_id=_PIPELINE,
                org_id=_ORG,
            )
        assert gate is existing
        mock_schedule.assert_not_called()

    async def test_schedule_failure_does_not_break_gate_creation(self) -> None:
        with patch(
            "modulo.core.hitl_email_alerts.schedule_hitl_email_dispatch",
            side_effect=RuntimeError("scheduler exploded"),
        ):
            gate = await HITLManager().create_gate(
                _session_for_create(),
                run_id=_RUN,
                gate_id=_GATE,
                pipeline_id=_PIPELINE,
                org_id=_ORG,
            )
        assert gate is not None


class TestOverdueThresholdDefault:
    def test_explicit_default_is_thirty_minutes(self) -> None:
        assert DEFAULT_OVERDUE_THRESHOLD_MINUTES == 30

    def test_overdue_tooling_uses_the_named_default(self) -> None:
        for method_name in ("list_overdue", "count_overdue"):
            signature = inspect.signature(getattr(HITLManager, method_name))
            default = signature.parameters["threshold_minutes"].default
            assert default is DEFAULT_OVERDUE_THRESHOLD_MINUTES

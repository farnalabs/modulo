"""FAR-1175 (ADR 043 S1): unit tests for the autonomy CRUD seams (no DB).

The manual promote/demote surfaces (REST + MCP) mock these seams, so without
these tests the write path that actually persists ``earned_autonomy_level`` and
the owner-team lookup that arms the RBAC gate would never be exercised.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.pipeline import set_earned_autonomy_level
from modulo.db.crud.pipeline_owner import is_account_in_team

pytestmark = pytest.mark.asyncio


class TestSetEarnedAutonomyLevel:
    async def test_writes_level_and_stamps_timestamp(self) -> None:
        pipeline = MagicMock()
        pipeline.earned_autonomy_level = None
        pipeline.earned_autonomy_updated_at = None
        session = AsyncMock()

        with patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)):
            result = await set_earned_autonomy_level(
                session,
                org_id=uuid.uuid4(),
                pipeline_id=uuid.uuid4(),
                level="notify_on_complete",
            )

        assert result is pipeline
        assert pipeline.earned_autonomy_level == "notify_on_complete"
        assert isinstance(pipeline.earned_autonomy_updated_at, datetime)
        session.flush.assert_awaited_once()

    async def test_missing_pipeline_returns_none(self) -> None:
        session = AsyncMock()

        with patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=None)):
            result = await set_earned_autonomy_level(
                session,
                org_id=uuid.uuid4(),
                pipeline_id=uuid.uuid4(),
                level=None,
            )

        assert result is None
        session.flush.assert_not_awaited()


class TestIsAccountInTeam:
    async def test_true_when_membership_row_present(self) -> None:
        result_obj = MagicMock()
        result_obj.first.return_value = (uuid.uuid4(),)
        session = MagicMock()
        session.execute = AsyncMock(return_value=result_obj)

        assert await is_account_in_team(session, account_id=uuid.uuid4(), team_id=uuid.uuid4()) is True

    async def test_false_when_no_membership_row(self) -> None:
        result_obj = MagicMock()
        result_obj.first.return_value = None
        session = MagicMock()
        session.execute = AsyncMock(return_value=result_obj)

        assert await is_account_in_team(session, account_id=uuid.uuid4(), team_id=uuid.uuid4()) is False

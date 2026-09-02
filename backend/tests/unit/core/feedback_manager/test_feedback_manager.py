"""Unit tests for FeedbackManager service."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.feedback_manager import (
    ConcurrentModificationError,
    FeedbackManager,
    FeedbackRecordNotFoundError,
    FeedbackRecordRunNotFoundError,
    InvalidTransitionError,
    ValidationError,
)
from modulo.db.models.feedback_record import FeedbackRecord

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()
_GATE_ID = "gate-1"
_PRODUCING_NODE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.info = {}
    session.add = MagicMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def mgr(mock_session: AsyncMock) -> FeedbackManager:
    return FeedbackManager(mock_session, _ORG_ID)


@pytest.fixture
def sample_record() -> FeedbackRecord:
    r = MagicMock(spec=FeedbackRecord)
    r.id = uuid.uuid4()
    r.organisation_id = _ORG_ID
    r.run_id = _RUN_ID
    r.gate_id = _GATE_ID
    r.account_id = _USER_ID
    r.rejection_reason = "Output did not match requirements"
    r.rejected_output = {"result": "wrong answer"}
    r.producing_node_id = _PRODUCING_NODE_ID
    r.producing_agent_id = uuid.uuid4()
    r.feedback_status = "pending"
    r.feedback_handler_type = "human"
    r.correction_run_id = None
    r.eval_gap = None
    return r


class TestCreateFeedbackRecord:
    async def _dummy_record(self, handler_type: str = "human") -> MagicMock:
        r = MagicMock(spec=FeedbackRecord)
        r.id = uuid.uuid4()
        r.organisation_id = _ORG_ID
        r.run_id = _RUN_ID
        r.feedback_status = "pending" if handler_type == "human" else "correcting"
        r.feedback_handler_type = handler_type
        r.correction_run_id = None
        return r

    async def test_creates_and_returns_record(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        record = await mgr.create_feedback_record(
            run_id=_RUN_ID,
            gate_id=_GATE_ID,
            account_id=_USER_ID,
            rejection_reason="Wrong output",
            rejected_output={"result": "bad"},
            producing_node_id=str(_PRODUCING_NODE_ID),
            producing_agent_id=uuid.uuid4(),
            feedback_handler_type="human",
        )

        assert record.organisation_id == _ORG_ID
        assert record.run_id == _RUN_ID
        assert record.feedback_status == "pending"
        assert record.feedback_handler_type == "human"
        mock_session.add.assert_called_once()
        added = mock_session.add.call_args.args[0]
        assert added.organisation_id == _ORG_ID
        assert added.run_id == _RUN_ID
        assert added.gate_id == _GATE_ID
        assert added.account_id == _USER_ID
        assert added.rejection_reason == "Wrong output"
        assert added.rejected_output == {"result": "bad"}
        assert added.producing_node_id == _PRODUCING_NODE_ID
        assert added.feedback_status == "pending"
        assert added.feedback_handler_type == "human"
        mock_session.flush.assert_called_once()

    @pytest.mark.parametrize(
        "handler_type",
        ["ai_correction", "ai_correction_with_human_review"],
    )
    async def test_creates_with_handler_type(
        self, mock_session: AsyncMock, mgr: FeedbackManager, handler_type: str
    ) -> None:
        dummy = await self._dummy_record(handler_type)
        with (
            patch.object(mgr, "update_status", AsyncMock(return_value=dummy)),
            patch.object(mgr, "spawn_correction_run", AsyncMock(return_value=uuid.uuid4())),
        ):
            record = await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason="Bad output",
                rejected_output={},
                producing_node_id=str(_PRODUCING_NODE_ID),
                feedback_handler_type=handler_type,
            )
        assert record.feedback_handler_type == handler_type

    @pytest.mark.parametrize(
        "handler_type",
        ["ai_correction", "ai_correction_with_human_review"],
    )
    async def test_auto_triggers_correction(
        self, mock_session: AsyncMock, mgr: FeedbackManager, handler_type: str
    ) -> None:
        dummy = await self._dummy_record(handler_type)
        with (
            patch.object(mgr, "update_status", AsyncMock(return_value=dummy)) as mock_update,
            patch.object(mgr, "spawn_correction_run", AsyncMock(return_value=uuid.uuid4())) as mock_spawn,
        ):
            record = await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason="Auto-fix this",
                rejected_output={"result": "bad"},
                producing_node_id=str(_PRODUCING_NODE_ID),
                feedback_handler_type=handler_type,
            )

            mock_update.assert_called_once_with(record.id, "correcting")
            mock_spawn.assert_called_once_with(record.id)

    @pytest.mark.parametrize("reason", ["", "   "])
    async def test_rejects_empty_rejection_reason(
        self, mock_session: AsyncMock, mgr: FeedbackManager, reason: str
    ) -> None:
        with pytest.raises(ValidationError, match="rejection_reason must not be empty"):
            await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason=reason,
                rejected_output={},
                producing_node_id=str(_PRODUCING_NODE_ID),
                feedback_handler_type="human",
            )

    async def test_rejects_oversized_rejection_reason(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        with pytest.raises(ValidationError, match="must not exceed 5000"):
            await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason="x" * 5001,
                rejected_output={},
                producing_node_id=str(_PRODUCING_NODE_ID),
                feedback_handler_type="human",
            )

    async def test_rejects_oversized_rejected_output(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        with pytest.raises(ValidationError, match="must not exceed 100KB"):
            await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason="bad output",
                rejected_output={"data": "x" * 200_000},
                producing_node_id=str(_PRODUCING_NODE_ID),
                feedback_handler_type="human",
            )

    @pytest.mark.parametrize("handler_type", ["", "auto", "ai_correction_human", "HUMAN"])
    async def test_rejects_unknown_handler_type(
        self, mock_session: AsyncMock, mgr: FeedbackManager, handler_type: str
    ) -> None:
        with pytest.raises(ValidationError, match="unknown feedback_handler_type"):
            await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason="bad output",
                rejected_output={},
                producing_node_id=str(_PRODUCING_NODE_ID),
                feedback_handler_type=handler_type,
            )

    async def test_does_not_auto_trigger_for_human_handler(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        with (
            patch.object(mgr, "update_status") as mock_update,
            patch.object(mgr, "spawn_correction_run") as mock_spawn,
        ):
            await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason="Manual review",
                rejected_output={"result": "bad"},
                producing_node_id=str(_PRODUCING_NODE_ID),
                feedback_handler_type="human",
            )

            mock_update.assert_not_called()
            mock_spawn.assert_not_called()


class TestPaginationValidation:
    @pytest.mark.parametrize("page", [0, -1])
    async def test_rejects_page_below_one(self, mock_session: AsyncMock, mgr: FeedbackManager, page: int) -> None:
        with pytest.raises(ValidationError, match="page must be >= 1"):
            await mgr.get_feedback_records(page=page)

    @pytest.mark.parametrize("page_size", [0, -5])
    async def test_rejects_page_size_below_one(
        self, mock_session: AsyncMock, mgr: FeedbackManager, page_size: int
    ) -> None:
        with pytest.raises(ValidationError, match="page_size must be >= 1"):
            await mgr.get_feedback_records(page_size=page_size)

    async def test_rejects_page_size_above_max(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        with pytest.raises(ValidationError, match="page_size must be <= 100"):
            await mgr.get_feedback_records(page_size=101)

    @pytest.mark.parametrize("page", [0, -1])
    async def test_rejects_page_below_one_inbox(self, mock_session: AsyncMock, mgr: FeedbackManager, page: int) -> None:
        with pytest.raises(ValidationError, match="page must be >= 1"):
            await mgr.get_feedback_records_inbox(page=page)

    async def test_rejects_page_size_above_max_inbox(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        with pytest.raises(ValidationError, match="page_size must be <= 100"):
            await mgr.get_feedback_records_inbox(page_size=200)


class TestPaginateUnscopedWarning:
    async def test_warns_when_called_with_no_conditions(
        self, mock_session: AsyncMock, mgr: FeedbackManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_paginate with empty conditions is a tenant-scoping hazard — must log a warning."""
        import logging

        count_result = MagicMock()
        count_result.scalar.return_value = 0
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[count_result, rows_result])

        with caplog.at_level(logging.WARNING, logger="modulo.core.feedback_manager"):
            rows, total = await mgr._paginate([], page=1, page_size=20)

        assert rows == []
        assert total == 0
        assert any("empty conditions" in r.message for r in caplog.records)


class TestGetFeedbackRecords:
    async def _setup_mock(self, mock_session: AsyncMock, items: list, total: int) -> MagicMock:
        mock_result = MagicMock()
        mock_result.scalar.return_value = total
        mock_result.scalars.return_value.all.return_value = items
        mock_session.execute = AsyncMock(return_value=mock_result)
        return mock_result

    async def test_returns_paginated_results(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        await self._setup_mock(mock_session, [sample_record], 1)

        result = await mgr.get_feedback_records(page=1, page_size=20)

        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["page"] == 1
        assert result["page_size"] == 20

    async def test_filters_by_status(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        await self._setup_mock(mock_session, [sample_record], 1)

        result = await mgr.get_feedback_records(status="pending")
        assert result["total"] == 1

    async def test_returns_empty_when_no_records(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        await self._setup_mock(mock_session, [], 0)

        result = await mgr.get_feedback_records()
        assert result["total"] == 0
        assert not result["items"]

    async def test_filters_by_pipeline_id(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        await self._setup_mock(mock_session, [sample_record], 1)

        result = await mgr.get_feedback_records(pipeline_id=uuid.uuid4())
        assert result["total"] == 1
        assert len(result["items"]) == 1

    async def test_applies_tenant_scope_filter(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """Every query must be scoped by organisation_id — no unscoped listing."""
        await self._setup_mock(mock_session, [sample_record], 1)

        await mgr.get_feedback_records()
        await mgr.get_feedback_records_inbox()

        assert mock_session.execute.await_count == 5
        for call in mock_session.execute.await_args_list:
            stmt = str(call.args[0])
            if "SELECT feedback_records" in stmt:
                assert "feedback_records.organisation_id" in stmt, f"Query not org-scoped: {stmt}"


class TestGetFeedbackRecord:
    async def test_returns_record_when_found(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_record
        mock_session.execute = AsyncMock(return_value=mock_result)

        record = await mgr.get_feedback_record(sample_record.id)
        assert record is not None
        assert record.id == sample_record.id

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        record = await mgr.get_feedback_record(uuid.uuid4())
        assert record is None


class TestUpdateStatus:
    async def test_updates_status_successfully(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        updated = MagicMock(spec=FeedbackRecord)
        updated.id = sample_record.id
        updated.feedback_status = "routing"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(side_effect=[fetch_result, update_result])

        record = await mgr.update_status(sample_record.id, "routing")
        assert record is not None
        assert record.feedback_status == "routing"

    async def test_rejects_invalid_transition(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        mock_session.execute = AsyncMock(return_value=fetch_result)

        with pytest.raises(InvalidTransitionError, match="Cannot transition"):
            await mgr.update_status(sample_record.id, "nonexistent")

    async def test_raises_when_not_found(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=fetch_result)

        with pytest.raises(FeedbackRecordNotFoundError, match="not found"):
            await mgr.update_status(uuid.uuid4(), "routing")

    async def test_raises_on_concurrent_modification(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """The UPDATE affects 0 rows when the status changed concurrently."""
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(side_effect=[fetch_result, update_result])

        with pytest.raises(ConcurrentModificationError, match="status changed concurrently"):
            await mgr.update_status(sample_record.id, "routing")

    async def test_rejects_transition_from_terminal_status(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        sample_record.feedback_status = "resolved"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        mock_session.execute = AsyncMock(return_value=fetch_result)

        with pytest.raises(InvalidTransitionError, match="Cannot transition"):
            await mgr.update_status(sample_record.id, "routing")

    async def test_allows_pending_to_dismissed(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """The dismiss review action sets 'dismissed' (PRD 8.20) — pending may transition there."""
        updated = MagicMock(spec=FeedbackRecord)
        updated.id = sample_record.id
        updated.feedback_status = "dismissed"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(side_effect=[fetch_result, update_result])

        record = await mgr.update_status(sample_record.id, "dismissed")
        assert record.feedback_status == "dismissed"

    async def test_allows_escalated_to_dismissed(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """Escalated records may still be dismissed (PRD 8.20)."""
        sample_record.feedback_status = "escalated"
        updated = MagicMock(spec=FeedbackRecord)
        updated.id = sample_record.id
        updated.feedback_status = "dismissed"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(side_effect=[fetch_result, update_result])

        record = await mgr.update_status(sample_record.id, "dismissed")
        assert record.feedback_status == "dismissed"

    async def test_allows_routing_to_dismissed(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """The inbox shows the dismiss action for every record, and routing records appear
        in the inbox — dismiss must not 409 on them (FAR-233 review MAJOR-2)."""
        sample_record.feedback_status = "routing"
        updated = MagicMock(spec=FeedbackRecord)
        updated.id = sample_record.id
        updated.feedback_status = "dismissed"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(side_effect=[fetch_result, update_result])

        record = await mgr.update_status(sample_record.id, "dismissed")
        assert record.feedback_status == "dismissed"

    async def test_allows_correcting_to_dismissed(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """Correcting records also surface in the inbox with a dismiss button — allow it."""
        sample_record.feedback_status = "correcting"
        updated = MagicMock(spec=FeedbackRecord)
        updated.id = sample_record.id
        updated.feedback_status = "dismissed"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(side_effect=[fetch_result, update_result])

        record = await mgr.update_status(sample_record.id, "dismissed")
        assert record.feedback_status == "dismissed"

    async def test_dismissed_is_terminal(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """Once dismissed, no further transition is allowed (terminal state)."""
        sample_record.feedback_status = "dismissed"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        mock_session.execute = AsyncMock(return_value=fetch_result)

        with pytest.raises(InvalidTransitionError, match="Cannot transition"):
            await mgr.update_status(sample_record.id, "resolved")


class TestLinkCorrectionRun:
    async def test_links_correction_run(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        correction_id = uuid.uuid4()
        updated = MagicMock(spec=FeedbackRecord)
        updated.id = sample_record.id
        updated.correction_run_id = correction_id
        updated.feedback_status = "correcting"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(side_effect=[fetch_result, update_result])

        record = await mgr.link_correction_run(sample_record.id, correction_id)
        assert record is not None
        assert record.correction_run_id == correction_id
        assert record.feedback_status == "correcting"

    async def test_raises_when_not_found(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=fetch_result)

        with pytest.raises(FeedbackRecordNotFoundError, match="not found"):
            await mgr.link_correction_run(uuid.uuid4(), uuid.uuid4())

    async def test_raises_when_correcting_not_allowed(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """Records in terminal status (resolved) cannot be linked to a correction run."""
        sample_record.feedback_status = "resolved"
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        mock_session.execute = AsyncMock(return_value=fetch_result)

        with pytest.raises(InvalidTransitionError, match="Cannot link correction run"):
            await mgr.link_correction_run(sample_record.id, uuid.uuid4())

    async def test_raises_when_already_linked(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        sample_record.correction_run_id = uuid.uuid4()
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        mock_session.execute = AsyncMock(return_value=fetch_result)

        with pytest.raises(ConcurrentModificationError, match="already has a correction run linked"):
            await mgr.link_correction_run(sample_record.id, uuid.uuid4())

    async def test_raises_on_concurrent_modification(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """The guarded UPDATE affects 0 rows when status/correction_run_id changed concurrently."""
        correction_id = uuid.uuid4()
        fetch_result = MagicMock()
        fetch_result.scalar_one_or_none.return_value = sample_record
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(side_effect=[fetch_result, update_result])

        with pytest.raises(ConcurrentModificationError, match="status changed concurrently"):
            await mgr.link_correction_run(sample_record.id, correction_id)


class TestDetectEvalGap:
    async def test_returns_true_when_no_evals(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        mock_session.execute = AsyncMock()

        is_gap = await mgr.detect_eval_gap(sample_record, eval_suite=[])
        assert is_gap is True
        assert sample_record.eval_gap is True

    async def _fake_eval_result(self, passed: bool) -> MagicMock:
        result = MagicMock()
        result.passed = passed
        return result

    async def test_returns_false_when_eval_fails(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """A failing eval means NO gap — the eval caught the failure."""
        mock_eval_engine = MagicMock()
        mock_eval_engine.evaluate = MagicMock(return_value=await self._fake_eval_result(False))

        is_gap = await mgr.detect_eval_gap(
            sample_record,
            eval_engine=mock_eval_engine,
            eval_suite=[{"name": "quality"}],
        )
        assert is_gap is False
        assert sample_record.eval_gap is None
        mock_eval_engine.evaluate.assert_called_once()

    async def test_returns_true_when_eval_passes(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """Every eval passing means an eval gap: none caught the failure."""
        mock_eval_engine = MagicMock()
        mock_eval_engine.evaluate = MagicMock(side_effect=[await self._fake_eval_result(True)])
        sample_record.eval_gap = None

        is_gap = await mgr.detect_eval_gap(
            sample_record,
            eval_engine=mock_eval_engine,
            eval_suite=[{"name": "quality"}, {"name": "completeness"}],
        )
        assert is_gap is True
        assert sample_record.eval_gap is True
        assert mock_eval_engine.evaluate.call_count == 2

    async def test_skips_malformed_eval_defs(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        mock_eval_engine = MagicMock()
        mock_eval_engine.evaluate = MagicMock(return_value=await self._fake_eval_result(True))

        is_gap = await mgr.detect_eval_gap(
            sample_record,
            eval_engine=mock_eval_engine,
            eval_suite=["not-a-dict"],
        )
        assert is_gap is True
        mock_eval_engine.evaluate.assert_not_called()

    async def test_skips_eval_that_raises(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """An eval that raises is treated as inconclusive — the loop continues."""
        mock_eval_engine = MagicMock()
        mock_eval_engine.evaluate = MagicMock(
            side_effect=[
                RuntimeError("eval crashed"),
                await self._fake_eval_result(True),
            ]
        )

        is_gap = await mgr.detect_eval_gap(
            sample_record,
            eval_engine=mock_eval_engine,
            eval_suite=[{"name": "crashy"}, {"name": "fine"}],
        )
        assert is_gap is True
        assert sample_record.eval_gap is True

    async def test_propagates_cancellation(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """asyncio.CancelledError must propagate, not be swallowed by the generic except."""
        import asyncio

        mock_eval_engine = MagicMock()
        mock_eval_engine.evaluate = MagicMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await mgr.detect_eval_gap(
                sample_record,
                eval_engine=mock_eval_engine,
                eval_suite=[{"name": "quality"}],
            )
        assert sample_record.eval_gap is None

    async def test_double_detection_is_idempotent(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """Re-running gap detection on the same record keeps eval_gap True (idempotent read)."""
        mock_eval_engine = MagicMock()
        mock_eval_engine.evaluate = MagicMock(return_value=await self._fake_eval_result(True))

        first = await mgr.detect_eval_gap(
            sample_record,
            eval_engine=mock_eval_engine,
            eval_suite=[{"name": "quality"}],
        )
        assert first is True
        assert sample_record.eval_gap is True

        second = await mgr.detect_eval_gap(
            sample_record,
            eval_engine=mock_eval_engine,
            eval_suite=[{"name": "quality"}],
        )
        assert second is True
        assert sample_record.eval_gap is True

    async def test_uses_real_eval_engine_standalone_path(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """Gap detection exercises the standalone EvalEngine.evaluate() interface against
        the rejected output with real regex eval definitions (PRD 8.20 ¶Eval suite growth)."""
        from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType

        sample_record.rejected_output = {"answer": "the answer is 42"}
        engine = EvalEngine()
        passing_def = EvalDefinition(
            id=uuid.uuid4(),
            org_id=_ORG_ID,
            name="answer-must-mention-42",
            eval_type=EvalType.REGEX,
            config={"pattern": "42", "field": "answer"},
        )

        is_gap = await mgr.detect_eval_gap(sample_record, eval_engine=engine, eval_suite=[passing_def])
        assert is_gap is True
        assert sample_record.eval_gap is True

        failing_record = MagicMock(spec=FeedbackRecord)
        failing_record.id = uuid.uuid4()
        failing_record.organisation_id = _ORG_ID
        failing_record.run_id = _RUN_ID
        failing_record.rejected_output = {"answer": "the answer is 42"}
        failing_record.eval_gap = None

        failing_def = EvalDefinition(
            id=uuid.uuid4(),
            org_id=_ORG_ID,
            name="answer-must-mention-99",
            eval_type=EvalType.REGEX,
            config={"pattern": "99", "field": "answer"},
        )
        no_gap = await mgr.detect_eval_gap(failing_record, eval_engine=engine, eval_suite=[failing_def])
        assert no_gap is False
        assert failing_record.eval_gap is None

    async def test_round_trips_orm_eval_definition_through_detect_gap(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        """The real ORM EvalDefinition shape (config_json, string eval_type) must survive
        detect_eval_gap — EvalEngine.evaluate reads eval_def.config, so the ORM row must
        be normalised to the DTO first, else every eval is swallowed as eval_gap=True
        (FAR-233 review MAJOR-1)."""
        from modulo.core.eval_engine import EvalEngine
        from modulo.db.models.eval_definition import EvalDefinition

        sample_record.rejected_output = {"answer": "the answer is 42"}
        engine = EvalEngine()

        passing_orm = EvalDefinition(
            id=uuid.uuid4(),
            organisation_id=_ORG_ID,
            pipeline_id=uuid.uuid4(),
            name="answer-must-mention-42",
            eval_type="regex",
            config_json={"pattern": "42", "field": "answer"},
            failure_behaviour="warn",
        )
        is_gap = await mgr.detect_eval_gap(sample_record, eval_engine=engine, eval_suite=[passing_orm])
        assert is_gap is True
        assert sample_record.eval_gap is True

        failing_record = MagicMock(spec=FeedbackRecord)
        failing_record.id = uuid.uuid4()
        failing_record.organisation_id = _ORG_ID
        failing_record.run_id = _RUN_ID
        failing_record.rejected_output = {"answer": "the answer is 42"}
        failing_record.eval_gap = None

        failing_orm = EvalDefinition(
            id=uuid.uuid4(),
            organisation_id=_ORG_ID,
            pipeline_id=uuid.uuid4(),
            name="answer-must-mention-99",
            eval_type="regex",
            config_json={"pattern": "99", "field": "answer"},
            failure_behaviour="warn",
        )
        no_gap = await mgr.detect_eval_gap(failing_record, eval_engine=engine, eval_suite=[failing_orm])
        assert no_gap is False
        assert failing_record.eval_gap is None


class TestSpawnCorrectionRun:
    @pytest.fixture
    def original_run(self) -> MagicMock:
        r = MagicMock()
        r.id = uuid.uuid4()
        r.pipeline_id = uuid.uuid4()
        r.snapshot_id = uuid.uuid4()
        r.input_payload = {"user_input": "hello"}
        r.status = "awaiting_human"
        return r

    @pytest.fixture
    def new_run(self) -> MagicMock:
        r = MagicMock()
        r.id = uuid.uuid4()
        return r

    async def test_spawns_correction_run(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        sample_record: FeedbackRecord,
        original_run: MagicMock,
        new_run: MagicMock,
    ) -> None:
        sample_record.run_id = original_run.id

        with (
            patch.object(mgr, "get_feedback_record", return_value=sample_record),
            patch("modulo.core.feedback_manager.get_run", return_value=original_run),
            patch("modulo.core.feedback_manager.create_run", return_value=new_run) as _mock_create_run,
            patch.object(mgr, "link_correction_run", AsyncMock(return_value=sample_record)) as _mock_link,
        ):
            run_id = await mgr.spawn_correction_run(sample_record.id)

        assert run_id == new_run.id
        _mock_create_run.assert_called_once()
        _call_kwargs = _mock_create_run.call_args.kwargs
        assert _call_kwargs["parent_run_id"] == original_run.id
        assert _call_kwargs["trigger_type"] == "correction"
        assert _call_kwargs["pipeline_id"] == original_run.pipeline_id
        assert _call_kwargs["snapshot_id"] == original_run.snapshot_id
        injected = _call_kwargs["feedback_correction"]
        assert injected["rejection_reason"] == sample_record.rejection_reason
        assert injected["rejected_output"] == sample_record.rejected_output
        assert injected["producing_node_id"] == str(sample_record.producing_node_id)
        assert injected["is_correction_run"] is True
        assert "_feedback_correction" not in _call_kwargs["input_payload"]

        _mock_link.assert_called_once_with(sample_record.id, new_run.id)

    async def test_merges_run_context_overrides(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        sample_record: FeedbackRecord,
        original_run: MagicMock,
        new_run: MagicMock,
    ) -> None:
        sample_record.run_id = original_run.id

        with (
            patch.object(mgr, "get_feedback_record", return_value=sample_record),
            patch("modulo.core.feedback_manager.get_run", return_value=original_run),
            patch("modulo.core.feedback_manager.create_run", return_value=new_run) as _mock_create_run,
            patch.object(mgr, "link_correction_run", AsyncMock(return_value=sample_record)),
        ):
            run_id = await mgr.spawn_correction_run(
                sample_record.id,
                run_context_overrides={"custom_key": "custom_value"},
            )

        assert run_id == new_run.id
        _call_kwargs = _mock_create_run.call_args.kwargs
        injected = _call_kwargs["feedback_correction"]
        assert injected["custom_key"] == "custom_value"
        assert injected["rejection_reason"] == sample_record.rejection_reason

    async def test_raises_when_feedback_record_not_found(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
    ) -> None:
        with (
            patch.object(mgr, "get_feedback_record", return_value=None),
            pytest.raises(FeedbackRecordNotFoundError, match=r"FeedbackRecord .* not found"),
        ):
            await mgr.spawn_correction_run(uuid.uuid4())

    async def test_raises_when_correction_run_already_exists(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        sample_record: FeedbackRecord,
    ) -> None:
        sample_record.run_id = uuid.uuid4()
        sample_record.correction_run_id = uuid.uuid4()
        with (
            patch.object(mgr, "get_feedback_record", return_value=sample_record),
            pytest.raises(ConcurrentModificationError, match=r"already has a correction run"),
        ):
            await mgr.spawn_correction_run(sample_record.id)

    async def test_raises_when_original_run_not_found(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        sample_record: FeedbackRecord,
    ) -> None:
        sample_record.run_id = uuid.uuid4()
        with (
            patch.object(mgr, "get_feedback_record", return_value=sample_record),
            patch("modulo.core.feedback_manager.get_run", return_value=None),
            pytest.raises(FeedbackRecordRunNotFoundError, match=r"Original run .* not found"),
        ):
            await mgr.spawn_correction_run(sample_record.id)

    async def test_copies_input_payload(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        sample_record: FeedbackRecord,
        original_run: MagicMock,
        new_run: MagicMock,
    ) -> None:
        sample_record.run_id = original_run.id

        with (
            patch.object(mgr, "get_feedback_record", return_value=sample_record),
            patch("modulo.core.feedback_manager.get_run", return_value=original_run),
            patch("modulo.core.feedback_manager.create_run", return_value=new_run) as _mock_create_run,
            patch.object(mgr, "link_correction_run", AsyncMock(return_value=sample_record)),
        ):
            await mgr.spawn_correction_run(sample_record.id)

        _call_kwargs = _mock_create_run.call_args.kwargs
        payload = _call_kwargs["input_payload"]
        assert payload["user_input"] == "hello"
        assert "_feedback_correction" not in payload
        assert _call_kwargs["feedback_correction"]["is_correction_run"] is True

    async def test_handles_empty_input_payload(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        sample_record: FeedbackRecord,
        original_run: MagicMock,
        new_run: MagicMock,
    ) -> None:
        original_run.input_payload = None
        sample_record.run_id = original_run.id

        with (
            patch.object(mgr, "get_feedback_record", return_value=sample_record),
            patch("modulo.core.feedback_manager.get_run", return_value=original_run),
            patch("modulo.core.feedback_manager.create_run", return_value=new_run) as _mock_create_run,
            patch.object(mgr, "link_correction_run", AsyncMock(return_value=sample_record)),
        ):
            run_id = await mgr.spawn_correction_run(sample_record.id)

        assert run_id == new_run.id
        _call_kwargs = _mock_create_run.call_args.kwargs
        injected = _call_kwargs["feedback_correction"]
        assert injected["rejection_reason"] == sample_record.rejection_reason


class TestGetFeedbackRecordsInbox:
    async def _setup_mock(self, mock_session: AsyncMock, items: list, total: int) -> MagicMock:
        mock_result = MagicMock()
        mock_result.scalar.return_value = total
        mock_result.scalars.return_value.all.return_value = items
        mock_session.execute = AsyncMock(return_value=mock_result)
        return mock_result

    async def test_returns_paginated_inbox(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        await self._setup_mock(mock_session, [sample_record], 1)

        result = await mgr.get_feedback_records_inbox(page=1, page_size=20)

        assert result["total"] == 1
        assert len(result["items"]) == 1

    async def test_filters_by_handler_type(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        await self._setup_mock(mock_session, [sample_record], 1)

        result = await mgr.get_feedback_records_inbox(handler_type="human")

        assert result["total"] == 1

    async def test_returns_empty_when_no_records(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        await self._setup_mock(mock_session, [], 0)

        result = await mgr.get_feedback_records_inbox()
        assert result["total"] == 0
        assert not result["items"]

    async def test_filters_by_status_and_pipeline(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        await self._setup_mock(mock_session, [sample_record], 1)

        result = await mgr.get_feedback_records_inbox(status="pending", pipeline_id=uuid.uuid4())
        assert result["total"] == 1
        assert len(result["items"]) == 1

    async def test_filters_by_date_range(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        from datetime import UTC, datetime

        await self._setup_mock(mock_session, [sample_record], 1)

        result = await mgr.get_feedback_records_inbox(
            date_from=datetime(2026, 1, 1, tzinfo=UTC),
            date_to=datetime(2026, 12, 31, tzinfo=UTC),
        )
        assert result["total"] == 1
        assert len(result["items"]) == 1

    async def test_includes_pipeline_map(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        sample_record.run_id = uuid.uuid4()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [sample_record]
        enrich_result = MagicMock()
        enrich_result.all.return_value = [(sample_record.run_id, "My Pipeline")]
        mock_session.execute = AsyncMock(side_effect=[count_result, rows_result, enrich_result])

        result = await mgr.get_feedback_records_inbox()

        assert result["pipeline_map"] == {str(sample_record.run_id): "My Pipeline"}
        assert len(result["items"]) == 1

    async def test_pipeline_map_empty_when_no_rows(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[count_result, rows_result])

        result = await mgr.get_feedback_records_inbox()

        assert not result["pipeline_map"]
        assert not result["items"]


class TestGetEvalProposals:
    async def _setup_mock(self, mock_session: AsyncMock, items: list, total: int) -> MagicMock:
        mock_result = MagicMock()
        mock_result.scalar.return_value = total
        mock_result.scalars.return_value.all.return_value = items
        mock_session.execute = AsyncMock(return_value=mock_result)
        return mock_result

    async def test_returns_proposals(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        await self._setup_mock(mock_session, [sample_record], 1)

        result = await mgr.get_eval_proposals(page=1, page_size=20)

        assert result["total"] == 1

    async def test_returns_empty_when_no_proposals(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        await self._setup_mock(mock_session, [], 0)

        result = await mgr.get_eval_proposals()
        assert result["total"] == 0
        assert not result["items"]

    async def test_proposal_query_is_scoped_to_eval_gap_and_open_status(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        await self._setup_mock(mock_session, [sample_record], 1)

        await mgr.get_eval_proposals()

        count_stmt = str(mock_session.execute.await_args_list[0].args[0])
        assert "eval_gap" in count_stmt, f"Proposal query missing eval_gap filter: {count_stmt}"
        assert "feedback_status" in count_stmt, f"Proposal query missing status filter: {count_stmt}"
        assert "feedback_records.organisation_id" in count_stmt, f"Proposal query not org-scoped: {count_stmt}"
        params = mock_session.execute.await_args_list[0].args[0].compile().params
        assert set(params.get("feedback_status_1", [])) == {"pending", "routing"}


class TestRunPostCorrectionEval:
    """Tests for FeedbackManager.run_post_correction_eval() — §8.20 feedback loop."""

    @pytest.fixture
    def correcting_record(self) -> MagicMock:
        r = MagicMock(spec=FeedbackRecord)
        r.id = uuid.uuid4()
        r.organisation_id = _ORG_ID
        r.run_id = uuid.uuid4()
        r.gate_id = _GATE_ID
        r.account_id = _USER_ID
        r.rejection_reason = "bad output"
        r.rejected_output = {"result": "bad"}
        r.producing_node_id = _PRODUCING_NODE_ID
        r.feedback_status = "correcting"
        r.feedback_handler_type = "ai_correction"
        r.correction_run_id = uuid.uuid4()
        r.eval_gap = None
        r.needs_human_review = None
        return r

    @pytest.fixture
    def completed_correction_run(self) -> MagicMock:
        r = MagicMock()
        r.id = uuid.uuid4()
        r.outputs_json = {"result": "corrected output", "score": 0.95}
        r.status = "complete"
        return r

    async def test_raises_when_record_not_found(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        with (
            patch.object(mgr, "get_feedback_record", return_value=None),
            pytest.raises(FeedbackRecordNotFoundError, match=r"FeedbackRecord .* not found"),
        ):
            await mgr.run_post_correction_eval(uuid.uuid4())

    async def test_raises_when_not_in_correcting_status(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        with (
            patch.object(mgr, "get_feedback_record", return_value=sample_record),
            pytest.raises(InvalidTransitionError, match="expected 'correcting'"),
        ):
            await mgr.run_post_correction_eval(sample_record.id)

    async def test_raises_when_no_correction_run_linked(
        self, mock_session: AsyncMock, mgr: FeedbackManager, correcting_record: MagicMock
    ) -> None:
        correcting_record.correction_run_id = None
        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            pytest.raises(InvalidTransitionError, match="no correction run linked"),
        ):
            await mgr.run_post_correction_eval(correcting_record.id)

    async def test_raises_when_correction_run_not_found(
        self, mock_session: AsyncMock, mgr: FeedbackManager, correcting_record: MagicMock
    ) -> None:
        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=None),
            pytest.raises(FeedbackRecordNotFoundError, match=r"Correction run .* not found"),
        ):
            await mgr.run_post_correction_eval(correcting_record.id)

    async def test_auto_resolves_ai_correction_on_pass(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
        completed_correction_run: MagicMock,
    ) -> None:
        correcting_record.feedback_handler_type = "ai_correction"

        mock_eval_engine = MagicMock()
        mock_eval_result = MagicMock()
        mock_eval_result.passed = True
        mock_eval_result.detail = "All checks passed"
        mock_eval_result.score = 1.0
        mock_eval_engine.standalone_evaluate = MagicMock(return_value=mock_eval_result)

        mock_exec_result = MagicMock()
        mock_exec_result.scalar_one_or_none.return_value = correcting_record
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=completed_correction_run),
        ):
            outcome = await mgr.run_post_correction_eval(
                correcting_record.id,
                eval_engine=mock_eval_engine,
            )

        assert outcome["passed"] is True
        assert outcome["detail"] == "All checks passed"
        assert outcome["needs_human_review"] is False

    async def test_feeds_pure_return_to_standalone_evaluate(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
    ) -> None:
        correcting_record.feedback_handler_type = "ai_correction"

        correction_run = MagicMock()
        correction_run.id = uuid.uuid4()
        correction_run.status = "complete"
        correction_run.outputs_json = {"node-a": {"summary": "corrected output"}}
        correction_run.node_telemetry_json = {
            "node-a": {"status": "completed", "agent_stdout": "console noise", "error_message": "boom"}
        }

        mock_eval_engine = MagicMock()
        mock_eval_result = MagicMock()
        mock_eval_result.passed = True
        mock_eval_result.detail = "All checks passed"
        mock_eval_result.score = 1.0
        mock_eval_engine.standalone_evaluate = MagicMock(return_value=mock_eval_result)

        mock_exec_result = MagicMock()
        mock_exec_result.scalar_one_or_none.return_value = correcting_record
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=correction_run),
        ):
            await mgr.run_post_correction_eval(
                correcting_record.id,
                eval_engine=mock_eval_engine,
            )

        called_with = mock_eval_engine.standalone_evaluate.call_args[0][0]
        assert called_with == {"node-a": {"summary": "corrected output"}}
        assert "agent_stdout" not in json.dumps(called_with)
        assert "error_message" not in json.dumps(called_with)

    async def test_marks_needs_review_for_human_review_handler(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
        completed_correction_run: MagicMock,
    ) -> None:
        correcting_record.feedback_handler_type = "ai_correction_with_human_review"

        mock_eval_engine = MagicMock()
        mock_eval_result = MagicMock()
        mock_eval_result.passed = True
        mock_eval_result.detail = "Eval passed"
        mock_eval_result.score = 0.95
        mock_eval_engine.standalone_evaluate = MagicMock(return_value=mock_eval_result)

        mock_exec_result = MagicMock()
        mock_exec_result.scalar_one_or_none.return_value = correcting_record
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=completed_correction_run),
        ):
            outcome = await mgr.run_post_correction_eval(
                correcting_record.id,
                eval_engine=mock_eval_engine,
            )

        assert outcome["passed"] is True
        assert outcome["needs_human_review"] is True

    async def test_does_not_resolve_when_eval_fails(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
        completed_correction_run: MagicMock,
    ) -> None:
        mock_eval_engine = MagicMock()
        mock_eval_result = MagicMock()
        mock_eval_result.passed = False
        mock_eval_result.detail = "Output did not match schema"
        mock_eval_result.score = 0.0
        mock_eval_engine.standalone_evaluate = MagicMock(return_value=mock_eval_result)

        mock_exec_result = MagicMock()
        mock_exec_result.scalar_one_or_none.return_value = correcting_record
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=completed_correction_run),
        ):
            outcome = await mgr.run_post_correction_eval(
                correcting_record.id,
                eval_engine=mock_eval_engine,
            )

        assert outcome["passed"] is False
        assert outcome["needs_human_review"] is False

    async def test_raises_when_correction_run_not_complete(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
    ) -> None:
        incomplete_run = MagicMock()
        incomplete_run.status = "pending"
        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=incomplete_run),
            pytest.raises(InvalidTransitionError, match="expected 'complete'"),
        ):
            await mgr.run_post_correction_eval(correcting_record.id)

    async def test_escalates_when_correction_run_has_no_output(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
    ) -> None:
        no_output_run = MagicMock()
        no_output_run.id = uuid.uuid4()
        no_output_run.outputs_json = None
        no_output_run.status = "complete"
        updated = MagicMock(spec=FeedbackRecord)
        updated.feedback_status = "escalated"
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(return_value=exec_result)

        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=no_output_run),
        ):
            outcome = await mgr.run_post_correction_eval(correcting_record.id)

        assert outcome["passed"] is False
        assert outcome["detail"] == "Correction run produced no output"
        assert outcome["needs_human_review"] is True

    async def test_escalates_when_eval_engine_raises(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
        completed_correction_run: MagicMock,
    ) -> None:
        mock_eval_engine = MagicMock()
        mock_eval_engine.standalone_evaluate = MagicMock(side_effect=RuntimeError("eval boom"))
        updated = MagicMock(spec=FeedbackRecord)
        updated.feedback_status = "escalated"
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(return_value=exec_result)

        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=completed_correction_run),
        ):
            outcome = await mgr.run_post_correction_eval(
                correcting_record.id,
                eval_engine=mock_eval_engine,
            )

        assert outcome["passed"] is False
        assert outcome["detail"] == "Post-correction eval raised an error"
        assert outcome["needs_human_review"] is True

    async def test_propagates_cancellation_from_standalone_evaluate(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
        completed_correction_run: MagicMock,
    ) -> None:
        """asyncio.CancelledError from standalone_evaluate must propagate, not be swallowed."""
        import asyncio

        mock_eval_engine = MagicMock()
        mock_eval_engine.standalone_evaluate = MagicMock(side_effect=asyncio.CancelledError())

        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=completed_correction_run),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr.run_post_correction_eval(
                correcting_record.id,
                eval_engine=mock_eval_engine,
            )

    async def test_raises_when_resolve_is_concurrently_blocked(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
        correcting_record: MagicMock,
        completed_correction_run: MagicMock,
    ) -> None:
        """The guarded resolve UPDATE affects 0 rows when the status changed concurrently."""
        mock_eval_engine = MagicMock()
        mock_eval_result = MagicMock()
        mock_eval_result.passed = True
        mock_eval_result.detail = "ok"
        mock_eval_result.score = 1.0
        mock_eval_engine.standalone_evaluate = MagicMock(return_value=mock_eval_result)

        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=completed_correction_run),
            pytest.raises(ConcurrentModificationError, match="status changed concurrently"),
        ):
            await mgr.run_post_correction_eval(
                correcting_record.id,
                eval_engine=mock_eval_engine,
            )

    async def test_escalate_raises_when_status_changed_concurrently(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        """_escalate_record is atomic: 0 rows affected means the record left 'correcting'."""
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        with pytest.raises(ConcurrentModificationError, match="status changed concurrently"):
            await mgr._escalate_record(uuid.uuid4(), "escalation reason")

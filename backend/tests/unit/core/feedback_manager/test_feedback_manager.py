"""Unit tests for FeedbackManager service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.feedback_manager import FeedbackManager
from modulo.db.models.feedback_record import FeedbackRecord

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()
_GATE_ID = "gate-1"


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
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
    r.rejected_by = _USER_ID
    r.rejection_reason = "Output did not match requirements"
    r.rejected_output = {"result": "wrong answer"}
    r.producing_node_id = "node-b"
    r.producing_agent_id = uuid.uuid4()
    r.feedback_status = "pending"
    r.feedback_handler_type = "human"
    r.correction_run_id = None
    r.eval_gap = None
    return r


class TestCreateFeedbackRecord:
    async def test_creates_and_returns_record(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        record = await mgr.create_feedback_record(
            run_id=_RUN_ID,
            gate_id=_GATE_ID,
            rejected_by=_USER_ID,
            rejection_reason="Wrong output",
            rejected_output={"result": "bad"},
            producing_node_id="node-b",
            producing_agent_id=uuid.uuid4(),
            feedback_handler_type="human",
        )

        assert record.organisation_id == _ORG_ID
        assert record.run_id == _RUN_ID
        assert record.feedback_status == "pending"
        assert record.feedback_handler_type == "human"
        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()

    async def test_creates_with_ai_correction_type(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        record = await mgr.create_feedback_record(
            run_id=_RUN_ID,
            gate_id=_GATE_ID,
            rejected_by=_USER_ID,
            rejection_reason="Bad output",
            rejected_output={},
            producing_node_id="node-b",
            feedback_handler_type="ai_correction",
        )
        assert record.feedback_handler_type == "ai_correction"

    async def test_creates_with_human_review_type(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        record = await mgr.create_feedback_record(
            run_id=_RUN_ID,
            gate_id=_GATE_ID,
            rejected_by=_USER_ID,
            rejection_reason="Needs review",
            rejected_output={},
            producing_node_id="node-b",
            feedback_handler_type="ai_correction_with_human_review",
        )
        assert record.feedback_handler_type == "ai_correction_with_human_review"


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

    async def test_returns_empty_when_no_records(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        await self._setup_mock(mock_session, [], 0)

        result = await mgr.get_feedback_records()
        assert result["total"] == 0
        assert len(result["items"]) == 0


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

    async def test_returns_none_when_not_found(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
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
        mock_session.get = AsyncMock(return_value=sample_record)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(return_value=mock_result)

        record = await mgr.update_status(sample_record.id, "routing")
        assert record is not None
        assert record.feedback_status == "routing"

    async def test_rejects_invalid_transition(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        mock_session.get = AsyncMock(return_value=sample_record)

        with pytest.raises(ValueError, match="Cannot transition"):
            await mgr.update_status(sample_record.id, "nonexistent")

    async def test_returns_none_when_not_found(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        mock_session.get = AsyncMock(return_value=None)

        record = await mgr.update_status(uuid.uuid4(), "routing")
        assert record is None


class TestLinkCorrectionRun:
    async def test_links_correction_run(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        correction_id = uuid.uuid4()
        updated = MagicMock(spec=FeedbackRecord)
        updated.id = sample_record.id
        updated.correction_run_id = correction_id
        updated.feedback_status = "correcting"
        mock_session.get = AsyncMock(return_value=sample_record)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = updated
        mock_session.execute = AsyncMock(return_value=mock_result)

        record = await mgr.link_correction_run(sample_record.id, correction_id)
        assert record is not None
        assert record.correction_run_id == correction_id
        assert record.feedback_status == "correcting"


class TestDetectEvalGap:
    async def test_returns_false_when_no_evals(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        mock_session.execute = AsyncMock()

        is_gap = await mgr.detect_eval_gap(sample_record, eval_suite=[])
        assert is_gap is False

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
        injected = _call_kwargs["input_payload"].get("_feedback_correction", {})
        assert injected["rejection_reason"] == sample_record.rejection_reason
        assert injected["rejected_output"] == sample_record.rejected_output
        assert injected["producing_node_id"] == sample_record.producing_node_id
        assert injected["is_correction_run"] is True

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
        injected = _call_kwargs["input_payload"].get("_feedback_correction", {})
        assert injected["custom_key"] == "custom_value"
        assert injected["rejection_reason"] == sample_record.rejection_reason

    async def test_raises_when_feedback_record_not_found(
        self,
        mock_session: AsyncMock,
        mgr: FeedbackManager,
    ) -> None:
        with patch.object(mgr, "get_feedback_record", return_value=None):
            with pytest.raises(ValueError, match="FeedbackRecord .* not found"):
                await mgr.spawn_correction_run(uuid.uuid4())

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
        ):
            with pytest.raises(ValueError, match="Original run .* not found"):
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
        assert "_feedback_correction" in payload

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
        injected = _call_kwargs["input_payload"].get("_feedback_correction", {})
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

    async def test_returns_empty_when_no_records(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        await self._setup_mock(mock_session, [], 0)

        result = await mgr.get_feedback_records_inbox()
        assert result["total"] == 0
        assert len(result["items"]) == 0


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

    async def test_returns_empty_when_no_proposals(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        await self._setup_mock(mock_session, [], 0)

        result = await mgr.get_eval_proposals()
        assert result["total"] == 0
        assert len(result["items"]) == 0

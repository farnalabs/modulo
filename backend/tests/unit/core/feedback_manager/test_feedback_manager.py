"""Unit tests for FeedbackManager service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.feedback_manager import (
    FeedbackManager,
    FeedbackManagerError,
    FeedbackRecordNotFoundError,
    InvalidTransitionError,
)
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
    r.account_id = _USER_ID
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

    async def test_creates_with_ai_correction_type(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        dummy = await self._dummy_record("ai_correction")
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
                producing_node_id="node-b",
                feedback_handler_type="ai_correction",
            )
        assert record.feedback_handler_type == "ai_correction"

    async def test_creates_with_human_review_type(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        dummy = await self._dummy_record("ai_correction_with_human_review")
        with (
            patch.object(mgr, "update_status", AsyncMock(return_value=dummy)),
            patch.object(mgr, "spawn_correction_run", AsyncMock(return_value=uuid.uuid4())),
        ):
            record = await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason="Needs review",
                rejected_output={},
                producing_node_id="node-b",
                feedback_handler_type="ai_correction_with_human_review",
            )
        assert record.feedback_handler_type == "ai_correction_with_human_review"

    async def test_auto_triggers_correction_for_ai_handler(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        dummy = await self._dummy_record("ai_correction")
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
                producing_node_id="node-b",
                feedback_handler_type="ai_correction",
            )

            mock_update.assert_called_once_with(record.id, "correcting")
            mock_spawn.assert_called_once_with(record.id)

    async def test_auto_triggers_correction_for_human_review_handler(
        self, mock_session: AsyncMock, mgr: FeedbackManager
    ) -> None:
        dummy = await self._dummy_record("ai_correction_with_human_review")
        with (
            patch.object(mgr, "update_status", AsyncMock(return_value=dummy)) as mock_update,
            patch.object(mgr, "spawn_correction_run", AsyncMock(return_value=uuid.uuid4())) as mock_spawn,
        ):
            record = await mgr.create_feedback_record(
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                account_id=_USER_ID,
                rejection_reason="Auto-fix then show",
                rejected_output={"result": "bad"},
                producing_node_id="node-b",
                feedback_handler_type="ai_correction_with_human_review",
            )

            mock_update.assert_called_once_with(record.id, "correcting")
            mock_spawn.assert_called_once_with(record.id)

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
                producing_node_id="node-b",
                feedback_handler_type="human",
            )

            mock_update.assert_not_called()
            mock_spawn.assert_not_called()


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


class TestDetectEvalGap:
    async def test_returns_true_when_no_evals(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        mock_session.execute = AsyncMock()

        is_gap = await mgr.detect_eval_gap(sample_record, eval_suite=[])
        assert is_gap is True


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
            with pytest.raises(FeedbackRecordNotFoundError, match=r"FeedbackRecord .* not found"):
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
            with pytest.raises(FeedbackManagerError, match=r"Original run .* not found"):
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

    async def test_returns_empty_when_no_records(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
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

    async def test_returns_empty_when_no_proposals(self, mock_session: AsyncMock, mgr: FeedbackManager) -> None:
        await self._setup_mock(mock_session, [], 0)

        result = await mgr.get_eval_proposals()
        assert result["total"] == 0
        assert len(result["items"]) == 0


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
        r.producing_node_id = "node-b"
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
        with patch.object(mgr, "get_feedback_record", return_value=None):
            with pytest.raises(FeedbackRecordNotFoundError, match=r"FeedbackRecord .* not found"):
                await mgr.run_post_correction_eval(uuid.uuid4())

    async def test_raises_when_not_in_correcting_status(
        self, mock_session: AsyncMock, mgr: FeedbackManager, sample_record: FeedbackRecord
    ) -> None:
        with patch.object(mgr, "get_feedback_record", return_value=sample_record):
            with pytest.raises(InvalidTransitionError, match="expected 'correcting'"):
                await mgr.run_post_correction_eval(sample_record.id)

    async def test_raises_when_no_correction_run_linked(
        self, mock_session: AsyncMock, mgr: FeedbackManager, correcting_record: MagicMock
    ) -> None:
        correcting_record.correction_run_id = None
        with patch.object(mgr, "get_feedback_record", return_value=correcting_record):
            with pytest.raises(InvalidTransitionError, match="no correction run linked"):
                await mgr.run_post_correction_eval(correcting_record.id)

    async def test_raises_when_correction_run_not_found(
        self, mock_session: AsyncMock, mgr: FeedbackManager, correcting_record: MagicMock
    ) -> None:
        with (
            patch.object(mgr, "get_feedback_record", return_value=correcting_record),
            patch("modulo.core.feedback_manager.get_run", return_value=None),
        ):
            with pytest.raises(FeedbackRecordNotFoundError, match=r"Correction run .* not found"):
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

        mock_session.execute = AsyncMock()

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

"""Unit tests for the HITL claim-expiry job.

Covers ``expire_stale_claims`` (the shared per-org expiry sweep used by the
in-process loop and the SAQ ``claim_expiry`` cron) and the in-process
``ClaimExpiryJob`` polling loop — including the advisory-lock guard, audit
event capture, notification dispatch, and cancellation handling.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager.expiry_job import (
    POLL_INTERVAL,
    ClaimExpiryJob,
    expire_stale_claims,
)


def _make_session(*execute_results: Any) -> AsyncMock:
    """Return a mock async session pre-wired for one expiry pass.

    ``execute`` is driven by an explicit result list so the number and order
    of queries is asserted implicitly (over-calling raises StopIteration).
    """
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    if len(execute_results) == 1:
        session.execute = AsyncMock(return_value=execute_results[0])
    else:
        session.execute = AsyncMock(side_effect=list(execute_results))
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    """Return a mock ``async_sessionmaker`` yielding *session* via ``async with``."""
    factory = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = context
    return factory


def _org_result(*org_ids: uuid.UUID) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value = list(org_ids)
    return result


def _lock_result(granted: bool) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = granted
    return result


def _stale_result(*rows: Any) -> MagicMock:
    result = MagicMock()
    result.all.return_value = list(rows)
    return result


def _claim_row(
    *,
    claim_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    gate_id: str = "review",
    account_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=claim_id or uuid.uuid4(),
        run_id=run_id or uuid.uuid4(),
        gate_id=gate_id,
        account_id=account_id or uuid.uuid4(),
    )


@patch("modulo.core.hitl_manager.expiry_job.append_audit_event", new_callable=AsyncMock)
class TestExpireStaleClaims:
    async def test_expires_stale_claims_and_returns_entries(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        claim = _claim_row(gate_id="qa_gate")
        session = _make_session(
            _org_result(org_id),
            _lock_result(True),
            _stale_result(claim),
            MagicMock(),
            MagicMock(),
        )

        expired = await expire_stale_claims(_make_factory(session))

        assert len(expired) == 1
        assert expired[0]["claim_id"] == claim.id
        assert expired[0]["run_id"] == claim.run_id
        assert expired[0]["gate_id"] == claim.gate_id
        assert expired[0]["claimed_by"] == claim.account_id
        assert expired[0]["organisation_id"] == org_id
        mock_audit.assert_awaited_once()
        event_kwargs = mock_audit.await_args.kwargs
        assert event_kwargs["org_id"] == org_id
        assert event_kwargs["event_type"] == "hitl.claim_expired"
        assert event_kwargs["payload_json"]["node_id"] == "qa_gate"

    async def test_returns_empty_when_no_orgs(self, mock_audit) -> None:
        session = _make_session(_org_result())

        expired = await expire_stale_claims(_make_factory(session))

        assert expired == []
        mock_audit.assert_not_awaited()

    async def test_skips_org_when_lock_not_granted(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        session = _make_session(_org_result(org_id), _lock_result(False))

        expired = await expire_stale_claims(_make_factory(session))

        assert expired == []
        mock_audit.assert_not_awaited()

    async def test_cancel_during_lock_acquisition_propagates(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        session = _make_session(_org_result(org_id), asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await expire_stale_claims(_make_factory(session))

    async def test_tolerates_lock_query_failure(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        session = _make_session()
        session.execute.side_effect = [_org_result(org_id), RuntimeError("lock unavailable"), _stale_result()]

        expired = await expire_stale_claims(_make_factory(session))

        assert expired == []
        mock_audit.assert_not_awaited()

    async def test_skips_org_with_no_stale_claims(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        session = _make_session(_org_result(org_id), _lock_result(True), _stale_result())

        expired = await expire_stale_claims(_make_factory(session))

        assert expired == []
        mock_audit.assert_not_awaited()

    async def test_resets_claim_and_run(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        claim = _claim_row()
        session = _make_session(
            _org_result(org_id),
            _lock_result(True),
            _stale_result(claim),
            MagicMock(),
            MagicMock(),
        )

        await expire_stale_claims(_make_factory(session))

        claim_update, run_update = session.execute.await_args_list[-2][0][0], session.execute.await_args_list[-1][0][0]
        assert claim_update.table.name == "hitl_claims"
        assert run_update.table.name == "runs"
        params = run_update.compile().params
        assert params["status"] == "awaiting_human"
        assert params["status_1"] == "claimed"

    async def test_audit_failure_does_not_abort_org_transaction(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        claim = _claim_row()
        session = _make_session(
            _org_result(org_id),
            _lock_result(True),
            _stale_result(claim),
            MagicMock(),
            MagicMock(),
        )
        mock_audit.side_effect = RuntimeError("audit db down")

        expired = await expire_stale_claims(_make_factory(session))

        assert len(expired) == 1

    async def test_cancelled_during_audit_propagates(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        claim = _claim_row()
        session = _make_session(
            _org_result(org_id),
            _lock_result(True),
            _stale_result(claim),
            MagicMock(),
            MagicMock(),
        )
        mock_audit.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await expire_stale_claims(_make_factory(session))

    async def test_multiple_orgs_process_independently(self, mock_audit) -> None:
        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        claim_a, claim_b = _claim_row(), _claim_row()
        session = _make_session(
            _org_result(org_a, org_b),
            _lock_result(True),
            _stale_result(claim_a),
            MagicMock(),
            MagicMock(),
            _lock_result(True),
            _stale_result(claim_b),
            MagicMock(),
            MagicMock(),
        )

        expired = await expire_stale_claims(_make_factory(session))

        assert {entry["organisation_id"] for entry in expired} == {org_a, org_b}

    async def test_dispatches_claim_expired_notifications(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        claim = _claim_row(gate_id="gate-7")
        session = _make_session(
            _org_result(org_id),
            _lock_result(True),
            _stale_result(claim),
            MagicMock(),
            MagicMock(),
        )
        notifier = MagicMock()
        notifier.dispatch_event = AsyncMock()

        expired = await expire_stale_claims(_make_factory(session), notifier=notifier)

        assert len(expired) == 1
        notifier.dispatch_event.assert_awaited_once()
        payload = notifier.dispatch_event.await_args.kwargs["payload"]
        assert payload["run_id"] == str(claim.run_id)
        assert payload["gate_id"] == "gate-7"
        assert payload["claimed_by"] == str(claim.account_id)

    async def test_no_notification_when_notifier_not_provided(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        claim = _claim_row()
        session = _make_session(
            _org_result(org_id),
            _lock_result(True),
            _stale_result(claim),
            MagicMock(),
            MagicMock(),
        )

        await expire_stale_claims(_make_factory(session))

    async def test_notification_failure_is_tolerated(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        claim = _claim_row()
        session = _make_session(
            _org_result(org_id),
            _lock_result(True),
            _stale_result(claim),
            MagicMock(),
            MagicMock(),
        )
        notifier = MagicMock()
        notifier.dispatch_event = AsyncMock(side_effect=RuntimeError("notifier down"))

        expired = await expire_stale_claims(_make_factory(session), notifier=notifier)

        assert len(expired) == 1

    async def test_cancel_during_notification_propagates(self, mock_audit) -> None:
        org_id = uuid.uuid4()
        claim = _claim_row()
        session = _make_session(
            _org_result(org_id),
            _lock_result(True),
            _stale_result(claim),
            MagicMock(),
            MagicMock(),
        )
        notifier = MagicMock()
        notifier.dispatch_event = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await expire_stale_claims(_make_factory(session), notifier=notifier)


class TestClaimExpiryJob:
    def _make_job(self) -> ClaimExpiryJob:
        return ClaimExpiryJob(MagicMock())

    async def test_start_creates_single_task(self) -> None:
        job = self._make_job()
        with patch.object(job, "_run", new_callable=AsyncMock):
            await job.start()
            assert job._task is not None
            await job.start()
        await job.stop()

    async def test_stop_without_start_is_noop(self) -> None:
        job = self._make_job()
        await job.stop()
        assert job._task is None

    async def test_stop_cancels_running_task(self) -> None:
        job = self._make_job()

        async def _block_forever() -> None:
            await asyncio.Event().wait()

        job._run = _block_forever
        await job.start()
        assert job._task is not None
        await job.stop()
        assert job._task is None

    async def test_run_loop_expires_claims_and_sleeps(self) -> None:
        job = self._make_job()
        mock_expire = AsyncMock(return_value=[{"claim_id": uuid.uuid4()}])
        mock_sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with (
            patch("modulo.core.hitl_manager.expiry_job.expire_stale_claims", mock_expire),
            patch("modulo.core.hitl_manager.expiry_job.asyncio.sleep", mock_sleep),
        ):
            await job._run()

        assert mock_expire.await_count == 2
        assert mock_sleep.await_count == 2
        mock_sleep.assert_awaited_with(POLL_INTERVAL)

    async def test_run_loop_tick_failure_is_logged_and_continues(self) -> None:
        job = self._make_job()
        mock_expire = AsyncMock(side_effect=[RuntimeError("boom"), asyncio.CancelledError()])
        mock_sleep = AsyncMock(return_value=None)
        with (
            patch("modulo.core.hitl_manager.expiry_job.expire_stale_claims", mock_expire),
            patch("modulo.core.hitl_manager.expiry_job.asyncio.sleep", mock_sleep),
        ):
            await job._run()

        assert mock_expire.await_count == 2
        mock_sleep.assert_awaited_once_with(POLL_INTERVAL)

    async def test_run_loop_breaks_on_cancel_during_sleep(self) -> None:
        job = self._make_job()
        mock_expire = AsyncMock(return_value=[])
        mock_sleep = AsyncMock(side_effect=asyncio.CancelledError())
        with (
            patch("modulo.core.hitl_manager.expiry_job.expire_stale_claims", mock_expire),
            patch("modulo.core.hitl_manager.expiry_job.asyncio.sleep", mock_sleep),
        ):
            await job._run()

        mock_expire.assert_awaited_once()

    async def test_expire_once_forwards_notifier(self) -> None:
        notifier = MagicMock()
        job = ClaimExpiryJob(MagicMock(), notifier=notifier)
        with patch(
            "modulo.core.hitl_manager.expiry_job.expire_stale_claims",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_expire:
            result = await job._expire_once()

        assert result == []
        assert mock_expire.await_args.args[0] is job._session_factory
        assert mock_expire.await_args.kwargs["notifier"] is notifier

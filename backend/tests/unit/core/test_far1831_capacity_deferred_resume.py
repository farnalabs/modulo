"""FAR-1831: a capacity-denied HITL resume must never lose the committed decision.

Three coupled behaviours (the route-level half lives in
``tests/unit/api/test_hitl_resilience.py``):

1. A resume-path ``SandboxCapacityExceededError`` resets ``running`` back to
   ``awaiting_human`` (fenced by the claim token) instead of terminal-failing
   the run — the decision survives for the dispatcher to resume.
2. ``dispatcher_reconcile``'s resume re-dispatch (the shared
   ``_re_dispatch_reconciled_run`` chokepoint that both the F6a resume leg and
   the FAR-1728 override-resume leg exit through) is capacity-gated: a
   saturated org skips the tick rather than burning resume claims.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch
from modulo.core import pipeline_execution as pe
from modulo.core.pipeline_engine.executor import SandboxCapacityExceededError

ORG = uuid.uuid4()


class _FakeBegin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakeSession:
    """Minimal async-context-manager session for the reset helper."""

    def __init__(self) -> None:
        self.begin_cm = _FakeBegin()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _FakeBegin:
        return self.begin_cm


def _patch_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the helper's engine/session plumbing with fakes."""
    monkeypatch.setattr(pe, "async_sessionmaker", MagicMock(return_value=lambda: _FakeSession()))
    monkeypatch.setattr(pe, "set_rls_org", AsyncMock())


class TestDeferCapacityDeniedRun:
    async def test_reset_writes_awaiting_human_fenced_by_claim_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_session_factory(monkeypatch)
        run_id = uuid.uuid4()
        update = AsyncMock(return_value=object())
        with patch("modulo.db.crud.run.update_run_status", new=update):
            landed = await pe.defer_capacity_denied_run_to_awaiting_human(
                MagicMock(), str(run_id), str(ORG), claim_token="tok-9"
            )

        assert landed is True
        update.assert_awaited_once()
        args, kwargs = update.await_args
        assert args[1] == run_id
        assert args[2] == "awaiting_human"
        # Fenced: the write only lands from ``running`` and only for this claim.
        assert kwargs["claim_token"] == "tok-9"
        assert kwargs["from_status"] == "running"
        # FAR-1831 M1: the reset REFUNDS the single claim the capacity-denied
        # resume consumed, so repeated denials cannot exhaust the claim cap and
        # strand the committed decision.
        assert kwargs["refund_claim_count"] is True

    async def test_reset_reports_false_when_the_fence_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_session_factory(monkeypatch)
        with patch("modulo.db.crud.run.update_run_status", new=AsyncMock(return_value=None)):
            landed = await pe.defer_capacity_denied_run_to_awaiting_human(
                MagicMock(), str(uuid.uuid4()), str(ORG), claim_token="stale"
            )
        assert landed is False


class TestRunExecutorCapacityDeferred:
    """``run_executor_with_watchdog`` must not terminal-fail a capacity-denied
    resume — proven by observation: ``fail_run_terminal`` is never awaited and
    the reset helper IS, with the live claim token.

    FAR-1831 M2: the reset is SCOPED to a resume carrying a committed HITL
    decision (``hitl_decision_resume=True``). A node-recovery / guardrail
    override resume (no committed decision) must keep the terminal outcome.
    """

    async def test_hitl_decision_resume_capacity_error_does_not_terminal_fail(self) -> None:
        async def _raise_capacity() -> None:
            raise SandboxCapacityExceededError(ORG)

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=0.05, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail,
            patch.object(
                pe, "defer_capacity_denied_run_to_awaiting_human", new_callable=AsyncMock, return_value=True
            ) as defer,
        ):
            result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                engine,
                run_id=str(uuid.uuid4()),
                org_id=str(ORG),
                executor=MagicMock(),
                job=None,
                claim_token="tok-7",
                hitl_decision_resume=True,
                execute_fn=_raise_capacity,
            )

        assert result == {"status": "capacity_deferred"}
        fail.assert_not_awaited()
        defer.assert_awaited_once()
        assert defer.await_args.kwargs["claim_token"] == "tok-7"

    async def test_non_hitl_resume_capacity_error_terminal_fails(self) -> None:
        """FAR-1831 M2: a NON-HITL resume (node recovery / FAR-1728 guardrail
        override) carries no committed HITL decision, so parking it in
        ``awaiting_human`` would strand it decisionless. It keeps the honest,
        OBSERVABLE terminal outcome instead — never a silent no-op."""

        async def _raise_capacity() -> None:
            raise SandboxCapacityExceededError(ORG)

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=0.05, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail,
            patch.object(
                pe, "defer_capacity_denied_run_to_awaiting_human", new_callable=AsyncMock, return_value=True
            ) as defer,
        ):
            result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                engine,
                run_id=str(uuid.uuid4()),
                org_id=str(ORG),
                executor=MagicMock(),
                job=None,
                claim_token="tok-8",
                hitl_decision_resume=False,
                execute_fn=_raise_capacity,
            )

        assert result == {"status": "failed"}
        defer.assert_not_awaited()
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "executor_failed"

    async def test_generic_error_still_terminal_fails(self) -> None:
        """Regression guard: only the capacity error is deferred; every other
        executor failure keeps the honest terminal-fail."""

        async def _boom() -> None:
            raise RuntimeError("boom")

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=0.05, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail,
            patch.object(pe, "defer_capacity_denied_run_to_awaiting_human", new_callable=AsyncMock) as defer,
        ):
            result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                engine,
                run_id=str(uuid.uuid4()),
                org_id=str(ORG),
                executor=MagicMock(),
                job=None,
                execute_fn=_boom,
            )

        assert result == {"status": "failed"}
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "executor_failed"
        defer.assert_not_awaited()


class TestResumeCarriesCommittedHitlDecision:
    """FAR-1831 M2: the discriminator that scopes the non-terminal reset.

    The HITL decision vocabulary is closed (the same set the F6a guard accepts);
    node recovery and the FAR-1728 guardrail override are NOT committed HITL
    decisions, so they must not be parked in ``awaiting_human``.
    """

    @pytest.mark.parametrize("action", ["approved", "rejected", "deliver_manual", "manual_output"])
    def test_hitl_decision_actions_are_recognised(self, action: str) -> None:
        assert pe.resume_carries_committed_hitl_decision({"action": action, "review_id": "gate-1"}) is True

    @pytest.mark.parametrize(
        "resume_data",
        [None, {}, {"action": "skip"}, {"action": "replay", "output": None}, {"action": "unknown"}],
    )
    def test_non_hitl_payloads_are_not_recognised(self, resume_data: dict[str, object] | None) -> None:
        assert pe.resume_carries_committed_hitl_decision(resume_data) is False


class TestFencedWriterRefundContract:
    """FAR-1831 M1: the refund is fenced to the claim it refunds, so requesting
    it without a claim token is a fail-loud programming error (never a silent
    no-op on the unfenced ORM path)."""

    async def test_refund_without_claim_token_is_refused(self) -> None:
        from modulo.db.crud.run import update_run_status

        with pytest.raises(ValueError, match="refund_claim_count requires claim_token"):
            await update_run_status(
                MagicMock(),
                uuid.uuid4(),
                "awaiting_human",
                refund_claim_count=True,
            )


class TestResumeRunThreadsHitlDecisionFlag:
    """FAR-1831 M2: ``resume_run`` classifies its payload and threads the flag
    into the watchdog, so a recover / guardrail-override resume is never parked
    decisionless."""

    @pytest.mark.parametrize(
        ("resume_data", "expected"),
        [
            ({"action": "approved", "review_id": "gate-1"}, True),
            ({"action": "rejected", "review_id": "gate-1"}, True),
            ({"action": "skip"}, False),
            ({"action": "replay", "output": {"x": 1}}, False),
            (None, False),
        ],
    )
    async def test_flag_matches_the_payload(self, resume_data: dict[str, object] | None, expected: bool) -> None:
        captured: dict[str, object] = {}

        async def _capture(_aeng: object, **kwargs: object) -> dict[str, str]:
            captured.update(kwargs)
            return {"status": "complete"}

        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value="tok-fresh"),
            patch.object(pe, "load_and_setup", new_callable=AsyncMock) as load,
            patch.object(pe, "mark_complete", new_callable=AsyncMock),
            patch.object(pe, "run_executor_with_watchdog", side_effect=_capture),
        ):
            run = MagicMock()
            executor = MagicMock()
            executor.resume = AsyncMock()
            load.return_value = (run, executor)
            await pe.resume_run(
                async_engine=MagicMock(),  # type: ignore[arg-type]
                run_id=str(uuid.uuid4()),
                org_id=str(uuid.uuid4()),
                resume_data=resume_data,
            )

        assert captured["hitl_decision_resume"] is expected


class TestResumeRedispatchCapacityGate:
    """``_re_dispatch_reconciled_run`` is the single chokepoint for a
    ``resume_run`` re-dispatch (F6a resume leg + FAR-1728 override-resume leg),
    so gating there covers every reconcile-driven resume."""

    @staticmethod
    def _summary() -> dict[str, object]:
        return {"capacity_deferred": 0, "repaired": 0}

    async def test_resume_skipped_while_at_capacity(self) -> None:
        row = SimpleNamespace(id=uuid.uuid4(), status="awaiting_human")
        q = MagicMock()
        q.name = "runs"
        summary = self._summary()
        enqueue = AsyncMock(return_value=("enqueued", "job-1"))
        with (
            patch.object(ch, "_resume_org_capacity_free", new=AsyncMock(return_value=False)),
            patch.object(ch, "_re_enqueue_run", new=enqueue),
        ):
            rc = await ch._re_dispatch_reconciled_run(
                MagicMock(), q, ORG, row, "resume_run", "suffix", {"action": "approved"}, False, 0, summary
            )

        assert rc == 0
        assert summary["capacity_deferred"] == 1
        assert summary["repaired"] == 0
        enqueue.assert_not_awaited()

    async def test_resume_proceeds_once_capacity_frees(self) -> None:
        row = SimpleNamespace(id=uuid.uuid4(), status="awaiting_human")
        q = MagicMock()
        q.name = "runs"
        summary = self._summary()
        enqueue = AsyncMock(return_value=("enqueued", "job-1"))
        with (
            patch.object(ch, "_resume_org_capacity_free", new=AsyncMock(return_value=True)),
            patch.object(ch, "_re_enqueue_run", new=enqueue),
        ):
            rc = await ch._re_dispatch_reconciled_run(
                MagicMock(), q, ORG, row, "resume_run", "suffix", {"action": "approved"}, False, 0, summary
            )

        assert rc == 0
        assert summary["repaired"] == 1
        assert summary["capacity_deferred"] == 0
        enqueue.assert_awaited_once()
        assert enqueue.await_args.args[3] == "resume_run"

    async def test_execute_run_is_not_resume_capacity_gated(self) -> None:
        """``execute_run`` re-dispatch is already org-cap-deferred by
        ``dispatch_run`` itself — the new gate applies to resumes only."""
        row = SimpleNamespace(id=uuid.uuid4(), status="pending")
        q = MagicMock()
        q.name = "runs"
        summary = self._summary()
        enqueue = AsyncMock(return_value=("deferred", None))
        capacity = AsyncMock(return_value=False)
        with (
            patch.object(ch, "_resume_org_capacity_free", new=capacity),
            patch.object(ch, "_re_enqueue_run", new=enqueue),
        ):
            await ch._re_dispatch_reconciled_run(
                MagicMock(), q, ORG, row, "execute_run", "suffix", None, False, 0, summary
            )

        capacity.assert_not_awaited()
        enqueue.assert_awaited_once()
        assert summary["capacity_deferred"] == 1

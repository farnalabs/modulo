"""FAR-1088 (W4): the zero-node failure-coverage carve-out for nodeless zombies.

A claimed-but-nodeless zombie executed ZERO nodes, so re-dispatch is safe
(nothing to double-execute). Before FAR-1088, a policy like Branch Fixer's
``{"on": ["failure"], "max_retries": 2}`` terminal-failed the zombie with NO
re-dispatch at all: the shared ``_retry_after_policy`` matcher returned None
(``_stall_event_matches`` requires ``"stall"`` in ``on``;
``_failure_event_matches`` requires ``final_status == "failed"`` and is called
here with ``"stalled"``), and the call site then returned False for any
non-empty ``on``. W4 makes a zero-node death re-dispatchable under a
failure-covered policy, bounded by THAT policy's own ``max_retries``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core import cron_helpers as ch

FAILURE_POLICY = {"on": ["failure"], "max_retries": 2}


def _row(claim_count: int, retry_policy: Any = None, *, checkpoints_absent: bool = True) -> SimpleNamespace:
    """A running, claimed-but-nodeless run row (the decision's input).

    ``checkpoints_absent`` mirrors the reconcile SELECT's computed flag —
    ``True`` means the row is genuinely zero-node (no LangGraph checkpoint for
    the thread), which is the premise the carve-out is allowed under.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        status="running",
        claim_count=claim_count,
        retry_policy=retry_policy,
        node_token_usage=None,
        outputs_absent=True,
        checkpoints_absent=checkpoints_absent,
        started_at=datetime.now(UTC) - timedelta(minutes=60),
        dispatched_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=30),
    )


def _settings(saq_nodeless_redispatch_budget: int = 4) -> MagicMock:
    settings = MagicMock()
    settings.saq_nodeless_redispatch_budget = saq_nodeless_redispatch_budget
    return settings


@pytest.fixture
def budget_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SAQ_NODELESS_REDISPATCH_BUDGET default path (4)."""
    monkeypatch.setattr(ch, "get_settings", lambda: _settings())


class TestFailureCoverageCarveOut:
    """The NEW zero-node failure-coverage carve-out (FAR-1088 W4)."""

    def test_re_dispatches_claim_1_2_3(self, budget_default: None) -> None:
        """claim_count 1/2/3 re-dispatch: attempt = max(0, claim - 1) <= 2."""
        assert ch._should_redispatch_nodeless(_row(1, FAILURE_POLICY)) is True
        assert ch._should_redispatch_nodeless(_row(2, FAILURE_POLICY)) is True
        assert ch._should_redispatch_nodeless(_row(3, FAILURE_POLICY)) is True

    def test_terminal_fails_at_claim_4(self, budget_default: None) -> None:
        """Bounded by the POLICY's max_retries=2: attempt 3 > 2 terminal-fails."""
        assert ch._should_redispatch_nodeless(_row(4, FAILURE_POLICY)) is False

    def test_bounded_by_policy_budget_not_config_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even with the configurable budget raised well above the policy, the
        carve-out stops at the policy's own max_retries (no aggregate cap is
        introduced; the setting stays a per-run default for policy-less runs)."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings(saq_nodeless_redispatch_budget=10))
        assert ch._should_redispatch_nodeless(_row(4, FAILURE_POLICY)) is False

    def test_zero_max_retries_terminal_fails(self, budget_default: None) -> None:
        """max_retries == 0 means no retry — unchanged (carve-out not applied)."""
        policy = {"on": ["failure"], "max_retries": 0}
        assert ch._should_redispatch_nodeless(_row(1, policy)) is False

    def test_malformed_budget_terminal_fails(self, budget_default: None) -> None:
        """A non-int / out-of-range budget with a non-empty `on` stays
        terminal-fail (the shared matcher's validation fail-closes)."""
        for budget in ("lots", True, 6, -1):
            policy = {"on": ["failure"], "max_retries": budget}
            assert ch._should_redispatch_nodeless(_row(1, policy)) is False


class TestPreservedBehaviour:
    """Every pre-existing behaviour must be byte-for-byte unchanged."""

    def test_stall_policy_honors_policy_budget(self, budget_default: None) -> None:
        """The stall branch (shared matcher) is untouched: claim 1/2/3 True,
        claim 4 False under max_retries=2."""
        policy = {"on": ["stall"], "max_retries": 2}
        assert ch._should_redispatch_nodeless(_row(1, policy)) is True
        assert ch._should_redispatch_nodeless(_row(2, policy)) is True
        assert ch._should_redispatch_nodeless(_row(3, policy)) is True
        assert ch._should_redispatch_nodeless(_row(4, policy)) is False

    def test_timeout_only_policy_terminal_fails(self, budget_default: None) -> None:
        """A non-empty `on` covering neither stall nor failure still
        terminal-fails regardless of claim_count."""
        policy = {"on": ["timeout"], "max_retries": 5}
        assert ch._should_redispatch_nodeless(_row(1, policy)) is False
        assert ch._should_redispatch_nodeless(_row(4, policy)) is False

    def test_uncovered_event_policy_terminal_fails(self, budget_default: None) -> None:
        """An event that names no stall/failure coverage (e.g. ci_failure)
        still terminal-fails."""
        policy = {"on": ["ci_failure"], "max_retries": 5}
        assert ch._should_redispatch_nodeless(_row(1, policy)) is False

    def test_empty_on_uses_budget_default(self, budget_default: None) -> None:
        """An explicit empty `on` keeps budget-default repair: claim 4 True
        (inclusive bound), claim 5 False under the default budget of 4."""
        policy = {"on": [], "max_retries": 0}
        assert ch._should_redispatch_nodeless(_row(4, policy)) is True
        assert ch._should_redispatch_nodeless(_row(5, policy)) is False

    def test_missing_on_uses_shared_matcher_not_budget_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-649: an absent `on` with a valid budget is ALL-events coverage —
        the POLICY budget (3) is the binding bound, not the configurable
        budget-default: with the default raised to 10, claim 5 still
        terminal-fails (attempt 4 > 3) where the budget-default would allow it."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings(saq_nodeless_redispatch_budget=10))
        policy = {"max_retries": 3}
        assert ch._should_redispatch_nodeless(_row(4, policy)) is True
        assert ch._should_redispatch_nodeless(_row(5, policy)) is False

    def test_policy_less_uses_budget_default(self, budget_default: None) -> None:
        """No policy at all: the configurable budget path is untouched."""
        assert ch._should_redispatch_nodeless(_row(4)) is True
        assert ch._should_redispatch_nodeless(_row(5)) is False

    def test_carve_out_budget_is_derived_from_the_shared_matcher(
        self, monkeypatch: pytest.MonkeyPatch, budget_default: None
    ) -> None:
        """The carve-out asks the SHARED ``_retry_after_policy`` matcher for
        the failure budget instead of re-validating ``max_retries`` by hand
        (F3): a matcher that answers with a DIFFERENT budget moves the
        decision with it — under the hand-rolled validation the policy's own
        ``max_retries=2`` would have bound (claim 3 still True)."""
        import modulo.core.pipeline_engine.executor as executor_module

        real = executor_module._retry_after_policy

        def _fake(
            policy: Any, final_status: str, error_code: str | None, error_detail: str | None = None
        ) -> int | None:
            if final_status == "failed":
                # The carve-out's derivation call — answer with 1, not the
                # policy's max_retries, so the decision proves which source won.
                return 1
            return real(policy, final_status, error_code, error_detail)

        monkeypatch.setattr(executor_module, "_retry_after_policy", _fake)

        # attempt = max(0, claim-1): claim 2 -> 1 <= 1 re-dispatches; claim 3
        # -> 2 > 1 terminal-fails — the matcher-derived budget, not max_retries.
        assert ch._should_redispatch_nodeless(_row(2, FAILURE_POLICY)) is True
        assert ch._should_redispatch_nodeless(_row(3, FAILURE_POLICY)) is False


class TestZeroNodePremiseEnforcedAtTheCarveOut:
    """F4: the carve-out is reachable ONLY for a genuinely zero-node row.

    ``_is_nodeless_zombie_row`` is the gate both carve-out call sites run
    through, and it now carries the SQL predicate's checkpoint leg — a
    worker-died-MID-RUN row (selected via the stale-heartbeat branch) is never
    treated as a zero-node zombie, so the failure carve-out can never
    re-dispatch a run that already executed nodes.
    """

    @pytest.fixture(autouse=True)
    def _settings_double(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The row recheck reads the in-flight floor from settings (FAR-1088
        F2); this class tests the checkpoint leg, so stand in the file's
        settings double — the floor resolves to its default-config fallback
        (3900) without needing a real Settings."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())

    def test_checkpointed_mid_run_row_is_not_a_nodeless_zombie(self) -> None:
        """Checkpoints present (a super-step completed) ⇒ NOT zero-node, no
        matter how old or how stale the heartbeat is."""
        row = _row(1, FAILURE_POLICY, checkpoints_absent=False)

        assert ch._is_nodeless_zombie_row(row, 35) is False

    def test_finalised_mid_run_row_is_not_a_nodeless_zombie(self) -> None:
        """node_token_usage set (run finalisation wrote it) ⇒ NOT zero-node."""
        row = _row(1, FAILURE_POLICY, checkpoints_absent=True)
        row.node_token_usage = {"node-a": 1234}

        assert ch._is_nodeless_zombie_row(row, 35) is False

    def test_row_without_the_checkpoint_flag_fails_closed(self) -> None:
        """A row that does not carry the SELECT's ``checkpoints_absent`` flag
        fails CLOSED — a future row source that forgets the column can never
        reach the carve-out."""
        row = _row(1, FAILURE_POLICY)
        del row.checkpoints_absent

        assert ch._is_nodeless_zombie_row(row, 35) is False

    async def test_checkpointed_row_never_reaches_the_carve_out(self) -> None:
        """The repair branch returns ``None`` (row continues down the normal
        path) for a checkpoint-present row: no re-dispatch AND no
        terminal-fail through the zero-node branch — the carve-out is simply
        never consulted."""
        row = _row(1, FAILURE_POLICY, checkpoints_absent=False)
        summary = {"nodeless_failed": 0, "nodeless_redispatched": 0, "nodeless_capped": 0, "skipped": 0}

        handled = await ch._reconcile_nodeless_repair(
            AsyncMock(),
            MagicMock(),
            uuid.uuid4(),
            row,
            35,
            0,
            summary,
            [],
        )

        assert handled is None
        assert summary["nodeless_failed"] == 0
        assert summary["nodeless_redispatched"] == 0
        assert summary["skipped"] == 0

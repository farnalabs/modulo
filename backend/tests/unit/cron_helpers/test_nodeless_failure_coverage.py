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
from unittest.mock import MagicMock

import pytest

from modulo.core import cron_helpers as ch

FAILURE_POLICY = {"on": ["failure"], "max_retries": 2}


def _row(claim_count: int, retry_policy: Any = None) -> SimpleNamespace:
    """A running, claimed-but-nodeless run row (the decision's input)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        status="running",
        claim_count=claim_count,
        retry_policy=retry_policy,
        node_token_usage=None,
        outputs_absent=True,
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

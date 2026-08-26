"""Unit tests for modulo.core.spend_ceiling (FAR-391 hard spend ceilings).

Pure logic only — no DB, no FastAPI. Verifies the per-run and per-org ceiling
evaluation, the combined evaluator, the cents conversion, and the kill-switch
(zero-ceiling) semantics.
"""

from decimal import Decimal

from modulo.core.spend_ceiling import (
    ORG_CEILING_EXCEEDED,
    RUN_CEILING_EXCEEDED,
    cents_from_usd,
    evaluate_org_spend_ceiling,
    evaluate_run_spend_ceiling,
    evaluate_spend_ceilings,
)

# ---------------------------------------------------------------------------
# cents_from_usd
# ---------------------------------------------------------------------------


class TestCentsFromUsd:
    def test_none_passthrough(self) -> None:
        assert cents_from_usd(None) is None

    def test_rounds_to_cents(self) -> None:
        assert cents_from_usd(Decimal("12.34")) == 1234
        assert cents_from_usd(12.34) == 1234
        assert cents_from_usd("0.01") == 1
        # Half-up rounding on a >2dp input (malformed money) rounds the half cent up.
        assert cents_from_usd(Decimal("12.345")) == 1235
        assert cents_from_usd(Decimal("12.344")) == 1234

    def test_nan_degrades_to_none(self) -> None:
        assert cents_from_usd(Decimal("NaN")) is None


# ---------------------------------------------------------------------------
# evaluate_run_spend_ceiling
# ---------------------------------------------------------------------------


class TestEvaluateRunSpendCeiling:
    def test_unlimited_when_none(self) -> None:
        d = evaluate_run_spend_ceiling(run_cost_so_far_cents=999_999_999, max_run_cost_cents=None)
        assert d.allowed is True
        assert not d.reason

    def test_within_ceiling_allowed(self) -> None:
        d = evaluate_run_spend_ceiling(
            run_cost_so_far_cents=500, estimated_next_step_cents=100, max_run_cost_cents=1000
        )
        assert d.allowed is True

    def test_next_step_pushes_over_ceiling_refused(self) -> None:
        d = evaluate_run_spend_ceiling(
            run_cost_so_far_cents=900, estimated_next_step_cents=200, max_run_cost_cents=1000
        )
        assert d.allowed is False
        assert d.reason == RUN_CEILING_EXCEEDED

    def test_already_over_ceiling_refused(self) -> None:
        d = evaluate_run_spend_ceiling(run_cost_so_far_cents=1500, max_run_cost_cents=1000)
        assert d.allowed is False
        assert d.reason == RUN_CEILING_EXCEEDED

    def test_zero_ceiling_is_kill_switch(self) -> None:
        d = evaluate_run_spend_ceiling(run_cost_so_far_cents=0, estimated_next_step_cents=1, max_run_cost_cents=0)
        assert d.allowed is False
        assert d.reason == RUN_CEILING_EXCEEDED

    def test_zero_ceiling_allows_zero_cost_run(self) -> None:
        d = evaluate_run_spend_ceiling(run_cost_so_far_cents=0, estimated_next_step_cents=0, max_run_cost_cents=0)
        assert d.allowed is True

    def test_negative_inputs_normalised(self) -> None:
        d = evaluate_run_spend_ceiling(run_cost_so_far_cents=-5, estimated_next_step_cents=-5, max_run_cost_cents=10)
        assert d.allowed is True


# ---------------------------------------------------------------------------
# evaluate_org_spend_ceiling
# ---------------------------------------------------------------------------


class TestEvaluateOrgSpendCeiling:
    def test_unlimited_when_none(self) -> None:
        d = evaluate_org_spend_ceiling(org_cumulative_spend_cents=10**12, spend_ceiling_cents=None)
        assert d.allowed is True

    def test_within_ceiling_allowed(self) -> None:
        d = evaluate_org_spend_ceiling(
            org_cumulative_spend_cents=4000, additional_cents=5000, spend_ceiling_cents=10_000
        )
        assert d.allowed is True
        assert d.projected_org_cumulative_cents == 9000

    def test_exceeds_ceiling_refused(self) -> None:
        d = evaluate_org_spend_ceiling(
            org_cumulative_spend_cents=9000, additional_cents=2000, spend_ceiling_cents=10_000
        )
        assert d.allowed is False
        assert d.reason == ORG_CEILING_EXCEEDED
        assert d.projected_org_cumulative_cents == 11_000

    def test_exact_ceiling_allowed(self) -> None:
        d = evaluate_org_spend_ceiling(
            org_cumulative_spend_cents=8000, additional_cents=2000, spend_ceiling_cents=10_000
        )
        assert d.allowed is True

    def test_zero_ceiling_kill_switch(self) -> None:
        d = evaluate_org_spend_ceiling(org_cumulative_spend_cents=0, additional_cents=1, spend_ceiling_cents=0)
        assert d.allowed is False

    def test_already_at_ceiling_blocks_new_spend(self) -> None:
        # At ceiling with a new (non-zero) run: projected exceeds the ceiling.
        d = evaluate_org_spend_ceiling(
            org_cumulative_spend_cents=10_000, additional_cents=1, spend_ceiling_cents=10_000
        )
        assert d.allowed is False


# ---------------------------------------------------------------------------
# evaluate_spend_ceilings (combined)
# ---------------------------------------------------------------------------


class TestEvaluateSpendCeilings:
    def test_both_unlimited_allowed(self) -> None:
        d = evaluate_spend_ceilings(
            run_cost_so_far_cents=500,
            max_run_cost_cents=None,
            org_cumulative_spend_cents=100,
            spend_ceiling_cents=None,
        )
        assert d.allowed is True

    def test_run_violation_reported_first(self) -> None:
        d = evaluate_spend_ceilings(
            run_cost_so_far_cents=2000,
            estimated_next_step_cents=0,
            max_run_cost_cents=1000,
            org_cumulative_spend_cents=0,
            spend_ceiling_cents=10_000,
        )
        assert d.allowed is False
        assert d.reason == RUN_CEILING_EXCEEDED

    def test_org_violation_reported_when_run_ok(self) -> None:
        d = evaluate_spend_ceilings(
            run_cost_so_far_cents=100,
            estimated_next_step_cents=200,
            max_run_cost_cents=1000,
            org_cumulative_spend_cents=9900,
            spend_ceiling_cents=10_000,
        )
        assert d.allowed is False
        assert d.reason == ORG_CEILING_EXCEEDED

    def test_projects_org_total_on_success(self) -> None:
        d = evaluate_spend_ceilings(
            run_cost_so_far_cents=100,
            estimated_next_step_cents=50,
            max_run_cost_cents=1000,
            org_cumulative_spend_cents=900,
            spend_ceiling_cents=10_000,
        )
        assert d.allowed is True
        assert d.projected_org_cumulative_cents == 1050

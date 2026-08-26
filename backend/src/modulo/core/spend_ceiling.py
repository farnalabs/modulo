"""Hard spend ceilings — per-run and per-org.

These ceilings are a HARD stop on billable work (spec §5.1 cost controls):

- *Per-run ceiling* (``max_run_cost_cents``): a single run must never be billed
  beyond this. Enforced at the terminal ledger block (post-hoc): once a run has
  incurred cost, ``finalize_cost`` refuses the daily-ledger write and
  terminalizes the run as ``cost_ceiling_exceeded`` (the org's consumed total is
  NOT incremented) if the run's recorded cost exceeds the ceiling. This is a
  *post-hoc ledger refusal*, not a pre-spawn halt — a run that overshoots its
  ceiling is still billed up to the point it completed, then stopped from
  accruing further.
- *Per-org ceiling* (``spend_ceiling_cents``): the organisation's lifetime
  budget. Enforced on TWO surfaces: (1) a FAIL-OPEN pre-step gate in the
  executor (``PipelineExecutor._check_spend_ceiling_gate``) halts a brand-new
  run BEFORE any billable step when the org has zero remaining budget, and (2)
  the terminal ledger block refuses the ledger when a run would push the org's
  consumed total (``organisations.org_cumulative_spend_cents``) past its
  ceiling, terminalizing as ``cost_ceiling_exceeded`` and halting future runs.

Both values are stored in integer CENTS to avoid floating-point drift in the
limit comparison (the same discipline as the daily ledger's ``Decimal`` column,
but kept as ints so the gate comparison is exact and allocation-free).

The module is PURE: no DB, no FastAPI. The executor / finalize path reads the
columns and calls ``evaluate_spend_ceilings``; the API layer converts USD
<-> cents at the boundary. Every branch is unit-tested in
``tests/unit/core/test_spend_ceiling.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

# Machine-stable refusal reasons (consumed by the executor / finalize path and
# surfaced as a run ``error_code``). Kept as plain strings so they serialise
# cleanly and match ``cost_controller``'s ``daily_limit_exceeded`` convention.
RUN_CEILING_EXCEEDED: Literal["run_cost_ceiling_exceeded"] = "run_cost_ceiling_exceeded"
ORG_CEILING_EXCEEDED: Literal["org_spend_ceiling_exceeded"] = "org_spend_ceiling_exceeded"

_CeilingReason = Literal["", "run_cost_ceiling_exceeded", "org_spend_ceiling_exceeded"]


@dataclass(frozen=True)
class SpendCeilingDecision:
    """The outcome of a ceiling evaluation.

    ``allowed`` is False exactly when a ceiling is violated. ``reason`` is the
    stable machine code; ``message`` is a human-readable explanation (never
    logged as a secret). ``projected_org_cumulative_cents`` is the org's
    cumulative spend that *would* result (run cost included) — used by the
    caller to decide whether to increment the persisted counter.
    """

    allowed: bool
    reason: _CeilingReason
    message: str
    projected_org_cumulative_cents: int


def cents_from_usd(value: Decimal | float | str | None) -> int | None:
    """Convert a USD amount (or None) to integer cents, or None when absent.

    ``None`` is the canonical "no ceiling configured" sentinel at every layer
    (the API sends ``null``, the model column is NULL). A ``NaN`` / ``Infinity``
    decimal degrades to None rather than raising, so a corrupted stored value
    never trips a ceiling check (fail-open on bad input — the daily-limit path
    shares this stance).
    """
    if value is None:
        return None
    try:
        cents = int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return cents


def _format_usd(cents: int) -> str:
    return f"{cents / 100:.2f}"


def evaluate_run_spend_ceiling(
    *,
    run_cost_so_far_cents: int,
    estimated_next_step_cents: int = 0,
    max_run_cost_cents: int | None,
) -> SpendCeilingDecision:
    """Refuse a run whose recorded cumulative cost passes its per-run ceiling.

    This is evaluated at the terminal ledger block (post-hoc): ``run_cost_so_far_cents``
    is the run's recorded cumulative cost (from completed nodes) and
    ``estimated_next_step_cents`` is a conservative estimate of any further
    billable step (LLM + sandbox). When their sum would exceed
    ``max_run_cost_cents`` the decision is ``allowed=False`` with reason
    ``run_cost_ceiling_exceeded`` — the run's ledger write is refused and it is
    terminalized as ``cost_ceiling_exceeded``.

    A ``None`` ceiling means "unlimited" (no check). A ceiling of ``0`` blocks
    every run that would cost anything (``projected > 0``), which is the
    intended "kill switch" semantics for a zero per-run budget.

    Args:
        run_cost_so_far_cents: already-recorded run cost in cents (>= 0).
        estimated_next_step_cents: conservative next-step estimate in cents
            (>= 0). Defaults to 0 — the gate still catches an already-over run.
        max_run_cost_cents: per-run hard ceiling in cents, or None.

    Returns:
        A ``SpendCeilingDecision``. ``projected_org_cumulative_cents`` is left
        at ``run_cost_so_far_cents`` (the org view is owned by
        ``evaluate_org_spend_ceiling``).
    """
    if run_cost_so_far_cents < 0:
        run_cost_so_far_cents = 0
    if estimated_next_step_cents < 0:
        estimated_next_step_cents = 0
    if max_run_cost_cents is not None and max_run_cost_cents >= 0:
        projected = run_cost_so_far_cents + estimated_next_step_cents
        if projected > max_run_cost_cents:
            return SpendCeilingDecision(
                allowed=False,
                reason=RUN_CEILING_EXCEEDED,
                message=(
                    f"Run cost {_format_usd(projected)} USD would exceed the per-run "
                    f"ceiling of {_format_usd(max_run_cost_cents)} USD."
                ),
                projected_org_cumulative_cents=run_cost_so_far_cents,
            )
    return SpendCeilingDecision(
        allowed=True,
        reason="",
        message="",
        projected_org_cumulative_cents=run_cost_so_far_cents,
    )


def evaluate_org_spend_ceiling(
    *,
    org_cumulative_spend_cents: int,
    additional_cents: int = 0,
    spend_ceiling_cents: int | None,
) -> SpendCeilingDecision:
    """Refuse a run that would push the org past its lifetime spend ceiling.

    ``org_cumulative_spend_cents`` is the org's persisted consumed total;
    ``additional_cents`` is the run's cost being added now. When their sum would
    exceed ``spend_ceiling_cents`` the decision is ``allowed=False`` with reason
    ``org_spend_ceiling_exceeded``. The executor's FAIL-OPEN pre-step gate also
    halts brand-new runs before any billable step when the org has zero remaining
    budget, but the authoritative refusal happens here at the terminal ledger
    block.

    A ``None`` ceiling means "unlimited". A ceiling of ``0`` blocks every run
    once any spend is pending (``projected > 0``).

    Args:
        org_cumulative_spend_cents: org consumed total in cents (>= 0).
        additional_cents: the run's cost being added in cents (>= 0).
        spend_ceiling_cents: org lifetime hard ceiling in cents, or None.

    Returns:
        A ``SpendCeilingDecision`` with ``projected_org_cumulative_cents`` set to
        the resulting org total.
    """
    if org_cumulative_spend_cents < 0:
        org_cumulative_spend_cents = 0
    if additional_cents < 0:
        additional_cents = 0
    projected = org_cumulative_spend_cents + additional_cents
    if spend_ceiling_cents is not None and spend_ceiling_cents >= 0 and projected > spend_ceiling_cents:
        return SpendCeilingDecision(
            allowed=False,
            reason=ORG_CEILING_EXCEEDED,
            message=(
                f"Organisation spend {_format_usd(projected)} USD would exceed the "
                f"spend ceiling of {_format_usd(spend_ceiling_cents)} USD "
                f"(remaining budget {_format_usd(max(spend_ceiling_cents - org_cumulative_spend_cents, 0))} USD)."
            ),
            projected_org_cumulative_cents=projected,
        )
    return SpendCeilingDecision(
        allowed=True,
        reason="",
        message="",
        projected_org_cumulative_cents=projected,
    )


def evaluate_spend_ceilings(
    *,
    run_cost_so_far_cents: int,
    estimated_next_step_cents: int = 0,
    max_run_cost_cents: int | None,
    org_cumulative_spend_cents: int,
    spend_ceiling_cents: int | None,
) -> SpendCeilingDecision:
    """Evaluate BOTH ceilings, returning the first violation (run takes priority).

    The per-run check is evaluated first so a run that is over its own ceiling is
    reported as such rather than being masked by an org-ceiling violation. The
    returned ``projected_org_cumulative_cents`` reflects the org total that would
    result (run cost included) — used by the finalize path to increment the
    persisted counter on success.

    Args:
        run_cost_so_far_cents: run recorded cumulative cost in cents.
        estimated_next_step_cents: conservative next-step estimate in cents.
        max_run_cost_cents: per-run ceiling in cents, or None.
        org_cumulative_spend_cents: org consumed total in cents.
        spend_ceiling_cents: org lifetime ceiling in cents, or None.

    Returns:
        The first violating ``SpendCeilingDecision`` (``allowed=False``), or an
        ``allowed=True`` decision when neither ceiling is breached.
    """
    run_decision = evaluate_run_spend_ceiling(
        run_cost_so_far_cents=run_cost_so_far_cents,
        estimated_next_step_cents=estimated_next_step_cents,
        max_run_cost_cents=max_run_cost_cents,
    )
    if not run_decision.allowed:
        return run_decision
    return evaluate_org_spend_ceiling(
        org_cumulative_spend_cents=org_cumulative_spend_cents,
        additional_cents=run_cost_so_far_cents + estimated_next_step_cents,
        spend_ceiling_cents=spend_ceiling_cents,
    )

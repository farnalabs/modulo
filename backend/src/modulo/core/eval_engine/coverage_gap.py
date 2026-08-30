"""Coverage-gap signal for variant runs (FAR-381).

A coverage gap is the state where the variant **outputs** genuinely diverge yet
the eval suite **scored them the same** — i.e. the eval cannot differentiate
the variants. PRD §8.19 ("variants diverged but evals did not differentiate")
wants this surfaced as a *signal that the eval SUITE is insufficient*, not a
dashboard convenience.

This is a **pure read-model** over the existing
``VariantGroup -> Run -> EvalResult`` lineage (see ``data model`` in the module
docstring below) — there is **no new table, no migration**. It is deterministic
(terminal runs only, canonical serialisation, stable ordering) and org-scoped:
every query injects the explicit ``organisation_id`` predicate (``modulo_app``
is NOBYPASSRLS so RLS also enforces org scoping; the predicate is the PRIMARY
isolation control; ``set_rls_org`` remains defense-in-depth).

Data model it reads (already on main):
* ``Run`` — ``variant_group_id``, ``batch_id`` (one batch = all runs fired
  together share ``batch_id``), ``variant_config_snapshot`` (JSON holding
  ``variant_id`` / ``variant_name``), ``outputs_json``.
* ``EvalResult`` — ``run_id``, ``eval_id``, ``score`` (float, nullable),
  ``passed``.

Heuristics chosen (and why):
* **Variant divergence** — measured on the *serialised variant outputs* via a
  normalized edit distance (``1 - difflib.SequenceMatcher.ratio()`` over the
  canonical JSON of each variant run's ``outputs_json``). Outputs are often
  unstructured or node-keyed, so a structure-independent string-similarity
  gradient is the stable default: two variants that produced near-identical
  output score a low divergence; two that produced materially different output
  score a high one. ``sort_keys`` + compact separators make it deterministic
  across calls, and ``default=str`` keeps it total on non-JSON leaves.
* **Eval differentiation** — the population standard deviation of the
  per-variant eval metric **within one ``eval_id``** (``score`` when present,
  else ``passed`` as ``1.0/0.0``). Grouping by ``eval_id`` guarantees a single
  eval scale per comparison, so an absolute std is meaningful; a near-zero std
  means the eval scored every variant ~the same (it did not differentiate).
* **Statistical significance** — a signal is emitted only when at least
  ``min_runs`` (default 3) terminal runs carry eval data. Below that the result
  is ``insufficient_data``, which suppresses llm-judge high-variance false
  positives.

Gap condition (deterministic):
    ``variant_divergence >= divergence_threshold`` (variants truly differ)
    ``AND`` ``eval_differentiation < differentiation_threshold`` (an eval could
    not tell them apart)
    => ``has_gap`` with ``recommended_action="improve_evals"`` (route to
       eval-quality improvement). Otherwise ``recommended_action="ok"``.

The ``divergence_threshold`` is configurable per suite via the endpoint request
(``threshold`` query param); ``differentiation_threshold`` is a module default
(not surfaced on the endpoint) because it is an eval-quality calibration, not a
per-suite operator dial.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.eval_run import get_evals_for_runs
from modulo.db.crud.variant_group import get_batch_runs
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.run import TERMINAL_STATUSES, Run

_log = logging.getLogger(__name__)

DEFAULT_MIN_RUNS = 3
DEFAULT_DIVERGENCE_THRESHOLD = 0.15
DEFAULT_DIFFERENTIATION_THRESHOLD = 0.05


@dataclass
class EvalCoverageGap:
    """Coverage-gap verdict for a single eval definition within a batch.

    ``status`` reflects whether the eval had enough data to compute a
    differentiation verdict: ``"complete"`` when at least two distinct variants
    carried a value for this eval; ``"insufficient_data"`` when only one variant
    (or none) did — in that case ``has_gap`` is always ``False`` and
    ``recommended_action`` is always ``"ok"``, because a single-sample eval is a
    data-coverage artifact, NOT an eval-quality deficiency.
    """

    eval_id: UUID
    eval_name: str
    variant_divergence: float
    eval_score_spread: float
    has_gap: bool
    reason: str
    recommended_action: Literal["improve_evals", "ok"]
    status: Literal["insufficient_data", "complete"] = "complete"


@dataclass
class CoverageGapSummary:
    """Evaluated coverage-gap state for a batch or variant group."""

    status: Literal["insufficient_data", "complete"]
    batch_id: UUID | None
    variant_group_id: UUID | None
    run_count: int
    min_runs: int
    variant_divergence: float
    divergence_threshold: float
    differentiation_threshold: float
    evals: list[EvalCoverageGap] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "variant_group_id": str(self.variant_group_id) if self.variant_group_id else None,
            "run_count": self.run_count,
            "min_runs": self.min_runs,
            "variant_divergence": self.variant_divergence,
            "divergence_threshold": self.divergence_threshold,
            "differentiation_threshold": self.differentiation_threshold,
            "evals": [
                {
                    "eval_id": str(g.eval_id),
                    "eval_name": g.eval_name,
                    "variant_divergence": g.variant_divergence,
                    "eval_score_spread": g.eval_score_spread,
                    "has_gap": g.has_gap,
                    "reason": g.reason,
                    "recommended_action": g.recommended_action,
                    "status": g.status,
                }
                for g in self.evals
            ],
        }


# ---------------------------------------------------------------------------
# Pure (in-memory, side-effect-free) heuristics — unit-testable without a DB.
# ---------------------------------------------------------------------------


def _canonical_json(value: object) -> str:
    """Deterministic, compact serialisation for output comparison."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def _output_similarity(a: object, b: object) -> float:
    """Normalized edit-distance similarity of two variant outputs (0..1)."""
    sa = _canonical_json(a)
    sb = _canonical_json(b)
    if sa == sb:
        return 1.0
    return float(SequenceMatcher(None, sa, sb).ratio())


def compute_variant_divergence(outputs: Sequence[object]) -> float:
    """Divergence of variant outputs (``1 - mean pairwise similarity``).

    ``0.0`` means the outputs are identical (no divergence); ``1.0`` means
    maximally different. Returns ``0.0`` when fewer than two outputs are
    provided (no comparison possible). Round to 4 decimals so the value is a
    stable, readable signal.
    """
    outputs = list(outputs)
    if len(outputs) < 2:
        return 0.0
    pairwise = [
        _output_similarity(outputs[i], outputs[j]) for i in range(len(outputs)) for j in range(i + 1, len(outputs))
    ]
    similarity = sum(pairwise) / len(pairwise)
    divergence = 1.0 - similarity
    return round(max(0.0, divergence), 4)


def compute_eval_differentiation(values: Sequence[float]) -> float:
    """Population standard deviation of an eval's per-variant metric.

    Grouped by ``eval_id``, so the values share one scale. A near-zero value
    means the eval scored each variant ~the same and therefore could not
    differentiate them. Returns ``0.0`` when fewer than two values are given.
    """
    nums = [float(v) for v in values if v is not None]
    if len(nums) < 2:
        return 0.0
    mean = sum(nums) / len(nums)
    variance = sum((v - mean) ** 2 for v in nums) / len(nums)
    return round(float(math.sqrt(variance)), 4)


def _eval_metric(result: object) -> float:
    """One comparable per-variant eval metric (score else pass flag)."""
    score = getattr(result, "score", None)
    if score is not None:
        return float(score)
    return 1.0 if bool(getattr(result, "passed", False)) else 0.0


def _variant_key(run: object) -> str | None:
    """Stable identity of the variant a run belongs to.

    Prefers the frozen ``variant_id`` (a stable persisted UUID), then falls back
    to ``variant_name``, then ``None`` when the snapshot carries neither.
    ``None``-keyed runs group together and only ever produce a single "variant",
    yielding ``0.0`` divergence (no cross-variant comparison possible).
    """
    snapshot = getattr(run, "variant_config_snapshot", None)
    if not isinstance(snapshot, dict):
        return None
    variant_id = snapshot.get("variant_id")
    if variant_id is not None:
        return str(variant_id)
    variant_name = snapshot.get("variant_name")
    if variant_name is not None:
        return str(variant_name)
    return None


def _insufficient_summary(
    *,
    run_count: int,
    batch_id: UUID | None = None,
    variant_group_id: UUID | None = None,
    min_runs: int = DEFAULT_MIN_RUNS,
    divergence_threshold: float = DEFAULT_DIVERGENCE_THRESHOLD,
    differentiation_threshold: float = DEFAULT_DIFFERENTIATION_THRESHOLD,
) -> CoverageGapSummary:
    """Build the ``insufficient_data`` summary shared by both callers."""
    return CoverageGapSummary(
        status="insufficient_data",
        batch_id=batch_id,
        variant_group_id=variant_group_id,
        run_count=run_count,
        min_runs=min_runs,
        variant_divergence=0.0,
        divergence_threshold=divergence_threshold,
        differentiation_threshold=differentiation_threshold,
        evals=[],
    )


def _insufficient_data_gap(eval_id: UUID, eval_names: Mapping[UUID, str], variant_divergence: float) -> EvalCoverageGap:
    """Eval verdict when fewer than two distinct variants carry a value.

    A single-sample eval is a data-coverage artifact, NOT an eval-quality
    deficiency — never emit a gap for it (Major 1).
    """
    return EvalCoverageGap(
        eval_id=eval_id,
        eval_name=eval_names.get(eval_id, "unknown"),
        variant_divergence=variant_divergence,
        eval_score_spread=0.0,
        has_gap=False,
        reason=(
            "Only one distinct variant has data for this eval; a "
            "coverage-gap verdict needs at least two. Marked "
            "insufficient_data."
        ),
        recommended_action="ok",
        status="insufficient_data",
    )


def _verdict_for_eval(
    eval_id: UUID,
    values_by_variant: Mapping[str, float],
    *,
    eval_names: Mapping[UUID, str],
    variant_divergence: float,
    divergence_threshold: float,
    differentiation_threshold: float,
) -> EvalCoverageGap:
    """Compute the coverage-gap verdict for one eval definition.

    One value per distinct variant (the same population that produced
    ``variant_divergence``), so the two sides of the ``has_gap`` AND measure the
    same entities (Major 3).
    """
    values = list(values_by_variant.values())
    if len(values) < 2:
        return _insufficient_data_gap(eval_id, eval_names, variant_divergence)

    differentiation = compute_eval_differentiation(values)
    spread = round(max(values) - min(values), 4)

    has_gap = variant_divergence >= divergence_threshold and differentiation < differentiation_threshold
    if has_gap:
        reason = "Variants diverged but this eval could not differentiate them; route to eval-quality improvement."
        action: Literal["improve_evals", "ok"] = "improve_evals"
    elif variant_divergence >= divergence_threshold:
        reason = "Variants diverged and this eval differentiated them adequately."
        action = "ok"
    else:
        reason = "Variants did not diverge, so no coverage gap exists for this eval."
        action = "ok"

    return EvalCoverageGap(
        eval_id=eval_id,
        eval_name=eval_names.get(eval_id, "unknown"),
        variant_divergence=variant_divergence,
        eval_score_spread=spread,
        has_gap=has_gap,
        reason=reason,
        recommended_action=action,
    )


def evaluate_coverage_gap(
    runs: Sequence[object],
    eval_results: Sequence[object],
    *,
    eval_names: Mapping[UUID, str] | None = None,
    min_runs: int = DEFAULT_MIN_RUNS,
    divergence_threshold: float = DEFAULT_DIVERGENCE_THRESHOLD,
    differentiation_threshold: float = DEFAULT_DIFFERENTIATION_THRESHOLD,
    batch_id: UUID | None = None,
    variant_group_id: UUID | None = None,
) -> CoverageGapSummary:
    """Evaluate the coverage-gap signal from already-loaded run/evals.

    Pure and side-effect-free. The caller (``compute_coverage_gap``) is
    responsible for loading ``runs``, ``eval_results``, and ``eval_names``
    org-scoped. Only **terminal** runs with at least one (non-guardrail) eval
    result count as data points, so the signal is deterministic across two calls.
    """
    if min_runs < 1:
        raise ValueError(f"min_runs must be >= 1, got {min_runs}")
    if not 0.0 <= divergence_threshold <= 1.0:
        raise ValueError(f"divergence_threshold must be in [0, 1], got {divergence_threshold}")
    if differentiation_threshold < 0.0:
        raise ValueError(f"differentiation_threshold must be >= 0, got {differentiation_threshold}")

    eval_names = eval_names or {}

    # Data points: terminal runs that have at least one eval result.
    terminal_run_ids = {getattr(r, "id", None) for r in runs if getattr(r, "status", None) in TERMINAL_STATUSES}
    results_by_run: dict[UUID, list[object]] = defaultdict(list)
    for result in eval_results:
        run_id = getattr(result, "run_id", None)
        if run_id is None or run_id not in terminal_run_ids:
            continue
        results_by_run[run_id].append(result)
    data_point_run_ids = {run_id for run_id, results in results_by_run.items() if results}
    run_count = len(data_point_run_ids)

    if run_count < min_runs:
        return _insufficient_summary(
            run_count=run_count,
            batch_id=batch_id,
            variant_group_id=variant_group_id,
            min_runs=min_runs,
            divergence_threshold=divergence_threshold,
            differentiation_threshold=differentiation_threshold,
        )

    # --- Variant divergence ----------------------------------------------
    # One representative terminal run per distinct variant (stable order).
    # ``variant_key_by_run`` maps every data-point run to its variant key so the
    # differentiation pass below can restrict itself to the SAME population.
    runs_by_variant: dict[str, object] = {}
    variant_key_by_run: dict[UUID, str] = {}
    for run in runs:
        run_id = getattr(run, "id", None)
        if run_id not in data_point_run_ids:
            continue
        key = _variant_key(run)
        if key is None:
            continue  # a run without a variant identity cannot participate
        variant_key_by_run[run_id] = key
        if key not in runs_by_variant:
            runs_by_variant[key] = run

    variant_outputs = [getattr(run, "outputs_json", None) for run in runs_by_variant.values()]
    variant_outputs = [o for o in variant_outputs if o is not None]
    variant_divergence = compute_variant_divergence(variant_outputs)

    # --- Eval differentiation (per eval_id, ONE value per variant) -------
    # Both sides of the has_gap AND must be computed on the SAME entities:
    # divergence uses ``runs_by_variant`` (one representative run per distinct
    # variant); differentiation must therefore also use one value per distinct
    # variant, taken ONLY from that variant's representative run. Counting every
    # run per variant would weight run-count rather than variants and bias
    # differentiation down (inflating false-gap risk).
    results_by_eval_variant: dict[UUID, dict[str, float]] = defaultdict(dict)
    for result in eval_results:
        run_id = getattr(result, "run_id", None)
        if run_id not in data_point_run_ids:
            continue
        key = variant_key_by_run.get(run_id)
        if key is None:
            continue
        representative = runs_by_variant.get(key)
        if representative is None or getattr(representative, "id", None) != run_id:
            continue  # only the variant's representative run contributes
        eval_id = getattr(result, "eval_id", None)
        if eval_id is None:
            continue
        results_by_eval_variant[eval_id][key] = _eval_metric(result)

    evals: list[EvalCoverageGap] = []
    for eval_id, values_by_variant in results_by_eval_variant.items():
        evals.append(
            _verdict_for_eval(
                eval_id,
                values_by_variant,
                eval_names=eval_names,
                variant_divergence=variant_divergence,
                divergence_threshold=divergence_threshold,
                differentiation_threshold=differentiation_threshold,
            )
        )

    evals.sort(key=lambda g: str(g.eval_id))
    return CoverageGapSummary(
        status="complete",
        batch_id=batch_id,
        variant_group_id=variant_group_id,
        run_count=run_count,
        min_runs=min_runs,
        variant_divergence=variant_divergence,
        divergence_threshold=divergence_threshold,
        differentiation_threshold=differentiation_threshold,
        evals=evals,
    )


# ---------------------------------------------------------------------------
# Org-scoped DB loading (the only place the signal touches the database).
# ---------------------------------------------------------------------------


async def compute_coverage_gap(
    session: AsyncSession,
    *,
    org_id: UUID,
    batch_id: UUID | None = None,
    variant_group_id: UUID | None = None,
    min_runs: int = DEFAULT_MIN_RUNS,
    divergence_threshold: float = DEFAULT_DIVERGENCE_THRESHOLD,
) -> CoverageGapSummary:
    """Load a batch/group org-scoped and evaluate the coverage-gap signal.

    Every query injects the explicit ``organisation_id`` predicate (the only
    isolation control for BYPASSRLS ``modulo_app``). Exactly one of
    ``batch_id`` / ``variant_group_id`` must be supplied.
    """
    if batch_id is None and variant_group_id is None:
        raise ValueError("provide batch_id or variant_group_id")

    runs = await _load_runs(session, org_id=org_id, batch_id=batch_id, variant_group_id=variant_group_id)
    if not runs:
        return _insufficient_summary(
            run_count=0,
            batch_id=batch_id,
            variant_group_id=variant_group_id,
            min_runs=min_runs,
            divergence_threshold=divergence_threshold,
        )

    run_ids = [run.id for run in runs]
    eval_results = await get_evals_for_runs(session, org_id=org_id, run_ids=run_ids)

    eval_ids = {getattr(er, "eval_id", None) for er in eval_results}
    eval_names = await _load_eval_names(session, org_id=org_id, eval_ids={e for e in eval_ids if e is not None})

    return evaluate_coverage_gap(
        runs,
        eval_results,
        eval_names=eval_names,
        min_runs=min_runs,
        divergence_threshold=divergence_threshold,
        batch_id=batch_id,
        variant_group_id=variant_group_id,
    )


async def _load_runs(
    session: AsyncSession,
    *,
    org_id: UUID,
    batch_id: UUID | None,
    variant_group_id: UUID | None,
) -> list[Run]:
    """Org-scoped runs for a batch (by ``batch_id``) or a variant group.

    The batch path reuses the existing compare loader (org-scoped by
    ``batch_id``); the group path queries ``variant_group_id`` with an explicit
    ``organisation_id`` predicate so a cross-org group can never be read. Both
    are deterministic (ordered by ``created_at``) so two calls agree.
    """
    if batch_id is not None:
        return await get_batch_runs(session, org_id=org_id, batch_id=batch_id)
    result = await session.execute(
        select(Run)
        .where(Run.organisation_id == org_id, Run.variant_group_id == variant_group_id)
        .order_by(Run.created_at)
    )
    return list(result.scalars().all())


async def _load_eval_names(
    session: AsyncSession,
    *,
    org_id: UUID,
    eval_ids: set[UUID],
) -> dict[UUID, str]:
    """Org-scoped ``eval_id -> name`` for the referenced eval definitions."""
    if not eval_ids:
        return {}
    result = await session.execute(
        select(EvalDefinition.id, EvalDefinition.name).where(
            EvalDefinition.id.in_(eval_ids),
            EvalDefinition.organisation_id == org_id,
        )
    )
    return {row.id: row.name for row in result.all()}

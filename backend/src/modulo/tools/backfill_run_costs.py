"""Maintenance backfill: recompute ``cost_breakdown`` + ``total_cost_usd`` for
runs finalised before the org cost-component seed fix (PR #772).

A startup seed bug meant NO org ever received its default cost components, so
every run finalised with an EMPTY ``cost_breakdown`` and ``total_cost_usd =
0.000000`` even though the engine stored the ENRICHED per-node data
(``node_token_usage`` with ``wall_clock_time_ms``, ``sandbox_by_map``, token
fields; ``outputs_json``). The seed fix only prevents NEW runs from being
affected — ALREADY-FINALISED runs stay at $0 unless recomputed.

This script recomputes the cost fields for the affected terminal runs using the
EXISTING cost engine (``load_live_components`` / ``build_telemetry`` /
``build_cost_breakdown``) — it NEVER reimplements formulas — and writes them
back.

It touches RUN ROWS ONLY. It does NOT touch the spend ledger
(``OrgDailyRunCount`` / ``ledger_written`` / ``check_and_record_spend``): the
backfill is not a terminal finalisation and must not create or mutate ledger
rows. Ledger-based period totals will NOT include backfilled history — that is
accepted; the runs table is the component-report source
(``build_cost_report_buckets``).

Usage::

    uv run python -m modulo.tools.backfill_run_costs --dry-run
    uv run python -m modulo.tools.backfill_run_costs --org-id <uuid>
    uv run python -m modulo.tools.backfill_run_costs --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.cost_controller.breakdown.aggregate import build_cost_breakdown
from modulo.core.cost_controller.breakdown.params import CostComponentConfig, build_telemetry
from modulo.core.cost_controller.finalize import _write_back_node_cost, load_live_components
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

__all__ = ["BackfillSummary", "backfill_run_costs"]

_BATCH_SIZE = 500

# Fields folded from ``outputs_json`` into a node-token-usage entry ONLY when
# the entry lacks them (gap-fill for runs stored before union enrichment).
_GAP_FILL_FIELDS = ("wall_clock_time_ms", "model_cost_usd", "model_cost_raw_usd")


@dataclass
class BackfillSummary:
    """Aggregated outcome of a backfill pass (orgs / candidates / writes)."""

    orgs_scanned: int = 0
    candidates: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    no_components_orgs: list[uuid.UUID] = field(default_factory=list)

    def as_lines(self, *, dry_run: bool) -> list[str]:
        """Human-readable summary lines for the CLI output."""
        verb = "would update" if dry_run else "updated"
        lines = [
            f"organisations scanned: {self.orgs_scanned}",
            f"candidate runs:        {self.candidates}",
            f"runs {verb}:            {self.updated}",
            f"runs skipped:          {self.skipped}",
            f"runs errored:          {self.errors}",
        ]
        if self.no_components_orgs:
            shown = ", ".join(str(o) for o in self.no_components_orgs[:5])
            suffix = "…" if len(self.no_components_orgs) > 5 else ""
            lines.append(f"orgs skipped (no cost components): {shown}{suffix}")
        return lines


def _is_already_done(run: Run) -> bool:
    """Idempotency guard — a run with a non-empty breakdown or positive total
    already carries a cost write and must NOT be overwritten.
    """
    if isinstance(run.cost_breakdown, list) and run.cost_breakdown:
        return True
    return run.total_cost_usd is not None and run.total_cost_usd > 0


def _candidate_filter(org_id: uuid.UUID) -> ColumnElement[bool]:
    """Terminal runs with enriched usage whose cost columns are still empty.

    ``node_token_usage`` must be non-NULL (nothing to recompute without it).
    ``total_cost_usd`` NULL/0 OR ``cost_breakdown`` NULL mark the affected
    class. A run with a non-empty ``cost_breakdown`` is excluded here AND by
    ``_is_already_done`` (defense in depth).
    """
    return and_(
        Run.organisation_id == org_id,
        Run.status.in_(TERMINAL_STATUSES),
        Run.node_token_usage.is_not(None),
        or_(
            Run.cost_breakdown.is_(None),
            Run.total_cost_usd.is_(None),
            Run.total_cost_usd == 0,
        ),
    )


def _gap_fill_union(union: dict[str, dict[str, Any]], outputs_json: Any) -> dict[str, dict[str, Any]]:
    """Fold ``outputs_json`` wall-clock/model-cost fields into union entries
    that lack them (gap-fill for runs stored before union enrichment). Never
    overwrites an existing union field; never touches token fields; no formula
    logic.
    """
    if not isinstance(outputs_json, dict):
        return union
    for node_id, node_output in outputs_json.items():
        entry = union.get(str(node_id))
        if not isinstance(entry, dict) or not isinstance(node_output, dict):
            continue
        out = node_output.get("output")
        if not isinstance(out, dict):
            out = node_output
        for key in _GAP_FILL_FIELDS:
            if key not in entry and isinstance(out.get(key), (int, float)):
                entry[key] = out[key]
    return union


def _recompute_cost(
    run: Run,
    components: list[CostComponentConfig],
) -> tuple[list[dict[str, Any]], Decimal, dict[str, dict[str, Any]]]:
    """Recompute a run's breakdown + total with the existing cost engine.

    ``node_token_usage`` is the ENRICHED stored union (the engine's telemetry
    input); ``outputs_json`` is only consulted to gap-fill wall-clock /
    model-cost fields for runs stored before union enrichment. Returns
    ``(breakdown, total, enriched_union)`` where ``enriched_union`` carries the
    per-node ``cost_usd`` from ``build_telemetry``'s single authority (the
    ``_write_back_node_cost`` semantics).
    """
    usage = run.node_token_usage
    if not isinstance(usage, dict) or not usage:
        raise ValueError("run has empty node_token_usage; nothing to backfill")
    enriched: dict[str, dict[str, Any]] = {}
    for node_id, entry in usage.items():
        if isinstance(entry, dict):
            enriched[str(node_id)] = dict(entry)
        else:
            enriched[str(node_id)] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    enriched = _gap_fill_union(enriched, run.outputs_json)
    telemetry, per_node_cost = build_telemetry(enriched, components)
    breakdown, total = build_cost_breakdown(telemetry, components, settings=get_settings())
    enriched = _write_back_node_cost(enriched, per_node_cost)
    return breakdown, total, enriched


async def _process_org(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    dry_run: bool,
    limit: int | None,
    summary: BackfillSummary,
) -> None:
    """Backfill candidate runs for ONE org inside a single transaction (RLS
    scoped). Run rows only — the spend ledger is never touched.
    """
    if limit is not None and summary.candidates >= limit:
        return
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        components = await load_live_components(session, org_id)
        if not any(c.enabled for c in components):
            # Zero enabled components — the seed fix hasn't deployed (or the
            # org has none configured). Do NOT write zeros over existing data.
            summary.no_components_orgs.append(org_id)
            _log.warning("no cost components for org %s — run after the seed fix deploys", org_id)
            return

        offset = 0
        while True:
            if limit is not None and summary.candidates >= limit:
                break
            batch_size = _BATCH_SIZE if limit is None else min(_BATCH_SIZE, limit - summary.candidates)
            rows = list(
                (
                    await session.execute(
                        select(Run)
                        .where(_candidate_filter(org_id))
                        .order_by(Run.created_at)
                        .offset(offset)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                break
            offset += len(rows)
            for run in rows:
                if limit is not None and summary.candidates >= limit:
                    break
                if _is_already_done(run):
                    summary.skipped += 1
                    continue
                try:
                    breakdown, total, enriched = _recompute_cost(run, components)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    summary.errors += 1
                    _log.exception("backfill_run_costs.run_failed", extra={"run_id": str(run.id)})
                    continue
                summary.candidates += 1
                if not dry_run:
                    run.total_cost_usd = total
                    run.cost_breakdown = breakdown
                    run.node_token_usage = enriched
                summary.updated += 1


async def backfill_run_costs(
    factory: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> BackfillSummary:
    """Recompute ``cost_breakdown`` + ``total_cost_usd`` for pre-fix runs.

    Args:
        factory: async session factory bound to the target database.
        org_id: restrict to one organisation. When ``None``, enumerate all orgs
            in SYSTEM CONTEXT (no RLS) — mirroring ``seed_cost_components`` —
            then backfill each org in its own RLS-scoped transaction.
        limit: maximum number of candidate runs to recompute.
        dry_run: report what would change without writing anything.

    Safety:
        * Idempotent — runs with a non-empty ``cost_breakdown`` or
          ``total_cost_usd > 0`` are never overwritten.
        * Orgs with ZERO enabled cost components are skipped with a clear
          message (never write zeros over existing data).
        * Per-run failures are isolated — one bad run never aborts the batch.
        * The spend ledger (``OrgDailyRunCount`` / ``ledger_written`` /
          ``check_and_record_spend``) is NEVER touched.
    """
    summary = BackfillSummary()
    if org_id is not None:
        org_ids: list[uuid.UUID] = [org_id]
    else:
        async with factory() as session:
            result = await session.execute(select(Organisation.id).order_by(Organisation.created_at))
            org_ids = [row[0] for row in result.all()]

    for oid in org_ids:
        if limit is not None and summary.candidates >= limit:
            break
        try:
            await _process_org(factory, oid, dry_run=dry_run, limit=limit, summary=summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            summary.errors += 1
            _log.exception("backfill_run_costs.org_failed", extra={"org_id": str(oid)})
        summary.orgs_scanned += 1
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m modulo.tools.backfill_run_costs",
        description=(
            "Recompute cost_breakdown + total_cost_usd for terminal runs finalised "
            "before the org cost-component seed fix (PR #772). Updates run rows only "
            "— never the spend ledger."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing anything")
    parser.add_argument("--limit", type=int, default=None, help="maximum number of candidate runs to recompute")
    parser.add_argument("--org-id", type=uuid.UUID, default=None, help="restrict to a single organisation id")
    return parser.parse_args(argv)


async def main() -> int:
    """CLI entrypoint. Returns the process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    args = _parse_args()
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    try:
        summary = await backfill_run_costs(factory, org_id=args.org_id, limit=args.limit, dry_run=args.dry_run)
    finally:
        await engine.dispose()
    for line in summary.as_lines(dry_run=args.dry_run):
        _log.info("%s", line)
    if summary.no_components_orgs:
        _log.info("no cost components — run after the seed fix deploys")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

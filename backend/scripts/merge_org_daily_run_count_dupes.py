"""Merge duplicate (org, NULL, date) rows in org_daily_run_counts.

The NULLS NOT DISTINCT migration (0065) fails loudly when duplicate org-level
rows already exist — the NULLs-distinct pause+terminal double-record bug may
already have produced them in production. This helper mirrors the migration's
pre-flight query and performs the merge under org-scoped RLS discipline.

Merge policy (normative, plan §9.1):
  * ``total_spend_usd`` = SUM of the duplicate rows.
  * ``run_count``      = SUM of the duplicate rows.
  * ``clamped``        = TRUE if ANY source row was clamped (clamped-OR
                        semantics) — a merged day was a clamped day.
  * The merged total is CLAMPED to the column ceiling (99999999.999999)
    before insert: two clamped rows sum to ~199999999.999998 → a write at the
    unclamped sum would overflow Numeric(14,6) and crash the helper + block
    the deploy gate.

Usage (from backend/):
    uv run python -m scripts.merge_org_daily_run_count_dupes

Requires DATABASE_URL (the app URL — the helper scopes per org via RLS, never
a superuser session). Optional --dry-run to report without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_COST_COLUMN_CAP = Decimal("99999999.999999")


def _preflight_query() -> str:
    return (
        "SELECT organisation_id, run_date, id, total_spend_usd, run_count, clamped "
        "FROM org_daily_run_counts WHERE team_id IS NULL "
        "ORDER BY organisation_id, run_date"
    )


async def _org_ids(session: AsyncSession) -> list[uuid.UUID]:
    rows = await session.execute(text("SELECT DISTINCT organisation_id FROM organisations"))
    return [uuid.UUID(str(r[0])) for r in rows.all()]


async def _merge_for_org(session: AsyncSession, org_id: uuid.UUID, dry_run: bool) -> int:
    """Merge duplicate org rows for one org under set_rls_org; returns count merged."""
    await set_rls_org(session, org_id)
    rows = await session.execute(
        text(
            "SELECT run_date, id, total_spend_usd, run_count, clamped FROM org_daily_run_counts "
            "WHERE team_id IS NULL ORDER BY run_date"
        )
    )
    by_date: dict[str, list[tuple]] = {}
    for r in rows.all():
        by_date.setdefault(str(r[0]), []).append(r)

    merged = 0
    for run_date, group in by_date.items():
        if len(group) < 2:
            continue
        total = Decimal(0)
        run_count = 0
        clamped = False
        keep_id = group[0][1]
        for _d, _rid, spend, count, clamped_flag in group:
            total += Decimal(str(spend))
            run_count += int(count)
            if clamped_flag:
                clamped = True
        if total > _COST_COLUMN_CAP:
            total = _COST_COLUMN_CAP
        if dry_run:
            print(
                f"[dry-run] org={org_id} date={run_date}: {len(group)} rows -> "
                f"total={total} run_count={run_count} clamped={clamped} (keep id={keep_id})",
                file=sys.stderr,
            )
            merged += 1
            continue
        # Keep the first row's id; delete the rest; update the survivor.
        for _d, rid, _spend, _count, _clamped_flag in group[1:]:
            await session.execute(text("DELETE FROM org_daily_run_counts WHERE id = :rid").bindparams(rid=rid))
        await session.execute(
            text(
                "UPDATE org_daily_run_counts SET total_spend_usd = :total, "
                "run_count = :count, clamped = :clamped WHERE id = :rid"
            ).bindparams(
                total=total,
                count=run_count,
                clamped=clamped,
                rid=keep_id,
            )
        )
        merged += 1
    return merged


async def _run(dry_run: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    total_merged = 0
    try:
        # Enumeration runs in SYSTEM CONTEXT (no set_rls_org) — exactly like the
        # seeder/probe enumeration; set_rls_org applies ONLY to the per-org merge.
        async with factory() as session:
            orgs = await _org_ids(session)
        for org_id in orgs:
            try:
                async with factory() as session, session.begin():
                    total_merged += await _merge_for_org(session, org_id, dry_run)
            except Exception as exc:
                print(f"[error] org={org_id}: {exc}", file=sys.stderr)
                raise
    finally:
        await engine.dispose()
    return total_merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report duplicate groups without writing")
    args = parser.parse_args()
    merged = asyncio.run(_run(dry_run=args.dry_run))
    print(f"Duplicate org-day groups merged: {merged}", file=sys.stderr)


if __name__ == "__main__":
    main()

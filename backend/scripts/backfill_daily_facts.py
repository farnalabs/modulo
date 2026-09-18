"""Backfill/repair the analytics daily facts + org spend ledger (FAR-200).

``run_daily_facts`` is materialized asynchronously: the live writer stamps a row
per terminal run at finalize time, and the ``analytics_facts_maintenance`` cron
(01:00 UTC daily) backfills any gaps. When the live writer is broken — e.g. the
FAR-200 stale-writer bug, where facts were snapshotted from the run's PRE-write
state (``status='running'``, NULL cost) — the daily backfill could NOT repair
recent days, because its anti-join only inserts runs with NO existing fact row.
A stale fact row suppressed the correction forever.

This tool runs the exact per-day maintenance the cron runs (``repair_stale_facts``
+ ``backfill_facts`` + ``backfill_ledger``) over a date range, so a manual repair
behaves identically to the daily pass. Every step is idempotent (anti-join,
``ON CONFLICT DO NOTHING``, ledger gap-fill), so it is safe to run at any time
and to re-run after partial completion.

Usage::

    python backfill_daily_facts.py                     # last 7 days, DRY-RUN
    python backfill_daily_facts.py --apply              # last 7 days, write
    python backfill_daily_facts.py --days 30 --apply    # last 30 days
    python backfill_daily_facts.py --start 2026-08-01 --end 2026-08-14 --apply

Dry-run by default: each day's work runs inside a transaction that is ROLLED
BACK, reporting the would-be repaired/inserted row counts. ``--apply`` commits
each day's transaction. A DB failure rolls back only the in-flight day and exits
1.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.analytics.maintenance import backfill_facts, backfill_ledger, repair_stale_facts


def _normalise_async_url(raw: str) -> str:
    """Ensure the URL carries an async driver usable by ``create_async_engine``."""
    if raw.startswith(("postgresql://", "postgresql+psycopg://")):
        return raw.replace("postgresql://", "postgresql+asyncpg://").replace(
            "postgresql+psycopg://", "postgresql+asyncpg://"
        )
    if raw.startswith(("mysql://", "mysql+pymysql://")):
        return raw.replace("mysql://", "mysql+aiomysql://").replace("mysql+pymysql://", "mysql+aiomysql://")
    return raw


def _resolve_range(args: argparse.Namespace) -> list[date]:
    """Newest-first day list for the requested range (default: last ``--days``)."""
    if args.start or args.end:
        end = args.end or datetime.now(UTC).date()
        start = args.start or (end - timedelta(days=max(args.days - 1, 0)))
        if start > end:
            raise ValueError("--start must be <= --end")
        days: list[date] = []
        cursor = end
        while cursor >= start:
            days.append(cursor)
            cursor -= timedelta(days=1)
        return days
    today = datetime.now(UTC).date()
    return [today - timedelta(days=offset) for offset in range(max(args.days, 0))]


async def _run_days(
    factory: Any,
    days: list[date],
    *,
    apply: bool,
) -> list[dict[str, Any]]:
    """Per-day repair+backfill+ledger; one transaction per day.

    Dry-run rolls each day back after executing, so ``backfill_facts``' internal
    ``repair_stale_facts`` runs but the day is never committed.
    """
    results: list[dict[str, Any]] = []
    async with factory() as session:
        for day in days:
            trans = await session.begin()
            try:
                await session.execute(text("SELECT set_config('timezone', 'UTC', true)"))
                repaired = await repair_stale_facts(session, day)
                inserted = await backfill_facts(session, day)
                ledger = await backfill_ledger(session, day)
                if not apply:
                    await trans.rollback()
                else:
                    await trans.commit()
            except asyncio.CancelledError:
                await trans.rollback()
                raise
            except Exception:
                await trans.rollback()
                raise
            results.append({"day": day.isoformat(), "repaired": repaired, "inserted": inserted, "ledger": ledger})
    return results


def _print_summary(results: list[dict[str, Any]], apply: bool, out: Any) -> None:
    mode = "APPLY" if apply else "DRY-RUN (rolled back)"
    print(f"backfill_daily_facts [{mode}]")
    print(f"{'run_date':<12}{'repaired':>9}{'inserted':>10}{'ledger_rows':>12}")
    total_repaired = total_inserted = total_ledger = 0
    for row in results:
        total_repaired += row["repaired"]
        total_inserted += row["inserted"]
        total_ledger += row["ledger"]
        print(
            f"{row['day']:<12}{row['repaired']:>9}{row['inserted']:>10}{row['ledger']:>12}",
            file=out,
        )
    print(
        f"{'TOTAL':<12}{total_repaired:>9}{total_inserted:>10}{total_ledger:>12}",
        file=out,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="backfill_daily_facts.py",
        description=(
            "Idempotent per-day repair+backfill of run_daily_facts and the org spend "
            "ledger (same functions the analytics_facts_maintenance cron runs)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit each day's repair/backfill; without this the run is a dry-run that writes nothing.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Backfill the last N days ending today (default: 7). Ignored when --start/--end are given.",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="First run_date to process (inclusive).",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Last run_date to process (inclusive; defaults to today).",
    )
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    try:
        days = _resolve_range(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    if not days:
        print("ERROR: empty date range.", file=sys.stderr)
        sys.exit(1)

    engine = create_async_engine(_normalise_async_url(raw))
    factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    try:
        results = asyncio.run(_run_days(factory, days, apply=args.apply))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        asyncio.run(engine.dispose())

    _print_summary(results, args.apply, sys.stdout)


if __name__ == "__main__":
    main()

"""Verify the node-telemetry backfill completed (FAR-129 P5).

Scans the FULL ``runs`` table (no sampling) and reports every row that is
still "legacy": ``node_telemetry_json IS NULL``. Each legacy row is classified
by why it remains:

- ``NULL both`` -- ``outputs_json`` is also NULL (a run that never produced
  per-node outputs).
- ``envelope-pattern`` -- ``outputs_json`` still matches the pre-split legacy
  envelope (at least one node value is a dict with an ``output`` key that is a
  dict); the backfill either skipped it as unsplittable or never ran on it.
- ``other`` -- ``outputs_json`` is set but does not match the legacy envelope.

The envelope detection runs in SQL against ``outputs_json::jsonb`` using jsonb
operators (the documented ``::jsonb`` cast -- see backend AGENTS.md "Stale
cleanup ``outputs_json`` comparison needs ``::jsonb`` cast") so semantically
equal JSON compares by jsonb semantics, never by text equality.

Exits 0 when zero legacy rows remain; exits 1 otherwise, printing the legacy
row ids. This tool NEVER writes.

Usage::

    DATABASE_URL=postgresql+asyncpg://... python verify_backfill_node_telemetry.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import create_engine, text

_TOTAL_SQL = "SELECT count(*) FROM runs"

#: Every legacy row (node_telemetry_json IS NULL) with its legacy reason. The
#: envelope-pattern detection uses ``outputs_json::jsonb`` + jsonb operators so
#: the JSON is compared by jsonb semantics (never text) -- the documented
#: pattern for JSON comparisons against this column.
_LEGACY_SQL = text(
    "SELECT id,\n"
    "       CASE\n"
    "         WHEN outputs_json IS NULL THEN 'NULL both'\n"
    "         WHEN (SELECT count(*) FROM jsonb_each(outputs_json::jsonb) AS kv\n"
    "               WHERE jsonb_typeof(kv.value) = 'object'\n"
    "                 AND kv.value ? 'output'\n"
    "                 AND jsonb_typeof(kv.value -> 'output') = 'object') > 0\n"
    "           THEN 'envelope-pattern'\n"
    "         ELSE 'other'\n"
    "       END AS reason\n"
    "FROM runs\n"
    "WHERE node_telemetry_json IS NULL\n"
    "ORDER BY id"
)


def _resolve_db_url(raw: str) -> str:
    """Convert a SQLAlchemy/async-style URL into a sync psycopg connection string."""
    url = raw
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
            break
    return url


def _run_verify(conn: Any, out: TextIO = sys.stdout) -> int:
    """Scan the full runs table and report legacy rows. Returns the exit code."""
    total = conn.execute(text(_TOTAL_SQL)).scalar_one()
    rows = conn.execute(_LEGACY_SQL).fetchall()
    by_reason: dict[str, int] = {}
    legacy_ids: list[str] = []
    for row in rows:
        by_reason[row.reason] = by_reason.get(row.reason, 0) + 1
        legacy_ids.append(str(row.id))
    print(f"total runs: {total}", file=out)
    print(f"legacy rows remaining: {len(rows)}", file=out)
    for reason in ("NULL both", "envelope-pattern", "other"):
        print(f"  {reason}: {by_reason.get(reason, 0)}", file=out)
    if rows:
        print("legacy row ids:", file=out)
        for rid in legacy_ids:
            print(f"  {rid}", file=out)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="verify_backfill_node_telemetry.py",
        description="Verify the node-telemetry backfill completed (never writes).",
    )
    parser.parse_args()

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)
    engine = create_engine(_resolve_db_url(raw))
    try:
        with engine.connect() as conn:
            rc = _run_verify(conn)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        engine.dispose()
    sys.exit(rc)


if __name__ == "__main__":
    main()

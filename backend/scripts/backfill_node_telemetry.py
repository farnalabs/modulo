"""Backfill legacy ``runs`` rows into the node-telemetry split (FAR-129 P5).

Since FAR-125 (P1/P2) the split write path is live: new runs store the PURE
agent return in ``runs.outputs_json[node_id]`` and the exhaustive runtime
telemetry in ``runs.node_telemetry_json[node_id]``. Rows created before the
flip still hold the OLD MIXED envelope in ``outputs_json`` with
``node_telemetry_json IS NULL``.

This tool backfills those legacy rows using the SAME splitter the P1 writer
uses (``modulo.core.node_output_split.split_node_output``), driven by the run
snapshot's frozen node-type map (``derive_node_type_map`` +
``extend_node_type_map_from_edges``, exactly as ``finalize`` builds it). A row
is split only when EVERY node splits losslessly; any node that would fall
through to the splitter's fail-open best-effort branch (unknown node type,
malformed envelope, missing snapshot graph) causes the WHOLE ROW to be skipped
and left legacy. The best-effort branch is NEVER used to replace stored data.

Idempotent: rows whose ``node_telemetry_json`` is already set are excluded by
the scan predicate AND by the conditional ``UPDATE``'s ``WHERE`` clause, so
concurrent writers are never clobbered. Batched by primary key.

Usage::

    DATABASE_URL=postgresql+asyncpg://... python backfill_node_telemetry.py
    DATABASE_URL=... python backfill_node_telemetry.py --apply
    DATABASE_URL=... python backfill_node_telemetry.py --apply --limit 500 \\
        --since 2026-01-01 --until 2026-08-01

Dry-run by default: prints counts + a sample of would-be rows. ``--apply``
issues the per-row conditional UPDATE. ANY skipped row makes the tool exit 1:
a nonzero skip during a quiesced backfill means something is wrong (an
unsplittable legacy shape that must be inspected by hand).

This is a version-controlled maintenance tool (see ``repair_accounts_fks.py``);
it uses a SYNC SQLAlchemy engine (psycopg) and is never invoked from the async
application path.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import create_engine, text

from modulo.core.cost_controller.finalize import derive_node_type_map
from modulo.core.node_output_split import (
    NODE_TYPE_GATE,
    _looks_like_gate,
    extend_node_type_map_from_edges,
    split_node_output,
)

_log = logging.getLogger("backfill_node_telemetry")

#: Node types that split_node_output resolves to a KNOWN splitter. Anything
#: outside this set (and not a recovery marker / gate-shaped envelope) falls
#: through to the helper's fail-open best-effort branch -- never lossless.
_LOSSLESS_NODE_TYPES = frozenset({"sandbox_agent", "agent", "connector", "manual", NODE_TYPE_GATE})

_UPDATE_SQL = text(
    "UPDATE runs SET outputs_json = :new, node_telemetry_json = :tel WHERE id = :rid AND node_telemetry_json IS NULL"
)


def _resolve_db_url(raw: str) -> str:
    """Convert a SQLAlchemy/async-style URL into a sync psycopg connection string."""
    url = raw
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
            break
    return url


def _can_split_losslessly(envelope: Any, node_type: str | None) -> bool:
    """True when ``split_node_output`` will dispatch to a KNOWN splitter.

    Mirrors the helper's dispatch order (node_output_split.split_node_output):
    malformed (non-dict) envelopes and envelopes with no resolvable node type
    fall through to its best-effort branch, which re-shapes the data without
    guaranteeing a faithful return/telemetry split -- such nodes must force the
    row to stay legacy. Gate envelopes are accepted both via a resolved
    ``gate`` node type and via shape detection.
    """
    if not isinstance(envelope, dict):
        return False
    if ("recovered" in envelope or "skipped" in envelope) and not isinstance(envelope.get("artifacts"), list):
        return True
    if (node_type or "") in _LOSSLESS_NODE_TYPES:
        return True
    return _looks_like_gate(envelope)


def _split_row(
    outputs_json: Any,
    node_type_map: dict[str, str] | None,
    run_id: str,
) -> tuple[bool, Any, dict[str, Any], str | None]:
    """Split a legacy row's ``outputs_json`` into lockstep column dicts.

    Returns ``(ok, new_outputs, new_telemetry, skip_reason)``. When ``ok`` is
    False the caller must SKIP the whole row and leave it legacy -- the
    best-effort branch is never used to replace stored data. ``new_outputs``
    keeps ``outputs_json`` verbatim for empty/NULL outputs (nothing to split),
    so the row is still marked split with an empty telemetry map.
    """
    if outputs_json is None or (isinstance(outputs_json, dict) and not outputs_json):
        return True, outputs_json, {}, None
    if not isinstance(outputs_json, dict):
        return False, {}, {}, f"outputs_json is {type(outputs_json).__name__}, expected dict"
    if node_type_map is None:
        return False, {}, {}, "snapshot graph_json unavailable (cannot derive node types)"
    new_outputs: dict[str, Any] = {}
    new_telemetry: dict[str, Any] = {}
    for node_id, envelope in outputs_json.items():
        node_type = node_type_map.get(node_id)
        if not _can_split_losslessly(envelope, node_type):
            return False, {}, {}, f"node {node_id!r}: no lossless split (node_type={node_type!r})"
        return_value, telemetry = split_node_output(envelope, node_type, None, run_id=run_id, node_id=node_id)
        if telemetry.get("skipped") is True:
            # Skipped recovery marker: telemetry is the SOLE record (mirrors the
            # P1 writer -- no outputs_json entry is created).
            new_telemetry[node_id] = telemetry
            continue
        new_outputs[node_id] = return_value
        new_telemetry[node_id] = telemetry
    return True, new_outputs, new_telemetry, None


def _scan_batch(
    conn: Any,
    *,
    after_id: Any,
    batch_size: int,
    since: datetime | None,
    until: datetime | None,
) -> list[Any]:
    """One keyset batch of legacy rows (``node_telemetry_json IS NULL``)."""
    stmt = "SELECT id, snapshot_id, outputs_json, node_telemetry_json FROM runs WHERE node_telemetry_json IS NULL"
    params: dict[str, Any] = {}
    if since is not None:
        stmt += " AND created_at >= :since"
        params["since"] = since
    if until is not None:
        stmt += " AND created_at <= :until"
        params["until"] = until
    if after_id is not None:
        stmt += " AND id > :after_id"
        params["after_id"] = after_id
    stmt += " ORDER BY id LIMIT :batch_size"
    params["batch_size"] = batch_size
    return conn.execute(text(stmt), params).fetchall()


def _node_type_map(conn: Any, cache: dict[Any, dict[str, str] | None], snapshot_id: Any) -> dict[str, str] | None:
    """Derive a run's frozen node-type map from its snapshot's ``graph_json``.

    ``None`` when the snapshot (or its ``graph_json``) is unavailable -- the
    row is treated as legacy-safe and kept as-is, mirroring how ``finalize``
    loads the map but failing closed instead of guessing node types.
    """
    if snapshot_id is None:
        return None
    if snapshot_id in cache:
        return cache[snapshot_id]
    graph_json = conn.execute(
        text("SELECT graph_json FROM pipeline_snapshots WHERE id = :sid"),
        {"sid": snapshot_id},
    ).scalar_one_or_none()
    result = (
        extend_node_type_map_from_edges(derive_node_type_map(graph_json), graph_json)
        if isinstance(graph_json, dict)
        else None
    )
    cache[snapshot_id] = result
    return result


def _run_backfill(
    conn: Any,
    *,
    apply: bool,
    batch_size: int,
    limit: int | None,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, Any]:
    """Walk legacy rows in PK batches, splitting each row's outputs.

    Returns a summary dict: ``mode``, ``batches``, ``rows_processed``,
    ``rows_split``, ``skips`` (list of ``(run_id, reason)``) and ``sample``
    (run ids a dry-run would split, up to 5).
    """
    cache: dict[Any, dict[str, str] | None] = {}
    after_id: Any = None
    batches = 0
    processed_total = 0
    rows_split = 0
    skips: list[tuple[str, str]] = []
    sample: list[str] = []

    while True:
        if limit is not None and processed_total >= limit:
            break
        batch_limit = batch_size if limit is None else min(batch_size, limit - processed_total)
        if batch_limit <= 0:
            break
        rows = _scan_batch(conn, after_id=after_id, batch_size=batch_limit, since=since, until=until)
        if not rows:
            break
        batches += 1
        for row in rows:
            if limit is not None and processed_total >= limit:
                break
            processed_total += 1
            run_id = str(row.id)
            needs_types = isinstance(row.outputs_json, dict) and bool(row.outputs_json)
            node_type_map = _node_type_map(conn, cache, row.snapshot_id) if needs_types else None
            ok, new_outputs, new_telemetry, reason = _split_row(row.outputs_json, node_type_map, run_id)
            if not ok:
                _log.warning(
                    "backfill_node_telemetry.skip",
                    extra={"run_id": run_id, "reason": reason or "unknown"},
                )
                skips.append((run_id, reason or "unknown"))
                continue
            rows_split += 1
            if apply:
                conn.execute(_UPDATE_SQL, {"new": new_outputs, "tel": new_telemetry, "rid": row.id})
            elif len(sample) < 5:
                sample.append(run_id)
        after_id = rows[-1].id
        if len(rows) < batch_limit:
            break

    return {
        "mode": "apply" if apply else "dry-run",
        "batches": batches,
        "rows_processed": processed_total,
        "rows_split": rows_split,
        "skips": skips,
        "sample": sample,
    }


def _print_summary(summary: dict[str, Any], out: TextIO) -> None:
    print(f"Backfill summary ({summary['mode']}):", file=out)
    print(f"  batches: {summary['batches']}", file=out)
    print(f"  rows processed: {summary['rows_processed']}", file=out)
    print(f"  rows split: {summary['rows_split']}", file=out)
    print(f"  rows skipped: {len(summary['skips'])}", file=out)
    for run_id, reason in summary["skips"]:
        print(f"    - {run_id}: {reason}", file=out)
    if summary["mode"] == "dry-run" and summary["sample"]:
        print("  sample of would-be split rows:", file=out)
        for run_id in summary["sample"]:
            print(f"    - {run_id}", file=out)


def _date_since(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time.min)


def _date_until(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time.max)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="backfill_node_telemetry.py",
        description="Backfill legacy runs rows into the outputs_json / node_telemetry_json split.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the split columns; without this the run is a dry-run that writes nothing.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows per primary-key batch (default: 500).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Bound the run to at most this many rows (bounded test run).",
    )
    parser.add_argument(
        "--since",
        type=_date_since,
        default=None,
        metavar="YYYY-MM-DD",
        help="Only rows with created_at >= this date.",
    )
    parser.add_argument(
        "--until",
        type=_date_until,
        default=None,
        metavar="YYYY-MM-DD",
        help="Only rows with created_at <= this date (end of day).",
    )
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)
    engine = create_engine(_resolve_db_url(raw))
    try:
        with engine.connect() as conn:
            summary = _run_backfill(
                conn,
                apply=args.apply,
                batch_size=args.batch_size,
                limit=args.limit,
                since=args.since,
                until=args.until,
            )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        engine.dispose()
    _print_summary(summary, sys.stdout)
    # Gate: a nonzero skip during a quiesced backfill means something is wrong.
    sys.exit(1 if summary["skips"] else 0)


if __name__ == "__main__":
    main()

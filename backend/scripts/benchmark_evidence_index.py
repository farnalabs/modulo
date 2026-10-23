"""Reproducible benchmark for the compliance evidence index (FAR-2055).

Seeds a synthetic large organisation on a REAL Postgres - either a throwaway
testcontainers instance (the default) or a database you pass with
``--database-url`` - then measures the evidence index's aggregate collectors
before and after the FAR-2055 indexes exist, and prints ``EXPLAIN ANALYZE`` for
the three queries the indexes target (wall-clock alone is a weak signal at
this scale; the plan is what proves a query went from sequential to
index-served).

The synthetic org includes undecided HITL gates as well as decided ones,
because the new HITL index is PARTIAL and its whole point is that undecided
gates never enter it - seeding only decided rows would hide that.

Why this exists: the index's cost claim ("every aggregate is index-served or
bounded") is a measurement, not an assertion. Run this and paste the output
into the PR.

Usage (from ``backend/``)::

    uv run python scripts/benchmark_evidence_index.py
    uv run python scripts/benchmark_evidence_index.py --scale 0.25 --repeats 3
    uv run python scripts/benchmark_evidence_index.py --database-url \
        postgresql+asyncpg://user:pass@host:5432/bench

Phases:

1. **before** - the new indexes are dropped, so the measurement is the schema
   as it was before FAR-2055 (pre-existing indexes 0155 / 0218 still apply).
2. **after** - the three new indexes are created exactly as migration 0325
   creates them.
3. **after, scoped** - the same indexed schema with a 30-day audit period, so
   the read is bounded by date as well as by index. This window keeps the HITL
   scan UNDER ``_HITL_SCAN_LIMIT`` (the full-history phase is over it), which
   exercises both sides of the truncation boundary.

The script is read-only against production data; it only ever writes to the
throwaway/test database it is pointed at.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.compliance.evidence_index import (
    _HITL_SCAN_LIMIT,
    _collect_audit_chain,
    _collect_autonomy_changes,
    _collect_eval_results,
    _collect_hitl_decisions,
    _collect_run_snapshots,
    build_evidence_index,
)
from modulo.compliance.pack import load_mapping_pack
from modulo.db.models import Base
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

#: The three indexes FAR-2055 adds. Their names match the models and 0326.
_NEW_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_hitl_claims_org_decision_at", "hitl_claims"),
    ("ix_runs_org_snapshot_id", "runs"),
    ("ix_eval_results_org_eval_id", "eval_results"),
)

_DROP_INDEX_SQL: tuple[str, ...] = tuple(f"DROP INDEX IF EXISTS {name}" for name, _table in _NEW_INDEXES)

_CREATE_INDEX_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_hitl_claims_org_decision_at "
    "ON hitl_claims (organisation_id, decision_at) WHERE decision IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_runs_org_snapshot_id ON runs (organisation_id, snapshot_id)",
    "CREATE INDEX IF NOT EXISTS ix_eval_results_org_eval_id ON eval_results (organisation_id, eval_id)",
)

_BASE_TIME = datetime(2025, 1, 1, tzinfo=UTC)
_DISTRIBUTION_DAYS = 365
_INSERT_CHUNK = 5000


class Scale:
    """Row counts for the synthetic org, scaled from the ticket's full size."""

    def __init__(self, factor: float) -> None:
        def rows(full: int) -> int:
            return max(1, round(full * factor))

        self.audit_events = rows(200_000)
        self.snapshots = rows(5_000)
        self.runs = rows(50_000)
        self.evals = rows(200)
        self.eval_results = rows(100_000)
        self.hitl_decisions = rows(15_000)
        # Undecided gates are the growing majority on a mature instance and the
        # reason the new HITL index is PARTIAL: they must never enter it.
        self.hitl_undecided = rows(50_000)


def _uuid(prefix: int, index: int) -> uuid.UUID:
    """Cheap deterministic id; the prefix keeps table ids disjoint."""
    return uuid.UUID(int=(prefix << 48) | index)


def _spread(index: int, total: int) -> datetime:
    """Distribute *index* of *total* evenly across the window."""
    span = _DISTRIBUTION_DAYS * 24 * 3600
    return _BASE_TIME + timedelta(seconds=(index * span) // max(total, 1))


async def _bulk_insert(
    session: AsyncSession,
    model: Any,
    rows: list[dict[str, Any]],
) -> None:
    # Core insert (not ORM add_all): one executemany per chunk, no per-row
    # identity-map cost on a 200k-row seed.
    for start in range(0, len(rows), _INSERT_CHUNK):
        await session.execute(insert(model), rows[start : start + _INSERT_CHUNK])
        await session.commit()


async def _seed(session: AsyncSession, scale: Scale) -> uuid.UUID:
    org_id = _uuid(1, 1)
    account_id = _uuid(2, 1)
    pipeline_id = _uuid(3, 1)
    session.add(Organisation(id=org_id, name="Benchmark Org", slug="benchmark-org"))
    session.add(Account(id=account_id, email="bench@evidence.example", display_name="Bench"))
    session.add(Pipeline(id=pipeline_id, organisation_id=org_id, name="benchmark pipeline", account_id=account_id))
    await session.commit()

    print(f"  seeding {scale.snapshots:,} pipeline snapshots ...", flush=True)
    snapshot_ids = [_uuid(10, index) for index in range(scale.snapshots)]
    await _bulk_insert(
        session,
        PipelineSnapshot,
        [
            {
                "id": snapshot_id,
                "organisation_id": org_id,
                "pipeline_id": pipeline_id,
                "snapshot_version": index + 1,
                "graph_json": {"nodes": []},
                "connector_bindings_json": [],
                "schema_pins_json": [],
                "prompt_pins_json": [],
                "model_backend_pins_json": [],
                "config_json": {},
                "run_context_defaults": {},
                "created_at": _BASE_TIME,
            }
            for index, snapshot_id in enumerate(snapshot_ids)
        ],
    )

    print(f"  seeding {scale.runs:,} runs ...", flush=True)
    run_ids = [_uuid(11, index) for index in range(scale.runs)]
    await _bulk_insert(
        session,
        Run,
        [
            {
                "id": run_id,
                "organisation_id": org_id,
                "pipeline_id": pipeline_id,
                "snapshot_id": snapshot_ids[index % len(snapshot_ids)],
                "trigger_type": "manual",
                "status": "complete",
                "run_number": index + 1,
                "input_hash": f"{index:064x}"[-64:],
                "langgraph_thread_id": f"bench-thread-{index}",
                "created_at": _spread(index, scale.runs),
            }
            for index, run_id in enumerate(run_ids)
        ],
    )

    print(f"  seeding {scale.evals:,} eval definitions ...", flush=True)
    eval_ids = [_uuid(12, index) for index in range(scale.evals)]
    await _bulk_insert(
        session,
        Eval,
        [
            {
                "id": eval_id,
                "organisation_id": org_id,
                "pipeline_id": pipeline_id,
                "account_id": account_id,
                "eval_type": "llm_judge",
                "config_json": {},
            }
            for eval_id in eval_ids
        ],
    )

    print(f"  seeding {scale.eval_results:,} eval results ...", flush=True)
    await _bulk_insert(
        session,
        EvalResult,
        [
            {
                "id": _uuid(13, index),
                "organisation_id": org_id,
                "eval_id": eval_ids[index % len(eval_ids)],
                "run_id": run_ids[index % len(run_ids)],
                "passed": index % 3 != 0,
                "observed": True,
                "evaluated_at": _spread(index, scale.eval_results),
            }
            for index in range(scale.eval_results)
        ],
    )

    print(f"  seeding {scale.hitl_decisions:,} decided HITL gates ...", flush=True)
    await _bulk_insert(
        session,
        HitlClaim,
        [
            {
                "id": _uuid(14, index),
                "organisation_id": org_id,
                "run_id": run_ids[index % len(run_ids)],
                "review_id": f"bench-review-{index}",
                "pipeline_id": pipeline_id,
                "expires_at": _BASE_TIME + timedelta(days=1),
                "decided_by": account_id,
                "decision": "approved" if index % 4 else "rejected",
                "decision_at": _spread(index, scale.hitl_decisions),
                "gate_config_json": {"human_only": index % 2 == 0},
            }
            for index in range(scale.hitl_decisions)
        ],
    )

    print(f"  seeding {scale.hitl_undecided:,} undecided HITL gates ...", flush=True)
    await _bulk_insert(
        session,
        HitlClaim,
        [
            {
                "id": _uuid(17, index),
                "organisation_id": org_id,
                "run_id": run_ids[index % len(run_ids)],
                "review_id": f"bench-pending-{index}",
                "pipeline_id": pipeline_id,
                "expires_at": _BASE_TIME + timedelta(days=1),
                "gate_config_json": {"human_only": True},
            }
            for index in range(scale.hitl_undecided)
        ],
    )

    print(f"  seeding {scale.audit_events:,} audit events ...", flush=True)
    audit_rows: list[dict[str, Any]] = []
    for index in range(scale.audit_events):
        event_type = "run.completed"
        if index == 1:
            event_type = "pipeline.autonomy_promotion_decided"
        elif index == 2:
            event_type = "pipeline.autonomy_demoted"
        audit_rows.append(
            {
                "id": _uuid(15, index),
                "organisation_id": org_id,
                "event_type": event_type,
                "payload_json": {},
                "created_at": _spread(index, scale.audit_events),
            }
        )
    await _bulk_insert(session, AuditEvent, audit_rows)

    last_event_id = _uuid(15, scale.audit_events - 1)
    session.add(
        AuditChainHead(
            id=_uuid(16, 1),
            organisation_id=org_id,
            last_event_hash="f" * 64,
            last_event_id=last_event_id,
            event_count=scale.audit_events,
        )
    )
    await session.commit()
    return org_id


async def _set_indexes(engine: Any, statements: tuple[str, ...]) -> None:
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))


async def _analyze(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "ANALYZE organisations, accounts, pipelines, pipeline_snapshots, runs, "
                "evals, eval_results, hitl_claims, audit_events, audit_chain_heads"
            )
        )


async def _present_indexes(engine: Any) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))
        return {str(row[0]) for row in result.all()}


#: The three aggregate queries the new indexes target, at their real shape.
_EXPLAIN_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "runs count(DISTINCT snapshot_id)",
        "SELECT count(DISTINCT r.snapshot_id) FROM runs r "
        "JOIN pipeline_snapshots s ON s.id = r.snapshot_id AND s.organisation_id = :org "
        "WHERE r.organisation_id = :org",
    ),
    (
        "eval_results count(DISTINCT eval_id)",
        "SELECT count(DISTINCT eval_id) FROM eval_results WHERE organisation_id = :org",
    ),
    (
        "hitl decided ordered scan",
        "SELECT id FROM hitl_claims WHERE organisation_id = :org AND decision IS NOT NULL "
        "ORDER BY decision_at DESC NULLS LAST LIMIT 10000",
    ),
)


async def _explain(engine: Any, org_id: uuid.UUID, title: str) -> None:
    """Print the planner's plan for the queries the new indexes target."""
    print(f"\n#### EXPLAIN ANALYZE - {title}")
    async with engine.connect() as conn:
        for label, sql in _EXPLAIN_QUERIES:
            rows = (
                await conn.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS) " + sql),
                    {"org": org_id},
                )
            ).all()
            print(f"- {label}:")
            for row in rows[:14]:
                print(f"    {row[0]}")


async def _timed(call: Callable[[], Awaitable[Any]], repeats: int) -> tuple[Any, float]:
    """Run *call*, returning its last result and the BEST wall-clock in ms."""
    best = float("inf")
    result: Any = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = await call()
        best = min(best, time.perf_counter() - started)
    return result, best * 1000.0


async def _measure_aggregates(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    repeats: int,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> list[tuple[str, int | None, float]]:
    pack = load_mapping_pack()
    window = {"period_from": period_from, "period_to": period_to}
    collectors: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...] = (
        ("audit_chain", lambda: _collect_audit_chain(session, org_id, pack, **window)),
        ("run_snapshots", lambda: _collect_run_snapshots(session, org_id, pack, **window)),
        ("hitl_decisions", lambda: _collect_hitl_decisions(session, org_id, pack, **window)),
        ("eval_results", lambda: _collect_eval_results(session, org_id, pack, **window)),
        ("autonomy_changes", lambda: _collect_autonomy_changes(session, org_id, pack, **window)),
    )
    results: list[tuple[str, int | None, float]] = []
    for label, collector in collectors:
        entry, milliseconds = await _timed(collector, repeats)
        results.append((label, entry.record_count, milliseconds))
    return results


def _print_table(title: str, rows: list[tuple[str, int | None, float]], full_build_ms: float) -> None:
    print(f"\n### {title}")
    print("| aggregate | row count | best wall-clock (ms) |")
    print("|---|---:|---:|")
    for label, count, milliseconds in rows:
        printed = "n/a" if count is None else f"{count:,}"
        print(f"| {label} | {printed} | {milliseconds:,.1f} |")
    print(f"| **full build** | | **{full_build_ms:,.1f}** |")


@asynccontextmanager
async def _database(database_url: str | None) -> AsyncIterator[str]:
    if database_url:
        yield database_url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as container:
        url = container.get_connection_url()
        if "psycopg2" in url:
            url = url.replace("psycopg2", "asyncpg")
        yield url


async def _run(args: argparse.Namespace) -> int:
    scale = Scale(args.scale)
    async with _database(args.database_url) as database_url:
        engine = create_async_engine(database_url)
        try:
            print("creating schema ...", flush=True)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)

            async with maker() as session:
                org_id = await _seed(session, scale)
            await _analyze(engine)

            present = await _present_indexes(engine)
            missing = {name for name, _table in _NEW_INDEXES if name not in present}
            if missing:
                print(f"warning: create_all did not create {sorted(missing)}", file=sys.stderr)

            # ---- BEFORE -------------------------------------------------
            await _set_indexes(engine, _DROP_INDEX_SQL)
            await _analyze(engine)
            async with maker() as session:
                before_rows = await _measure_aggregates(session, org_id, repeats=args.repeats)
                _, before_build = await _timed(lambda: build_evidence_index(session, org_id), args.repeats)
            _print_table("BEFORE - new indexes absent (pre-FAR-2055 schema)", before_rows, before_build)
            await _explain(engine, org_id, "BEFORE - new indexes absent")

            # ---- AFTER --------------------------------------------------
            await _set_indexes(engine, _CREATE_INDEX_SQL)
            await _analyze(engine)
            after_present = await _present_indexes(engine)
            if not all(name in after_present for name, _table in _NEW_INDEXES):
                print("error: new indexes were not created", file=sys.stderr)
                return 2
            async with maker() as session:
                after_rows = await _measure_aggregates(session, org_id, repeats=args.repeats)
                full_index, after_build = await _timed(lambda: build_evidence_index(session, org_id), args.repeats)
            _print_table("AFTER - FAR-2055 indexes present (full history)", after_rows, after_build)
            await _explain(engine, org_id, "AFTER - FAR-2055 indexes present")

            # ---- AFTER + SCOPED ----------------------------------------
            period_to = _BASE_TIME + timedelta(days=_DISTRIBUTION_DAYS)
            period_from = period_to - timedelta(days=30)
            async with maker() as session:
                scoped_rows = await _measure_aggregates(
                    session,
                    org_id,
                    repeats=args.repeats,
                    period_from=period_from,
                    period_to=period_to,
                )
                scoped_index, scoped_build = await _timed(
                    lambda: build_evidence_index(
                        session,
                        org_id,
                        period_from=period_from,
                        period_to=period_to,
                    ),
                    args.repeats,
                )
            _print_table(
                f"AFTER + SCOPED - 30-day audit period [{period_from.isoformat()}, {period_to.isoformat()})",
                scoped_rows,
                scoped_build,
            )

            hitl_full = next(entry for entry in full_index.artifacts if entry.artifact_type == "hitl_decision")
            hitl_scoped = next(entry for entry in scoped_index.artifacts if entry.artifact_type == "hitl_decision")
            print("\nHITL scan boundary:")
            print(
                f"- full history: {hitl_full.record_count:,} decided gates, "
                f"scan truncated={hitl_full.detail['human_only_scan_truncated']} "
                f"(limit {_HITL_SCAN_LIMIT:,})"
            )
            print(
                f"- 30-day window: {hitl_scoped.record_count:,} decided gates, "
                f"scan truncated={hitl_scoped.detail['human_only_scan_truncated']}"
            )
        finally:
            await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the compliance evidence index (FAR-2055).")
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Multiplier on the full synthetic org (default 1.0: 200k audit events, 50k runs).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Async SQLAlchemy URL for an existing empty database. Default: testcontainers Postgres 16.",
    )
    parser.add_argument("--repeats", type=int, default=3, help="Best-of-N runs per measurement (default 3).")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

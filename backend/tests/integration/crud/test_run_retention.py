"""Integration tests for the run-retention candidates listing (FAR-660).

Real-Postgres coverage of the SQL-side size estimation: the response shape,
the grouped per-status aggregation (whole-set vs terminal-only figures), the
page-level per-run estimates, the status/date filters, and org scoping. The
estimate path is deliberately NOT mocked — the unit suite stubs it, which is
exactly how the walk-based 503-on-prod regression shipped.

Payload design note (documented byte-metric asymmetry, qa FAR-660): the
page-level per-run estimates (``json_bytes`` = ``len(json.dumps(...))``) and
the whole-set SQL aggregates (``length(cast(jsonb AS text))``) measure the
SAME columns through DIFFERENT renderings. Python ``json.dumps``
ensure_ascii-escapes non-ASCII to ``\\uXXXX`` and renders numbers with Python
repr; Postgres' jsonb text rendering keeps raw unicode and re-renders numbers
as normalized numeric text. The renderings coincide only for simple ASCII
payloads, so equality assertions are kept to the ASCII render-equal subset;
``test_page_estimate_and_total_use_documented_different_byte_metrics`` seeds a
non-ASCII + exponent payload and pins the DOCUMENTED asymmetry (the page sum
may differ from the total for such rows — the totals are the authoritative
whole-set figure, per-run values are indicative).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.db.crud.run_retention import list_retention_candidates
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, META_NODE_ID, RunNodeOutput

pytestmark = pytest.mark.integration


def _seed_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    run_number: int,
    status: str,
    created_at: datetime,
    input_payload: dict[str, Any] | None = None,
    cost_breakdown: list[dict[str, Any]] | None = None,
) -> Run:
    run = Run(
        id=uuid.uuid4(),
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        trigger_type="manual",
        status=status,
        run_number=run_number,
        input_hash="a" * 64,
        langgraph_thread_id=f"{org_id}:{uuid.uuid4()}",
        created_at=created_at,
        input_payload=input_payload,
        cost_breakdown=cost_breakdown,
    )
    session.add(run)
    return run


async def _seed_checkpoints(session: AsyncSession, org_id: uuid.UUID, thread_id: str) -> None:
    """One checkpoints row + one checkpoint_blobs row for the thread (the same
    Postgres-only langgraph.* tables the aggregates join, created by the
    integration conftest from ModuloPostgresSaver's migration SQL)."""
    await session.execute(
        text(
            "INSERT INTO checkpoints (organisation_id, thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
            "VALUES (:oid, :tid, '', :cid, CAST(:cp AS jsonb), CAST(:md AS jsonb))"
        ),
        {
            "oid": str(org_id),
            "tid": thread_id,
            "cid": str(uuid.uuid4()),
            "cp": '{"state": "' + "x" * 200 + '"}',
            "md": "{}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO checkpoint_blobs (organisation_id, thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (:oid, :tid, '', 'ch', '1', 'json', :blob)"
        ),
        {"oid": str(org_id), "tid": thread_id, "blob": b"b" * 300},
    )


async def test_candidates_shape_and_estimates(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    bare = _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=1,
        status="complete",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    big = _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=2,
        status="complete",
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
        input_payload={"data": "y" * 1000},
        cost_breakdown=[{"amount": "1.00"}],
    )
    store = _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=3,
        status="pending",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
        input_payload={"k": "v"},
    )
    await rls_session.flush()

    await _seed_checkpoints(rls_session, test_org, store.langgraph_thread_id)
    rls_session.add(
        RunNodeOutput(
            organisation_id=test_org,
            run_id=store.id,
            node_id="node1",
            attempt_key=FINAL_ATTEMPT_KEY,
            outputs_json={"out": "z" * 500},
        )
    )
    # The metadata row carries the flags payload — it must be EXCLUDED from the
    # accounting by both the aggregate and the page reader.
    rls_session.add(
        RunNodeOutput(
            organisation_id=test_org,
            run_id=store.id,
            node_id=META_NODE_ID,
            attempt_key=FINAL_ATTEMPT_KEY,
            outputs_json={"empty_outputs": True, "empty_telemetry": False},
        )
    )
    await rls_session.flush()

    result = await list_retention_candidates(rls_session, org_id=test_org)

    assert set(result) == {
        "runs",
        "total_count",
        "total_estimated_bytes",
        "terminal_total",
        "terminal_estimated_bytes",
        "estimate_degraded",
    }
    assert result["estimate_degraded"] is False
    assert result["total_count"] == 3
    assert result["terminal_total"] == 2
    assert len(result["runs"]) == 3
    for item in result["runs"]:
        assert set(item) == {"id", "created_at", "status", "pipeline_id", "thread_id", "estimated_bytes"}

    est = {item["id"]: item["estimated_bytes"] for item in result["runs"]}
    est_bare = est[str(bare.id)]
    est_big = est[str(big.id)]
    est_store = est[str(store.id)]
    assert est_bare == 0  # no payloads, no node outputs, no checkpoints
    assert est_big > 0
    assert est_store > est_big  # payloads + node outputs + checkpoint rows

    # The grouped SQL totals measure exactly the same components as the
    # page-level per-run estimates for this ASCII render-equal subset (see
    # the module docstring) — a real semantic check of the aggregate SQL.
    assert result["total_estimated_bytes"] == est_bare + est_big + est_store
    assert result["terminal_estimated_bytes"] == est_bare + est_big
    assert result["terminal_estimated_bytes"] < result["total_estimated_bytes"]


async def test_page_estimate_and_total_use_documented_different_byte_metrics(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """qa Major (FAR-660): the page-level Python estimate (``json.dumps``,
    ensure_ascii-escaped) and the whole-set SQL aggregate (jsonb text
    rendering) are DIFFERENT documented metrics — they agree only for simple
    ASCII payloads. A non-ASCII payload (``é`` escapes to ``\\u00e9`` in
    ``json.dumps`` but renders raw in jsonb text) and an exponent-format
    number (Python repr ``1e-05`` vs normalized numeric text) make the page
    sum differ from the total. Neither metric is "wrong": the totals are the
    authoritative whole-set figure and the per-run values are indicative —
    this pins the documented asymmetry instead of pretending parity (the
    equality assertions stay on the ASCII render-equal subset above)."""
    ascii_run = _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=1,
        status="complete",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        input_payload={"data": "a" * 100},
    )
    unicode_run = _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=2,
        status="complete",
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
        input_payload={"accent": "é" * 10, "tiny": 1e-05},
    )
    await rls_session.flush()

    result = await list_retention_candidates(rls_session, org_id=test_org)

    assert result["estimate_degraded"] is False
    assert result["total_count"] == 2
    est = {item["id"]: item["estimated_bytes"] for item in result["runs"]}
    page_sum = est[str(ascii_run.id)] + est[str(unicode_run.id)]
    # The é escaping inflates the page (Python) metric by 5 characters per
    # accent character while the exponent number shifts the SQL metric by at
    # most a few characters — the page sum is strictly larger, and the two
    # figures are NOT equal for these rows (documented asymmetry).
    assert est[str(ascii_run.id)] > 0
    assert est[str(unicode_run.id)] > 0
    assert result["total_estimated_bytes"] > 0
    assert result["total_estimated_bytes"] != page_sum
    assert page_sum > result["total_estimated_bytes"]


async def test_candidates_status_filter_narrows_counts_and_estimates(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=1,
        status="complete",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    store = _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=2,
        status="pending",
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
        input_payload={"k": "v"},
    )
    await rls_session.flush()
    await _seed_checkpoints(rls_session, test_org, store.langgraph_thread_id)

    result = await list_retention_candidates(rls_session, org_id=test_org, status="pending")

    assert result["total_count"] == 1
    assert result["terminal_total"] == 0
    assert result["terminal_estimated_bytes"] == 0
    est = {item["id"]: item["estimated_bytes"] for item in result["runs"]}
    assert result["total_estimated_bytes"] == est[str(store.id)]
    assert result["total_estimated_bytes"] > 0  # the checkpoint bytes counted


async def test_candidates_date_filter_applies_to_the_aggregates(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=1,
        status="complete",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        input_payload={"old": "o" * 100},
    )
    new = _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=2,
        status="complete",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        input_payload={"new": "n" * 50},
    )
    await rls_session.flush()

    result = await list_retention_candidates(rls_session, org_id=test_org, date_from=datetime(2026, 3, 1, tzinfo=UTC))

    assert result["total_count"] == 1
    est = {item["id"]: item["estimated_bytes"] for item in result["runs"]}
    assert result["total_estimated_bytes"] == est[str(new.id)]
    assert result["terminal_estimated_bytes"] == result["total_estimated_bytes"]


async def _seed_foreign_run(db_engine: AsyncEngine) -> None:
    """A terminal run in a DIFFERENT org (seeded over the superuser engine —
    FORCE RLS does not bind superusers, mirroring how the fixture chain seeds
    the shared session org)."""
    org_b = uuid.uuid4()
    account_b = uuid.uuid4()
    pipeline_b = uuid.uuid4()
    snapshot_b = uuid.uuid4()
    run_b = uuid.uuid4()
    slug = f"ret-{org_b.hex[:8]}"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_b), "name": "Retention Org B", "slug": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(account_b), "email": f"ret-b-{slug}@test.local", "name": "Retention B"},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(pipeline_b), "oid": str(org_b), "name": "Retention B Pipeline", "aid": str(account_b)},
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, graph_json, "
                "connector_bindings_json, schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_b), "pid": str(pipeline_b), "oid": str(org_b)},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'complete', 1, :ih, :tid)"
            ),
            {
                "id": str(run_b),
                "oid": str(org_b),
                "pid": str(pipeline_b),
                "sid": str(snapshot_b),
                "ih": "b" * 64,
                "tid": f"{org_b}:{run_b}",
            },
        )


async def test_candidates_org_scoping_excludes_other_orgs(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    _seed_run(
        rls_session,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        run_number=1,
        status="complete",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        input_payload={"mine": "m" * 100},
    )
    await rls_session.flush()
    await _seed_foreign_run(db_engine)

    result = await list_retention_candidates(rls_session, org_id=test_org)

    # Only test_org's run is listed — the foreign org's terminal run is never
    # counted, neither in the page nor in the grouped aggregates.
    assert result["total_count"] == 1
    assert result["terminal_total"] == 1
    assert len(result["runs"]) == 1

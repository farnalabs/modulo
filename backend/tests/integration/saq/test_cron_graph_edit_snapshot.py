"""Cron fires must freeze the CURRENT live graph — FAR-1519 item 3, settled on the real paths.

Observation (self-hosted bring-up, FAR-1519 item 3): after a pipeline's graph
was edited, every subsequent cron-triggered run still carried the FIRST
snapshot, while a pipeline graphed *before* its first run snapshotted
correctly. The observing deployment also saw a plan-gated snapshot-save
endpoint (``402 pipeline_diff_rollback``), offered as a competing explanation.

Two hypotheses:

* **H1 — expected:** the edit produced no new snapshot (the save endpoint is
  plan-gated), so the pipeline correctly points at its one snapshot and every
  run uses it.
* **H2 — defect:** the cron dispatch path resolves a different snapshot source
  than the manual path (latest EXISTING snapshot vs the live graph at run
  start), so a run pins a stale snapshot after an edit.

This module decides between them end to end, through the real HTTP routes
(``PATCH /api/v1/pipelines/{id}/graph``, ``POST /api/v1/runs``) and the real
scheduler dispatch (``fire_due_triggers`` -> ``fire_cron_trigger``) against a
real Postgres + Redis (testcontainers):

1. The graph edit creates NO snapshot — **even with every plan feature
   enabled** (the ASGI client reports enterprise tier), so H1's plan-gate
   story cannot be the cause: nothing in the edit path attempts a save.
2. Pre-fix, the second cron fire reuses run A's pre-edit snapshot while
   ``POST /runs`` at the same moment creates a fresh snapshot from the edited
   live graph — the divergence H2 predicts. Post-fix, the second cron fire
   freezes the edited live graph, matching the manual path and the documented
   run lifecycle ("the pipeline's current definition is frozen as a
   PipelineSnapshot" at trigger time — docs/architecture.md).
3. A cron trigger whose config pins ``snapshot_id`` keeps executing that exact
   snapshot after the edit — pin semantics are unchanged by the fix.
4. A fire that skips because the org was paused mid-fire leaves NO snapshot
   behind (FAR-1536): the auto-create runs before ``create_run``'s authority
   gate and every return commits the fire transaction, so the race-backstop
   skip used to persist an unreferenced snapshot.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.auth.jwt import create_access_token
from modulo.core import cron_helpers as ch

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

_VALID_32 = "a" * 32
_NODE_ID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_headers(org_id: uuid.UUID, account_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(
        subject=f"user-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role="admin",
        is_system_admin=False,
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


def _graph_payload(label: str, output_schema_id: uuid.UUID) -> dict[str, Any]:
    """A single flat ``manual`` node (no agent) carrying the version marker."""
    return {
        "nodes": [
            {
                "id": _NODE_ID,
                "node_type": "manual",
                "position": {"x": 0, "y": 0},
                "label": label,
                "output_schema_id": str(output_schema_id),
            }
        ],
        "edges": [],
    }


async def _seed_schema(db_engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    """A committed Schema + version row the manual node's output can point at."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from modulo.db.crud.schema import create_schema, create_schema_version

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        schema = await create_schema(
            session,
            org_id=org_id,
            name=f"CronSnapshotSchema-{uuid.uuid4().hex[:6]}",
            account_id=user_id,
        )
        await create_schema_version(
            session,
            org_id=org_id,
            schema_id=schema.id,
            version="1.0",
            version_number=1,
            definition_json={"type": "object"},
            account_id=user_id,
        )
        return schema.id


async def _seed_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    label: str = "v1",
    output_schema_id: uuid.UUID,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, "
                "visibility, owner_team_id) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, (:graph)::json, 'manual_approval', 'org', NULL)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": f"cron-snapshot-{pipeline_id.hex[:8]}",
                "uid": str(user_id),
                "graph": json.dumps(_graph_payload(label, output_schema_id)["nodes"]),
            },
        )
    return pipeline_id


async def _seed_due_cron_trigger(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    config_json: dict[str, Any] | None = None,
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, account_id, trigger_type, active, "
                "max_concurrent_runs, config_json, cron_expression, next_fire_at) "
                "VALUES (:id, :oid, :pid, :uid, 'cron', true, 5, (:cfg)::json, :cron, now() - interval '1 second')"
            ),
            {
                "id": str(trigger_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "uid": str(user_id),
                "cfg": json.dumps(config_json or {}),
                # Yearly cadence: after the first atomic advance the row is a
                # year out, so a straggler tick can never legitimately re-fire
                # it while this test drives fires explicitly.
                "cron": "0 0 1 1 *",
            },
        )
    return trigger_id


async def _make_due(db_engine: AsyncEngine, trigger_id: uuid.UUID) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE triggers SET next_fire_at = now() - interval '1 second' WHERE id = :tid"),
            {"tid": str(trigger_id)},
        )


async def _set_trigger_config(
    db_engine: AsyncEngine,
    trigger_id: uuid.UUID,
    config_json: dict[str, Any],
) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE triggers SET config_json = (:cfg)::json WHERE id = :tid"),
            {"cfg": json.dumps(config_json), "tid": str(trigger_id)},
        )


async def _tick_and_capture_fire(monkeypatch: pytest.MonkeyPatch, trigger_id: uuid.UUID) -> dict[str, Any]:
    """Run one real scheduler tick and capture THIS trigger's fire-job kwargs.

    The spy wraps the real ``_enqueue_fire_job_async`` (still invoked), so the
    captured kwargs are exactly what the SAQ worker would receive — the only
    thing shortened is the queue hop itself.
    """
    captured: dict[str, Any] = {}
    original = ch._enqueue_fire_job_async

    async def _spy(q: Any, function: str, key: str, **kwargs: Any) -> str | None:
        if key.startswith(f"fire:{trigger_id}:"):
            captured.clear()
            captured.update(kwargs)
            captured["_key"] = key
            captured["_function"] = function
        return await original(q, function, key, **kwargs)

    monkeypatch.setattr(ch, "_enqueue_fire_job_async", _spy)
    await ch.fire_due_triggers()
    assert captured, f"scheduler tick enqueued no fire job for cron trigger {trigger_id}"
    assert captured["_function"] == "modulo.core.saq_worker.fire_cron_trigger"
    return captured


async def _execute_cron_fire(captured: dict[str, Any]) -> dict[str, Any]:
    """Execute the captured fire exactly as ``saq_worker.fire_cron_trigger`` does.

    The SAQ wrapper only re-parses these kwargs and then dispatches the created
    run to the executor; dispatch is irrelevant to snapshot resolution, and no
    worker is running here (runs stay ``pending``), so the delegation is the
    real fire path minus an idle queue hop.
    """
    return await ch.fire_cron_trigger(
        trigger_id=uuid.UUID(captured["trigger_id"]),
        org_id=uuid.UUID(captured["org_id"]),
        pipeline_id=uuid.UUID(captured["pipeline_id"]),
        cron_expression=captured["cron_expression"],
        snapshot_id=uuid.UUID(captured["snapshot_id"]) if captured["snapshot_id"] else None,
    )


async def _snapshot_count(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> int:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        return int(result.scalar_one())


async def _snapshot_node_label(db_engine: AsyncEngine, snapshot_id: uuid.UUID) -> str | None:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT graph_json FROM pipeline_snapshots WHERE id = :sid"),
            {"sid": str(snapshot_id)},
        )
        row = result.scalar_one_or_none()
    if row is None:
        return None
    graph = row if isinstance(row, dict) else json.loads(str(row))
    nodes = graph.get("nodes") or []
    return str(nodes[0].get("label")) if nodes else None


async def _run_snapshot_id(db_engine: AsyncEngine, run_id: uuid.UUID) -> uuid.UUID | None:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT snapshot_id FROM runs WHERE id = :rid"),
            {"rid": str(run_id)},
        )
        raw = result.scalar_one_or_none()
    return uuid.UUID(str(raw)) if raw is not None else None


async def _live_node_label(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> str | None:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT graph_nodes_json FROM pipelines WHERE id = :pid"),
            {"pid": str(pipeline_id)},
        )
        row = result.scalar_one_or_none()
    if row is None:
        return None
    nodes = row if isinstance(row, list) else json.loads(str(row))
    return str(nodes[0].get("label")) if nodes else None


async def _run_count(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> int:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM runs WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        return int(result.scalar_one())


async def _snapshot_exists(db_engine: AsyncEngine, snapshot_id: uuid.UUID) -> bool:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM pipeline_snapshots WHERE id = :sid"),
            {"sid": str(snapshot_id)},
        )
        return int(result.scalar_one()) > 0


async def _set_org_paused(db_engine: AsyncEngine, org_id: uuid.UUID, paused: bool) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE organisations SET triggers_paused = :paused, "
                "triggers_paused_at = CASE WHEN :paused THEN now() ELSE NULL END "
                "WHERE id = :oid"
            ),
            {"paused": paused, "oid": str(org_id)},
        )


async def _cleanup(
    db_engine: AsyncEngine,
    pipeline_id: uuid.UUID,
    trigger_id: uuid.UUID,
    schema_id: uuid.UUID,
) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "DELETE FROM trigger_events "
                "WHERE trigger_id = :tid OR run_id IN (SELECT id FROM runs WHERE pipeline_id = :pid)"
            ),
            {"tid": str(trigger_id), "pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM runs WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(
            text(
                "DELETE FROM snapshot_schema_pins WHERE snapshot_id IN "
                "(SELECT id FROM pipeline_snapshots WHERE pipeline_id = :pid)"
            ),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(text("DELETE FROM triggers WHERE id = :tid"), {"tid": str(trigger_id)})
        await conn.execute(text("DELETE FROM pipelines WHERE id = :pid"), {"pid": str(pipeline_id)})
        await conn.execute(text("DELETE FROM schema_versions WHERE schema_id = :sid"), {"sid": str(schema_id)})
        await conn.execute(text("DELETE FROM schemas WHERE id = :sid"), {"sid": str(schema_id)})


async def _patch_graph(
    client: AsyncClient,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    label: str,
    output_schema_id: uuid.UUID,
) -> None:
    resp = await client.patch(
        f"/api/v1/pipelines/{pipeline_id}/graph",
        json=_graph_payload(label, output_schema_id),
        headers=_auth_headers(org_id, account_id),
    )
    assert resp.status_code == 200, f"PATCH /graph ({label}) failed: {resp.status_code} {resp.text}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_fire_after_graph_edit_executes_the_edited_graph(
    saq_settings_env: str,
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cron fire after a graph edit must freeze the EDITED live graph.

    Pre-fix this fails: the second fire reuses the first run's snapshot (the
    pre-edit graph) while ``POST /runs`` at the same moment executes the
    edited graph — the FAR-1519 item 3 observation, on the real paths.
    """
    schema_id = await _seed_schema(db_engine, test_org, test_user)
    pipeline_id = await _seed_pipeline(db_engine, test_org, test_user, label="v1", output_schema_id=schema_id)
    trigger_id = await _seed_due_cron_trigger(db_engine, test_org, test_user, pipeline_id)
    try:
        # --- First fire: no snapshot exists yet, so it freezes live graph v1.
        first_kwargs = await _tick_and_capture_fire(monkeypatch, trigger_id)
        first = await _execute_cron_fire(first_kwargs)
        assert first["status"] == "fired", first
        run_a_id = uuid.UUID(first["run_id"])
        snap_a = await _run_snapshot_id(db_engine, run_a_id)
        assert snap_a is not None
        assert await _snapshot_node_label(db_engine, snap_a) == "v1"

        # --- Edit the graph through the REAL route (enterprise plan context:
        # every feature, including pipeline_diff_rollback, is enabled here).
        await _patch_graph(integration_client, test_org, test_user, pipeline_id, "v2", schema_id)
        assert await _live_node_label(db_engine, pipeline_id) == "v2"

        # H1 observation: the edit itself creates NO snapshot — with the plan
        # feature enabled, so the 402 plan-gate story cannot be the cause.
        assert await _snapshot_count(db_engine, pipeline_id) == 1, (
            "graph edit unexpectedly created a snapshot — the H1 (plan-gated save) premise needs re-examination"
        )

        # --- Second fire: the cron run must execute the EDITED graph.
        await _make_due(db_engine, trigger_id)
        second_kwargs = await _tick_and_capture_fire(monkeypatch, trigger_id)
        second = await _execute_cron_fire(second_kwargs)
        assert second["status"] == "fired", second
        run_b_id = uuid.UUID(second["run_id"])
        snap_b = await _run_snapshot_id(db_engine, run_b_id)
        assert snap_b is not None
        assert snap_b != snap_a, (
            "stale-snapshot defect (FAR-1519 item 3): the post-edit cron run "
            f"reused the pre-edit snapshot {snap_a} instead of freezing the "
            "current live graph"
        )
        assert await _snapshot_node_label(db_engine, snap_b) == "v2", (
            "post-edit cron run's snapshot does not carry the edited graph"
        )

        # --- Manual path at the same moment: parity is the defect's frame.
        manual = await integration_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(pipeline_id)},
            headers=_auth_headers(test_org, test_user),
        )
        assert manual.status_code == 202, manual.text
        manual_snap = await _run_snapshot_id(db_engine, uuid.UUID(manual.json()["run_id"]))
        assert manual_snap is not None
        assert await _snapshot_node_label(db_engine, manual_snap) == "v2"
        # The cron and manual paths must agree about what runs execute.
        assert await _snapshot_node_label(db_engine, snap_b) == await _snapshot_node_label(db_engine, manual_snap), (
            "cron and manual runs at the same moment disagree about the executed graph"
        )
    finally:
        await _cleanup(db_engine, pipeline_id, trigger_id, schema_id)


@pytest.mark.asyncio
async def test_pinned_cron_trigger_keeps_executing_its_pinned_snapshot(
    saq_settings_env: str,
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trigger whose config pins ``snapshot_id`` still runs that snapshot.

    Guard for the fix: freezing the live graph on unpinned fires must never
    override an explicit pin in ``trigger.config_json``.
    """
    schema_id = await _seed_schema(db_engine, test_org, test_user)
    pipeline_id = await _seed_pipeline(db_engine, test_org, test_user, label="v1", output_schema_id=schema_id)
    trigger_id = await _seed_due_cron_trigger(db_engine, test_org, test_user, pipeline_id)
    try:
        first_kwargs = await _tick_and_capture_fire(monkeypatch, trigger_id)
        first = await _execute_cron_fire(first_kwargs)
        assert first["status"] == "fired", first
        pinned_snap = await _run_snapshot_id(db_engine, uuid.UUID(first["run_id"]))
        assert pinned_snap is not None
        assert await _snapshot_node_label(db_engine, pinned_snap) == "v1"

        # Pin that snapshot in the trigger config, then edit the graph.
        await _set_trigger_config(db_engine, trigger_id, {"snapshot_id": str(pinned_snap)})
        await _patch_graph(integration_client, test_org, test_user, pipeline_id, "v2", schema_id)

        await _make_due(db_engine, trigger_id)
        second_kwargs = await _tick_and_capture_fire(monkeypatch, trigger_id)
        assert second_kwargs["snapshot_id"] == str(pinned_snap)
        second = await _execute_cron_fire(second_kwargs)
        assert second["status"] == "fired", second
        run_snap = await _run_snapshot_id(db_engine, uuid.UUID(second["run_id"]))
        assert run_snap == pinned_snap, "pinned snapshot must win over the edited live graph"
        assert await _snapshot_node_label(db_engine, run_snap) == "v1"
    finally:
        await _cleanup(db_engine, pipeline_id, trigger_id, schema_id)


@pytest.mark.asyncio
async def test_paused_race_fire_skips_without_persisting_a_snapshot(
    saq_settings_env: str,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fire that loses the pause race persists NO snapshot (FAR-1536 M1).

    ``fire_cron_trigger`` auto-creates the snapshot BEFORE ``create_run``'s
    org-pause authority gate, and every return out of the fire job (including
    skips) COMMITS the enclosing transaction — so the ``TriggersPausedError``
    race-backstop skip used to persist an unreferenced snapshot: a row no run
    points at, written by a fire that did nothing.

    The race is driven deterministically rather than by scheduling: the org is
    paused in the database, while the fire job's own early race-backstop read
    (``ch.org_is_paused``) is pinned to "not paused" — exactly the state that
    read would have observed had the pause landed a moment after it. The
    authority gate in ``create_run`` then reads the real, paused row and raises.

    What this proves: the auto-create ran, the fire reported skipped, and the
    snapshot it produced is NOT in the table once the fire returns — i.e. the
    outer transaction committed without it. What it does NOT prove: that the
    interleaving itself can be observed end-to-end in a single-threaded test
    (the window is a genuine concurrent race); here the two reads are forced
    to disagree instead.
    """
    schema_id = await _seed_schema(db_engine, test_org, test_user)
    pipeline_id = await _seed_pipeline(db_engine, test_org, test_user, label="v1", output_schema_id=schema_id)
    trigger_id = await _seed_due_cron_trigger(db_engine, test_org, test_user, pipeline_id)
    try:
        # Enqueue while the org is still unpaused — fire_due_triggers skips
        # paused orgs outright, so the tick has to happen before the pause.
        captured_kwargs = await _tick_and_capture_fire(monkeypatch, trigger_id)
        assert not captured_kwargs["snapshot_id"], "an unpinned fire must not carry a pre-resolved snapshot"

        # Enter the race window: pause, then pin the fire job's early read to
        # its pre-pause value.
        await _set_org_paused(db_engine, test_org, True)
        monkeypatch.setattr(ch, "org_is_paused", AsyncMock(return_value=False))

        # Record the snapshot the auto-create produces so the proof below is
        # not vacuous (it fails if the auto-create never ran).
        auto_created: list[uuid.UUID] = []
        real_auto_create = ch._auto_create_snapshot

        async def _spy_auto_create(*args: Any, **kwargs: Any) -> uuid.UUID | None:
            snapshot_id = await real_auto_create(*args, **kwargs)
            if snapshot_id is not None:
                auto_created.append(snapshot_id)
            return snapshot_id

        monkeypatch.setattr(ch, "_auto_create_snapshot", _spy_auto_create)

        snapshots_before = await _snapshot_count(db_engine, pipeline_id)
        runs_before = await _run_count(db_engine, pipeline_id)

        outcome = await _execute_cron_fire(captured_kwargs)

        # Paused-org semantics are unchanged: a skipped fire, never a failed one.
        assert outcome == {"status": "skipped", "reason": ch.PAUSE_SKIP_REASON}, outcome
        assert auto_created, "the fire never reached the snapshot auto-create — the test proves nothing"
        assert await _snapshot_count(db_engine, pipeline_id) == snapshots_before, (
            "the paused-race skip committed an unreferenced snapshot "
            f"(auto-created {auto_created[0] if auto_created else None})"
        )
        assert not await _snapshot_exists(db_engine, auto_created[0]), (
            f"auto-created snapshot {auto_created[0]} survived the pause-race skip"
        )
        assert await _run_count(db_engine, pipeline_id) == runs_before, "a paused fire must not create a run"
    finally:
        await _set_org_paused(db_engine, test_org, False)
        await _cleanup(db_engine, pipeline_id, trigger_id, schema_id)

"""Integration test: pending-HITL ``required_team_name`` survives soft-deleted teams (FAR-1653).

The viewmodel's pending-HITL query outer-joins ``teams`` to label each gate
with its required team's name. The global soft-delete filter injects
``deleted_at IS NULL`` into that join's ON clause, so a team soft-deleted
after the gate fired rendered a BLANK ``required_team_name`` — the gate row
itself was never dropped, only the label. The query now opts out via
``include_soft_deleted`` so historical reads keep resolving the name.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.auth.jwt import create_access_token
from modulo.db.soft_delete import register_soft_delete_filter

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": email, "name": email.split("@", maxsplit=1)[0]},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _seed_soft_deleted_team(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    team_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO teams (id, organisation_id, name, account_id, notification_endpoints, settings, "
                "deleted_at) VALUES (:id, :oid, :name, :aid, '[]'::json, '{}'::json, now())"
            ),
            {"id": str(team_id), "oid": str(org_id), "name": name, "aid": str(account_id)},
        )
        # Membership survives the team's soft delete (team_memberships is not
        # soft-deletable) — the gate stays visible to the team's members.
        await conn.execute(
            text(
                "INSERT INTO team_memberships (id, organisation_id, team_id, account_id, role) "
                "VALUES (:id, :oid, :tid, :aid, 'viewer')"
            ),
            {"id": str(uuid.uuid4()), "oid": str(org_id), "tid": str(team_id), "aid": str(account_id)},
        )
    return team_id


async def _seed_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID, name: str) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, description, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :desc, :aid, 5, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": name, "desc": name, "aid": str(account_id)},
        )
    return pipeline_id


async def _seed_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


async def _seed_awaiting_human_run(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "input_hash, langgraph_thread_id, run_number, created_at) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'awaiting_human', :ih, :lt, 1, :ca)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": uuid.uuid4().hex,
                "lt": f"thread-{run_id.hex[:16]}",
                "ca": now,
            },
        )
    return run_id


async def _seed_hitl_claim(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    required_team_id: uuid.UUID,
) -> uuid.UUID:
    claim_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO hitl_claims (id, organisation_id, run_id, required_team_id, review_id, "
                "pipeline_id, expires_at) VALUES (:id, :oid, :rid, :tid, 'gate-1', :pid, :exp)"
            ),
            {
                "id": str(claim_id),
                "oid": str(org_id),
                "rid": str(run_id),
                "tid": str(required_team_id),
                "pid": str(pipeline_id),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
        )
    return claim_id


async def test_pending_hitl_required_team_name_survives_soft_deleted_team(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """A soft-deleted required team's NAME must still render on the viewmodel (FAR-1653)."""
    # Idempotent — mirrors production (``db/session.py``) so the SELECT-only
    # soft-delete filter is active exactly as in a live deployment.
    register_soft_delete_filter()

    org_id = await _seed_org(db_engine, "VmSoftDelTeam")
    user_id = await _seed_user(db_engine, org_id, f"vm-{org_id.hex[:8]}@test.local")
    team_id = await _seed_soft_deleted_team(db_engine, org_id, user_id, "Far1653 Deleted Gate Team")
    pipeline_id = await _seed_pipeline(db_engine, org_id, user_id, "VmSoftDelTeam-Pipeline")
    snapshot_id = await _seed_snapshot(db_engine, org_id, pipeline_id)
    run_id = await _seed_awaiting_human_run(db_engine, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=snapshot_id)
    claim_id = await _seed_hitl_claim(
        db_engine,
        org_id=org_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        required_team_id=team_id,
    )

    token = create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        is_system_admin=False,
        client_kind="browser",
    )
    resp = await integration_client.get(
        "/api/v1/viewmodel/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    pending = resp.json()["pending_hitl_reviews"]
    matching = [p for p in pending if p["id"] == str(claim_id)]
    assert len(matching) == 1, f"the pending gate must surface for the team member, got {pending!r}"
    assert matching[0]["required_team_id"] == str(team_id)
    assert matching[0]["required_team_name"] == "Far1653 Deleted Gate Team", (
        "a soft-deleted required team's NAME must still render on the pending-HITL "
        f"list, got {matching[0]['required_team_name']!r}"
    )

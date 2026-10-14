"""FAR-1593: a pipeline created AFTER a guardrail config apply inherits the org default.

Real Postgres (testcontainers) + real migrations + the real FastAPI routes.
This module is the observation, not an inference from source: it applies an
org-level guardrail config set while one pipeline exists, creates a NEW
pipeline afterwards, and reads ``evals`` with raw SQL plus
``GET /api/v1/guardrails/config/drift``.

Before FAR-1593 the empirical result was: the new pipeline carried ZERO
org-level rows (both via ``POST /api/v1/pipelines`` and a raw
``INSERT INTO pipelines``), the enforcement seam
(``load_pipeline_guardrail_rows``) returned nothing for it — so its runs were
unguarded — and ``/drift`` still read ``clean``, because the drift hash
deduplicates org-level rows BY NAME across pipelines and one covered pipeline
masks an uncovered one. Decision recorded on FAR-1593: **freshness gap**, fixed
by materialising the applied snapshot at the pipeline-creation chokepoint
(``core/guardrails/materialise.py``), which this test now pins.

The org-level rows live on the ``evals`` table since the chunk-3b cutover
(``eval_definitions`` is the frozen pre-cutover snapshot — see the ``0254``
cutover comments in ``api/routes/evals.py``), so the queries below target
``evals`` with ``eval_type = 'guardrail'``, ``node_id IS NULL``,
``deleted_at IS NULL``.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.guardrail_config import load_pipeline_guardrail_rows
from tests.integration.test_guardrail_config_api import (
    _CONFIG_YAML,
    _apply,
    _auth_headers,
    _propose,
    _seed_org,
    _seed_pipeline,
    _seed_user,
)

pytestmark = pytest.mark.integration


async def _org_level_rows_for_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> list[str]:
    """Raw observation: live org-level guardrail row names bound to *pipeline_id*."""
    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT name FROM evals "
                "WHERE organisation_id = :oid AND pipeline_id = :pid "
                "AND eval_type = 'guardrail' AND node_id IS NULL AND deleted_at IS NULL "
                "ORDER BY name",
            ),
            {"oid": str(org_id), "pid": str(pipeline_id)},
        )
        return [str(r[0]) for r in rows.all()]


async def _create_pipeline_via_api(
    client: AsyncClient,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/pipelines",
        json={"name": name},
        headers=_auth_headers(org_id, user_id),
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _drift(client: AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    resp = await client.get(
        "/api/v1/guardrails/config/drift",
        headers=_auth_headers(org_id, user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _enforcement_rows(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> int:
    """Rows the run-creation interception seam would bind for *pipeline_id*."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        rows = await load_pipeline_guardrail_rows(
            session,
            pipeline_id=pipeline_id,
            organisation_id=org_id,
        )
    return len(rows)


async def test_pipeline_created_after_apply_inherits_org_guardrails(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
):
    org_id = await _seed_org(db_engine, "GR-Fresh-A")
    admin_id = await _seed_user(db_engine, org_id, "gr-fresh-admin-a@test.local")
    existing_pipeline_id = await _seed_pipeline(db_engine, org_id, admin_id, "GR-Fresh-Existing")

    # --- apply the org config while exactly one pipeline exists -------------
    await _propose(integration_client, org_id, admin_id, _CONFIG_YAML)
    applied = await _apply(integration_client, org_id, admin_id)
    assert applied["applied"] is True

    existing_rows = await _org_level_rows_for_pipeline(db_engine, org_id, existing_pipeline_id)
    assert existing_rows == ["no-aws-keys", "valid-payload"]

    # --- create a NEW pipeline after the apply, via the real API route ------
    api_pipeline_id = await _create_pipeline_via_api(integration_client, org_id, admin_id, "GR-Fresh-Created-Via-API")

    # --- and a second new pipeline through a raw INSERT ----------------------
    # Raw SQL bypasses the ORM creation chokepoint (and there is no DB trigger
    # on pipelines — verified in the FAR-1593 observation run), so this pins the
    # seam the fix lives on: only ORM creation materialises the config.
    raw_pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')",
            ),
            {"id": str(raw_pipeline_id), "oid": str(org_id), "name": "GR-Fresh-Created-Raw", "uid": str(admin_id)},
        )

    # --- the behaviour under test -------------------------------------------
    api_rows = await _org_level_rows_for_pipeline(db_engine, org_id, api_pipeline_id)
    raw_rows = await _org_level_rows_for_pipeline(db_engine, org_id, raw_pipeline_id)
    api_enforced = await _enforcement_rows(db_engine, org_id, api_pipeline_id)
    existing_enforced = await _enforcement_rows(db_engine, org_id, existing_pipeline_id)
    drift = await _drift(integration_client, org_id, admin_id)

    # Regression (FAR-1593): the pipeline created through the product inherits
    # the applied org default in the creation transaction — this assertion is
    # what fails without the creation-time seed.
    assert api_rows == ["no-aws-keys", "valid-payload"]
    # ...and the enforcement seam therefore binds it for runs.
    assert api_enforced == 2
    assert existing_enforced == 2
    # The drift surface stays clean: nothing diverged from the applied pin.
    assert drift["status"] == "clean"
    assert drift["current_hash"] == drift["applied_hash"]
    # Raw SQL is outside the ORM seam by design (no production path inserts
    # pipelines that way) — recorded so the boundary of the fix is explicit.
    assert not raw_rows

    # --- re-apply stays idempotent on the already-seeded pipeline ------------
    await _propose(integration_client, org_id, admin_id, _CONFIG_YAML)
    reapply = await _apply(integration_client, org_id, admin_id)
    assert reapply["applied"] is True
    assert await _org_level_rows_for_pipeline(db_engine, org_id, api_pipeline_id) == [
        "no-aws-keys",
        "valid-payload",
    ]
    assert await _enforcement_rows(db_engine, org_id, api_pipeline_id) == 2


async def test_pipeline_created_without_applied_config_seeds_nothing(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
):
    """No applied guardrail config → pipeline creation is a fast, silent no-op."""
    org_id = await _seed_org(db_engine, "GR-Fresh-B")
    admin_id = await _seed_user(db_engine, org_id, "gr-fresh-admin-b@test.local")

    pipeline_id = await _create_pipeline_via_api(integration_client, org_id, admin_id, "GR-Fresh-NoConfig")

    assert not await _org_level_rows_for_pipeline(db_engine, org_id, pipeline_id)
    drift = await _drift(integration_client, org_id, admin_id)
    # A never-applied org reads clean, not drift — creation must not invent one.
    assert drift["status"] == "clean"
    assert drift["applied_hash"] is None

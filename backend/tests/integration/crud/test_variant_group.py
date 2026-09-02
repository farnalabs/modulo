"""Integration tests for VariantGroup CRUD.

RLS is set to test_org; all inserts are rolled back after each test.
Requires Postgres via testcontainers.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import insert as sa_insert
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.db.crud.variant_group import (
    check_pipeline_run_quota,
    create_variant_group,
    get_batch_compare,
    get_batch_runs,
    get_coverage_gaps,
    get_variant_group,
    list_variant_groups,
    soft_delete_variant_group,
    update_variant_group,
    validate_batch_ownership,
)
from modulo.db.models.run import Run
from modulo.db.models.variant_group import VariantGroup

pytestmark = [
    pytest.mark.integration,
]


def _make_variants() -> list[dict[str, Any]]:
    return [
        {
            "snapshot_id": str(uuid.uuid4()),
            "name": "control",
            "weight": 1.0,
            "run_context_overrides": {},
            "eval_definition_ids": [],
        },
        {
            "snapshot_id": str(uuid.uuid4()),
            "name": "variant_a",
            "weight": 0.5,
            "run_context_overrides": {"model": "gpt-4"},
            "eval_definition_ids": [],
        },
    ]


async def _insert_test_snapshot(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_version: int = 1,
) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, :ver, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "ver": snapshot_version,
            },
        )
    return snapshot_id


async def _insert_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    batch_id: uuid.UUID,
    variant_config_snapshot: dict[str, Any],
    run_number: int,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await session.execute(
        sa_insert(Run).values(
            id=run_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="manual",
            status="complete",
            input_hash=uuid.uuid4().hex,
            langgraph_thread_id=f"thread-{run_id.hex[:16]}",
            run_number=run_number,
            total_cost_usd=0.5,
            total_tokens=100,
            batch_id=batch_id,
            variant_config_snapshot=variant_config_snapshot,
        )
    )
    return run_id


async def _insert_test_org(db_engine: AsyncEngine, org_id: uuid.UUID) -> None:
    """Seed a real ``organisations`` row so foreign-org FKs are satisfiable."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "Variant Foreign Org",
                "slug": f"variant-foreign-{org_id.hex[:8]}",
            },
        )


async def _create_test_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, test_user: uuid.UUID) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    # Unique per seed: the shared session org + uq_pipelines_org_name make a
    # fixed name collide with every later seed in the same pytest session
    # (the raw-SQL insert here commits; rls_session rollbacks don't cover it).
    pipeline_name = f"variant-test-pipeline-{uuid.uuid4().hex[:10]}"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :org_id, :name, :uid, '{}'::json, '[]'::json)",
            ),
            {"id": str(pipeline_id), "org_id": str(org_id), "name": pipeline_name, "uid": str(test_user)},
        )
    return pipeline_id


async def test_create_variant_group(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    variants = _make_variants()
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="A/B Test Group",
        variants=variants,
        description="Testing A/B variants",
    )
    assert group.id is not None
    assert group.name == "A/B Test Group"
    assert group.organisation_id == test_org
    assert group.pipeline_id == pipeline_id
    assert group.selection_strategy == "weighted"
    assert group.run_count == 0
    assert len(group.variants) == 2
    assert isinstance(group, VariantGroup)


async def test_get_variant_group_returns_existing(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Get Test",
        variants=[],
    )
    fetched = await get_variant_group(rls_session, group.id)
    assert fetched is not None
    assert fetched.id == group.id
    assert fetched.name == "Get Test"


async def test_get_variant_group_returns_none_for_unknown(
    rls_session: AsyncSession,
) -> None:
    assert await get_variant_group(rls_session, uuid.uuid4()) is None


async def test_list_variant_groups_pagination(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    for i in range(3):
        await create_variant_group(
            rls_session,
            org_id=test_org,
            pipeline_id=pipeline_id,
            name=f"Group {i}",
            variants=[],
        )

    items, total = await list_variant_groups(rls_session, page=1, page_size=2)
    assert total >= 3
    assert len(items) == 2


async def test_list_variant_groups_filtered_by_pipeline(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipe_a = await _create_test_pipeline(db_engine, test_org, test_user)
    pipe_b = await _create_test_pipeline(db_engine, test_org, test_user)
    await create_variant_group(rls_session, org_id=test_org, pipeline_id=pipe_a, name="A-1", variants=[])
    await create_variant_group(rls_session, org_id=test_org, pipeline_id=pipe_b, name="B-1", variants=[])

    items_a, total_a = await list_variant_groups(rls_session, pipeline_id=pipe_a)
    assert total_a == 1
    assert items_a[0].name == "A-1"


async def test_update_variant_group(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Old Name",
        variants=[],
    )
    updated = await update_variant_group(rls_session, group.id, name="New Name", max_concurrent_runs=10)
    assert updated is not None
    assert updated.name == "New Name"
    assert updated.max_concurrent_runs == 10


async def test_update_variant_group_unknown_returns_none(
    rls_session: AsyncSession,
) -> None:
    assert await update_variant_group(rls_session, uuid.uuid4(), name="x") is None


async def test_soft_delete_variant_group(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Delete Me",
        variants=[],
    )
    assert await soft_delete_variant_group(rls_session, group.id) is True
    assert await get_variant_group(rls_session, group.id) is None


async def test_soft_delete_variant_group_unknown_returns_false(
    rls_session: AsyncSession,
) -> None:
    assert await soft_delete_variant_group(rls_session, uuid.uuid4()) is False


async def test_check_pipeline_run_quota_allows_within_limit(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Quota Test",
        variants=[],
        max_concurrent_runs=5,
    )
    assert await check_pipeline_run_quota(rls_session, group) is True


async def test_coverage_gaps_detects_missing_evals(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    eval_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO eval_definitions "
                "(id, organisation_id, pipeline_id, name, eval_type, config_json, account_id) "
                "VALUES (:id, :org_id, :pipeline_id, :name, 'llm_judge', '{}'::json, :uid)",
            ),
            {
                "id": str(eval_id),
                "org_id": str(test_org),
                "pipeline_id": str(pipeline_id),
                "name": "test-eval",
                "uid": str(test_user),
            },
        )

    variants_with_gap = [
        {
            "snapshot_id": str(uuid.uuid4()),
            "name": "no-evals",
            "weight": 1.0,
            "eval_definition_ids": [],
        },
    ]
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Coverage Test",
        variants=variants_with_gap,
    )

    gaps = await get_coverage_gaps(rls_session, group)
    assert len(gaps) == 1
    assert gaps[0]["variant"]["name"] == "no-evals"
    assert gaps[0]["missing_evals"]


async def test_no_coverage_gaps_when_all_evals_present(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    eval_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO eval_definitions "
                "(id, organisation_id, pipeline_id, name, eval_type, config_json, account_id) "
                "VALUES (:id, :org_id, :pipeline_id, :name, 'llm_judge', '{}'::json, :uid)",
            ),
            {
                "id": str(eval_id),
                "org_id": str(test_org),
                "pipeline_id": str(pipeline_id),
                "name": "test-eval",
                "uid": str(test_user),
            },
        )

    variants_covered = [
        {
            "snapshot_id": str(uuid.uuid4()),
            "name": "covered",
            "weight": 1.0,
            "eval_definition_ids": [str(eval_id)],
        },
    ]
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Full Coverage",
        variants=variants_covered,
    )

    gaps = await get_coverage_gaps(rls_session, group)
    assert not gaps


async def test_batch_runs_load_by_batch_id_org_scoped(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    """FAR-332 3d/3i: a batch loads purely by batch_id and returns exactly its runs."""
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    snapshot_id = await _insert_test_snapshot(db_engine, test_org, pipeline_id)
    batch_id = uuid.uuid4()

    run_ids = []
    for i in range(2):
        run_ids.append(
            await _insert_run(
                rls_session,
                org_id=test_org,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                batch_id=batch_id,
                variant_config_snapshot={
                    "variant_id": f"variant-{i}",
                    "variant_name": f"variant-{i}",
                    "snapshot_id": str(snapshot_id),
                    "run_context_overrides": {"model_backend_id": f"backend-{i}"},
                    "batch_id": str(batch_id),
                },
                run_number=i + 1,
            )
        )

    runs = await get_batch_runs(rls_session, org_id=test_org, batch_id=batch_id)
    assert {run.id for run in runs} == set(run_ids)
    assert len(runs) == 2
    assert all(run.batch_id == batch_id for run in runs)


async def test_batch_compare_exposes_frozen_snapshot_status_cost_tokens(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    """FAR-332 3d: compare exposes status, cost, tokens and the frozen snapshot per run."""
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    snapshot_id = await _insert_test_snapshot(db_engine, test_org, pipeline_id)
    batch_id = uuid.uuid4()

    await _insert_run(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        batch_id=batch_id,
        variant_config_snapshot={
            "variant_id": "variant-control",
            "variant_name": "control",
            "snapshot_id": str(snapshot_id),
            "run_context_overrides": {"model_backend_id": "backend-a"},
            "batch_id": str(batch_id),
        },
        run_number=1,
    )
    await _insert_run(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        batch_id=batch_id,
        variant_config_snapshot={
            "variant_id": "variant-experiment",
            "variant_name": "experiment",
            "snapshot_id": str(snapshot_id),
            "run_context_overrides": {"model_backend_id": "backend-b"},
            "batch_id": str(batch_id),
        },
        run_number=2,
    )

    entries = await get_batch_compare(rls_session, org_id=test_org, batch_id=batch_id)
    assert len(entries) == 2
    names = {e["variant_name"] for e in entries}
    assert names == {"control", "experiment"}
    for e in entries:
        assert e["status"] == "complete"
        assert e["total_cost_usd"] == 0.5
        assert e["total_tokens"] == 100
        assert str(e["snapshot_id"]) == str(snapshot_id)
        assert "override_diff" in e
        assert e["run_context_overrides"]["model_backend_id"] in ("backend-a", "backend-b")


async def test_cross_org_batch_returns_empty(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    """FAR-332 3d/3f: another org's batch_id resolves to no org-owned runs (IDOR backstop)."""
    foreign_org = uuid.uuid4()
    await _insert_test_org(db_engine, foreign_org)
    foreign_pipeline = await _create_test_pipeline(db_engine, foreign_org, test_user)
    foreign_snapshot = await _insert_test_snapshot(db_engine, foreign_org, foreign_pipeline)
    foreign_batch = uuid.uuid4()

    async with db_engine.connect() as conn, conn.begin():
        await _insert_run(
            conn,
            org_id=foreign_org,
            pipeline_id=foreign_pipeline,
            snapshot_id=foreign_snapshot,
            batch_id=foreign_batch,
            variant_config_snapshot={
                "variant_id": "x",
                "variant_name": "x",
                "snapshot_id": str(foreign_snapshot),
                "run_context_overrides": {},
                "batch_id": str(foreign_batch),
            },
            run_number=1,
        )

    # test_org's RLS-scoped session must NOT see the foreign org's batch.
    runs = await get_batch_runs(rls_session, org_id=test_org, batch_id=foreign_batch)
    assert runs == []
    assert not (await get_batch_compare(rls_session, org_id=test_org, batch_id=foreign_batch))


async def test_validate_batch_ownership_allows_owned_pipeline_and_snapshots(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    """FAR-332 3f: owned pipeline + owned snapshots passes the fail-closed guard (real DB)."""
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    snap_a = await _insert_test_snapshot(db_engine, test_org, pipeline_id, snapshot_version=1)
    snap_b = await _insert_test_snapshot(db_engine, test_org, pipeline_id, snapshot_version=2)

    assert (
        await validate_batch_ownership(
            rls_session,
            org_id=test_org,
            pipeline_id=pipeline_id,
            snapshot_ids=[snap_a, snap_b],
        )
        is True
    )


async def test_validate_batch_ownership_rejects_foreign_org_snapshot(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    """FAR-332 3f: a cross-org snapshot reference fails the guard (real DB)."""
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    owned_snap = await _insert_test_snapshot(db_engine, test_org, pipeline_id)

    foreign_org = uuid.uuid4()
    await _insert_test_org(db_engine, foreign_org)
    foreign_pipeline = await _create_test_pipeline(db_engine, foreign_org, test_user)
    foreign_snap = await _insert_test_snapshot(db_engine, foreign_org, foreign_pipeline)

    # Owned pipeline but one snapshot belongs to a different org -> must reject.
    assert (
        await validate_batch_ownership(
            rls_session,
            org_id=test_org,
            pipeline_id=pipeline_id,
            snapshot_ids=[owned_snap, foreign_snap],
        )
        is False
    )


async def test_validate_batch_ownership_rejects_foreign_org_pipeline(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    """FAR-332 3f: a cross-org pipeline reference fails the guard (real DB)."""
    owned_pipeline = await _create_test_pipeline(db_engine, test_org, test_user)
    owned_snap = await _insert_test_snapshot(db_engine, test_org, owned_pipeline)

    foreign_org = uuid.uuid4()
    await _insert_test_org(db_engine, foreign_org)
    foreign_pipeline = await _create_test_pipeline(db_engine, foreign_org, test_user)

    # Pipeline belongs to a different org (snapshots are owned) -> must reject.
    assert (
        await validate_batch_ownership(
            rls_session,
            org_id=test_org,
            pipeline_id=foreign_pipeline,
            snapshot_ids=[owned_snap],
        )
        is False
    )


async def test_validate_batch_ownership_allows_empty_snapshot_list(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    """FAR-332 3f: an owned pipeline with no snapshots is allowed (real DB)."""
    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)

    assert (
        await validate_batch_ownership(
            rls_session,
            org_id=test_org,
            pipeline_id=pipeline_id,
            snapshot_ids=[],
        )
        is True
    )


async def test_create_group_persists_minted_variant_ids(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    """FAR-332 3b: variants supplied without an id persist with a minted id (round-trip)."""
    from modulo.api.routes.variants import _mint_variant_ids

    pipeline_id = await _create_test_pipeline(db_engine, test_org, test_user)
    pre_minted_id = str(uuid.uuid4())
    raw_variants = [
        {
            "id": pre_minted_id,
            "snapshot_id": str(uuid.uuid4()),
            "name": "control",
            "weight": 1.0,
            "run_context_overrides": {},
            "eval_definition_ids": [],
        },
        {
            # No id -> route mints one before persisting.
            "snapshot_id": str(uuid.uuid4()),
            "name": "variant_a",
            "weight": 0.5,
            "run_context_overrides": {"model": "gpt-4"},
            "eval_definition_ids": [],
        },
    ]

    minted = _mint_variant_ids(raw_variants)
    assert minted[0]["id"] == pre_minted_id
    assert minted[1]["id"] is not None
    uuid.UUID(minted[1]["id"])  # must be a valid UUID

    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Mint Round Trip",
        variants=minted,
    )
    await rls_session.flush()

    fetched = await get_variant_group(rls_session, group.id)
    assert fetched is not None
    stored = fetched.variants
    assert len(stored) == 2
    ids = {v["id"] for v in stored}
    assert pre_minted_id in ids
    # The legacy variant with no id now carries a persisted, valid uuid.
    stored_unknown = next(v for v in stored if v["name"] == "variant_a")
    assert stored_unknown["id"] is not None
    uuid.UUID(stored_unknown["id"])

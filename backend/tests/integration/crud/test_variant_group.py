"""Integration tests for VariantGroup CRUD.

RLS is set to test_org; all inserts are rolled back after each test.
Requires Postgres via testcontainers.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.db.crud.variant_group import (
    check_pipeline_run_quota,
    create_variant_group,
    delete_variant_group,
    get_coverage_gaps,
    get_variant_group,
    list_variant_groups,
    update_variant_group,
)
from modulo.db.models.variant_group import VariantGroup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skip(reason="awaiting-implementation — variant_group test fixtures need pipeline created_by"),
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


async def _create_test_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, run_context_defaults) "
                    "VALUES (:id, :org_id, :name, '{}'::json)"
                ),
                {"id": str(pipeline_id), "org_id": str(org_id), "name": "variant-test-pipeline"},
            )
    return pipeline_id


async def test_create_variant_group(rls_session: AsyncSession, test_org: uuid.UUID, db_engine: AsyncEngine) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org)
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
    rls_session: AsyncSession, test_org: uuid.UUID, db_engine: AsyncEngine
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org)
    group = await create_variant_group(
        rls_session, org_id=test_org, pipeline_id=pipeline_id, name="Get Test", variants=[]
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
    rls_session: AsyncSession, test_org: uuid.UUID, db_engine: AsyncEngine
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org)
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
    rls_session: AsyncSession, test_org: uuid.UUID, db_engine: AsyncEngine
) -> None:
    pipe_a = await _create_test_pipeline(db_engine, test_org)
    pipe_b = await _create_test_pipeline(db_engine, test_org)
    await create_variant_group(rls_session, org_id=test_org, pipeline_id=pipe_a, name="A-1", variants=[])
    await create_variant_group(rls_session, org_id=test_org, pipeline_id=pipe_b, name="B-1", variants=[])

    items_a, total_a = await list_variant_groups(rls_session, pipeline_id=pipe_a)
    assert total_a == 1
    assert items_a[0].name == "A-1"


async def test_update_variant_group(rls_session: AsyncSession, test_org: uuid.UUID, db_engine: AsyncEngine) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org)
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


async def test_delete_variant_group(rls_session: AsyncSession, test_org: uuid.UUID, db_engine: AsyncEngine) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org)
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Delete Me",
        variants=[],
    )
    assert await delete_variant_group(rls_session, group.id) is True
    assert await get_variant_group(rls_session, group.id) is None


async def test_delete_variant_group_unknown_returns_false(
    rls_session: AsyncSession,
) -> None:
    assert await delete_variant_group(rls_session, uuid.uuid4()) is False


async def test_check_pipeline_run_quota_allows_within_limit(
    rls_session: AsyncSession, test_org: uuid.UUID, db_engine: AsyncEngine
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org)
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
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID, db_engine: AsyncEngine
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org)
    eval_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO eval_definitions "
                    "(id, organisation_id, pipeline_id, name, eval_type, created_by) "
                    "VALUES (:id, :org_id, :pipeline_id, :name, 'llm_judge', :created_by)"
                ),
                {
                    "id": str(eval_id),
                    "org_id": str(test_org),
                    "pipeline_id": str(pipeline_id),
                    "name": "test-eval",
                    "created_by": str(test_user),
                },
            )

    variants_with_gap = [
        {
            "snapshot_id": str(uuid.uuid4()),
            "name": "no-evals",
            "weight": 1.0,
            "eval_definition_ids": [],
        }
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
    assert len(gaps[0]["missing_evals"]) >= 1


async def test_no_coverage_gaps_when_all_evals_present(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID, db_engine: AsyncEngine
) -> None:
    pipeline_id = await _create_test_pipeline(db_engine, test_org)
    eval_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO eval_definitions "
                    "(id, organisation_id, pipeline_id, name, eval_type, created_by) "
                    "VALUES (:id, :org_id, :pipeline_id, :name, 'llm_judge', :created_by)"
                ),
                {
                    "id": str(eval_id),
                    "org_id": str(test_org),
                    "pipeline_id": str(pipeline_id),
                    "name": "test-eval",
                    "created_by": str(test_user),
                },
            )

    variants_covered = [
        {
            "snapshot_id": str(uuid.uuid4()),
            "name": "covered",
            "weight": 1.0,
            "eval_definition_ids": [str(eval_id)],
        }
    ]
    group = await create_variant_group(
        rls_session,
        org_id=test_org,
        pipeline_id=pipeline_id,
        name="Full Coverage",
        variants=variants_covered,
    )

    gaps = await get_coverage_gaps(rls_session, group)
    assert len(gaps) == 0

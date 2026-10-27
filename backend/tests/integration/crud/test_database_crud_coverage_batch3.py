"""Real async database coverage for FAR-366 CRUD batch 3."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.core.trigger_engine import TriggerConfigInvalidError, TriggerNotFoundError
from modulo.db.crud.connector_instance import (
    clear_degraded_markers,
    create_connector_instance,
    list_connector_instances,
    mark_instances_degraded,
)
from modulo.db.crud.error_tracking import (
    count_error_events_by_group,
    count_error_groups,
    create_error_event,
    get_error_events_by_group,
    get_error_groups,
    scheduler_starvation_age_anchor,
    update_error_group,
    upsert_error_group,
)
from modulo.db.crud.eval_run import get_evals_for_runs
from modulo.db.crud.last_admin_guard import LastAdminLockoutError, assert_not_last_admin
from modulo.db.crud.node_observation import observe_node
from modulo.db.crud.trigger import (
    list_triggers,
    load_trigger_and_org_global,
    restore_trigger,
    soft_delete_trigger,
)

pytestmark = pytest.mark.integration


async def _insert_run(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "langgraph_thread_id, trigger_type, input_hash, run_number) "
                "VALUES (:id, :oid, :pid, :sid, :thread, 'manual', :hash, 1)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "thread": str(uuid.uuid4()),
                "hash": uuid.uuid4().hex * 2,
            },
        )
    return run_id


async def _insert_trigger(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    config: str = "{}",
    name: str | None = None,
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, name, trigger_type, active, "
                "max_concurrent_runs, config_json, account_id) "
                "VALUES (:id, :oid, :pid, :name, 'webhook', true, 2, CAST(:config AS jsonb), :aid)"
            ),
            {
                "id": str(trigger_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "name": name or f"trigger-{trigger_id.hex[:8]}",
                "config": config,
                "aid": str(account_id),
            },
        )
    return trigger_id


async def test_connector_cursor_and_org_filtering(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    first = await create_connector_instance(
        rls_session,
        org_id=test_org,
        name=f"cursor-native-{uuid.uuid4().hex[:8]}",
        connector_type_id="filesystem",
        account_id=test_user,
        credentials_ciphertext=b"cipher",
        tier="native",
    )
    await create_connector_instance(
        rls_session,
        org_id=test_org,
        name=f"cursor-dev-{uuid.uuid4().hex[:8]}",
        connector_type_id="filesystem",
        account_id=test_user,
        credentials_ciphertext=b"cipher",
        tier="in_dev",
    )
    page = await list_connector_instances(
        rls_session,
        organisation_id=test_org,
        excluded_tiers=[],
        page_size=10,
    )
    assert page.items
    assert page.total >= 2
    from modulo.db.crud.pagination import CursorPaginator

    cursor_row = page.items[0]
    cursor = CursorPaginator.encode_cursor(cursor_row.created_at, cursor_row.id)
    cursor_page = await list_connector_instances(
        rls_session,
        organisation_id=test_org,
        excluded_tiers=[],
        cursor=cursor,
        page_size=10,
    )
    assert cursor_page.total is not None
    assert cursor_row.id not in {item.id for item in cursor_page.items}

    await mark_instances_degraded(rls_session, {first.id: "temporary"})
    await clear_degraded_markers(rls_session, [first.id])
    refreshed_error = await rls_session.scalar(select(type(first).last_skip_error).where(type(first).id == first.id))
    assert refreshed_error is None


async def test_error_tracking_filters_counts_and_resolves(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    fingerprint = f"crud3-{uuid.uuid4().hex}"
    event = await create_error_event(
        rls_session,
        org_id=test_org,
        fingerprint=fingerprint,
        level="error",
        message="database failed",
        source="backend",
        environment="test",
    )
    group = await upsert_error_group(
        rls_session,
        org_id=test_org,
        fingerprint=fingerprint,
        level="error",
        sample_event_id=event.id,
    )
    await create_error_event(
        rls_session,
        org_id=test_org,
        fingerprint=fingerprint,
        level="warning",
        message="database warning",
        source="backend",
        environment="test",
    )
    groups = await get_error_groups(rls_session, org_id=test_org, source="backend", search="database")
    assert group in groups
    assert await count_error_groups(rls_session, org_id=test_org, environment="test") >= 1
    assert await get_error_events_by_group(rls_session, org_id=test_org, group_id=group.id)
    assert await count_error_events_by_group(rls_session, org_id=test_org, group_id=group.id) == 2

    updated = await update_error_group(
        rls_session,
        org_id=test_org,
        group_id=group.id,
        status="acknowledged",
        assigned_to=test_user,
    )
    assert updated.status == "acknowledged"
    assert updated.assigned_to is not None
    assert await count_error_events_by_group(rls_session, org_id=test_org, group_id=group.id) == 2
    anchor = scheduler_starvation_age_anchor()
    assert "coalesce" in str(anchor).lower()


async def test_eval_empty_run_ids_is_a_real_noop(rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    result = await get_evals_for_runs(rls_session, org_id=test_org, run_ids=[])
    assert not result


async def test_node_observation_is_idempotent(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
    db_engine: AsyncEngine,
) -> None:
    run_id = await _insert_run(db_engine, org_id=test_org, pipeline_id=test_pipeline, snapshot_id=test_snapshot)
    node_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO node_observations "
                "(id, organisation_id, run_id, node_id, account_id, human_observed_at) "
                "VALUES (:id, :oid, :rid, :nid, :aid, CURRENT_TIMESTAMP)"
            ),
            {
                "id": str(observation_id),
                "oid": str(test_org),
                "rid": str(run_id),
                "nid": str(node_id),
                "aid": str(test_user),
            },
        )
    existing = await observe_node(
        rls_session,
        organisation_id=test_org,
        run_id=run_id,
        node_id=str(node_id),
        observed_by=None,
    )
    assert existing.id == observation_id


async def test_last_admin_guard_uses_real_tenant_membership(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    await assert_not_last_admin(
        rls_session,
        org_id=test_org,
        target_account_id=test_user,
        target_role_after="admin",
        target_active_after=True,
    )
    with pytest.raises(LastAdminLockoutError):
        await assert_not_last_admin(
            rls_session,
            org_id=test_org,
            target_account_id=test_user,
            target_role_after="operator",
            target_active_after=False,
        )


async def test_trigger_crud_and_global_bootstrap_paths(
    rls_session: AsyncSession,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_trigger: uuid.UUID,
) -> None:
    extra_id = await _insert_trigger(
        db_engine,
        org_id=test_org,
        pipeline_id=test_pipeline,
        account_id=test_user,
    )
    listed = await list_triggers(rls_session, test_org, pipeline_id=test_pipeline, limit=10)
    assert len(listed.items) >= 2
    assert listed.has_more is False

    from modulo.db.crud.pagination import CursorPaginator

    cursor = CursorPaginator.encode_cursor(listed.items[0].created_at, listed.items[0].id)
    cursor_result = await list_triggers(rls_session, test_org, cursor=cursor, limit=10)
    assert cursor_result.page == 1

    trigger = await soft_delete_trigger(rls_session, extra_id)
    assert trigger is not None
    assert await soft_delete_trigger(rls_session, extra_id) is None
    restored = await restore_trigger(rls_session, extra_id)
    assert restored is not None
    assert await restore_trigger(rls_session, uuid.uuid4()) is None

    loaded, loaded_org = await load_trigger_and_org_global(db_session, test_trigger, test_org)
    assert loaded.id == test_trigger
    assert loaded_org == test_org
    with pytest.raises(TriggerNotFoundError):
        await load_trigger_and_org_global(db_session, test_trigger, uuid.uuid4())
    with pytest.raises(TriggerNotFoundError):
        await load_trigger_and_org_global(db_session, uuid.uuid4(), None)

    invalid_id = await _insert_trigger(
        db_engine,
        org_id=test_org,
        pipeline_id=test_pipeline,
        account_id=test_user,
        config="[]",
    )
    with pytest.raises(TriggerConfigInvalidError):
        await load_trigger_and_org_global(db_session, invalid_id, None)

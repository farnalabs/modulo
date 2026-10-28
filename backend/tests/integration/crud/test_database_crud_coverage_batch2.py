"""Real async database coverage for FAR-366 CRUD batch 2."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.db.crud.connector_instance import (
    clear_degraded_markers,
    create_connector_instance,
    list_connector_instances,
    mark_instances_degraded,
)
from modulo.db.crud.eval_run import get_evals_for_runs, get_run_evals
from modulo.db.crud.org_membership import (
    get_membership_by_account_and_org,
    list_memberships_for_account,
    list_memberships_for_org,
    reactivate_membership,
    resolve_role_from_membership,
    update_membership_role,
)
from modulo.db.crud.template import (
    _agent_count_from_content,
    _preview_data_from_content,
    get_template,
    list_templates,
)
from modulo.db.models.library_primitive import LibraryPrimitive

pytestmark = pytest.mark.integration


async def _insert_run(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots "
                "(id, pipeline_id, organisation_id, snapshot_version, graph_json, "
                "connector_bindings_json, schema_pins_json, prompt_pins_json, "
                "model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:sid, :pid, :oid, 2, '{}'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"sid": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO runs "
                "(id, organisation_id, pipeline_id, snapshot_id, langgraph_thread_id, "
                "trigger_type, input_hash, run_number) "
                "VALUES (:rid, :oid, :pid, :sid, :thread, 'manual', :hash, :number)"
            ),
            {
                "rid": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "thread": str(uuid.uuid4()),
                "hash": uuid.uuid4().hex * 2,
                "number": int(run_id.int % 10**9) + 1,
            },
        )
    return run_id


async def test_connector_instance_crud_cursor_and_marker_paths(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    first = await create_connector_instance(
        rls_session,
        org_id=test_org,
        name=f"batch2-{uuid.uuid4().hex}",
        connector_type_id="filesystem",
        account_id=test_user,
        credentials_ciphertext=b"cipher",
    )
    await create_connector_instance(
        rls_session,
        org_id=test_org,
        name=f"batch2-dev-{uuid.uuid4().hex}",
        connector_type_id="filesystem",
        account_id=test_user,
        credentials_ciphertext=b"cipher",
        tier="in_dev",
    )
    page = await list_connector_instances(rls_session, organisation_id=test_org, page_size=10)
    assert page.total >= 1
    assert all(item.tier != "in_dev" for item in page.items)
    await mark_instances_degraded(rls_session, {first.id: "bad\\x00" + "x" * 3000})
    await clear_degraded_markers(rls_session, [first.id])
    refreshed = await list_connector_instances(rls_session, organisation_id=test_org, excluded_tiers=[])
    assert any(item.id == first.id for item in refreshed.items)


async def test_eval_run_crud_excludes_guardrail_and_error_rows(
    db_engine: AsyncEngine,
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    test_pipeline: uuid.UUID,
) -> None:
    run_id = await _insert_run(db_engine, org_id=test_org, pipeline_id=test_pipeline)
    normal_id = uuid.uuid4()
    guardrail_id = uuid.uuid4()
    error_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        for eval_id, eval_type, name in (
            (normal_id, "model_judge", "normal"),
            (guardrail_id, "guardrail", "guardrail"),
            (error_id, "regex", "error"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO evals "
                    "(id, organisation_id, pipeline_id, name, eval_type, config_json, account_id) "
                    "VALUES (:eid, :oid, :pid, :name, :eval_type, '{}'::jsonb, :aid)"
                ),
                {
                    "eid": str(eval_id),
                    "oid": str(test_org),
                    "pid": str(test_pipeline),
                    "name": name,
                    "eval_type": eval_type,
                    "aid": str(test_user),
                },
            )
        for eval_id, outcome, passed in (
            (normal_id, "pass", True),
            (guardrail_id, "pass", True),
            (error_id, "error", False),
        ):
            await conn.execute(
                text(
                    "INSERT INTO eval_results "
                    "(id, organisation_id, run_id, eval_id, passed, outcome) "
                    "VALUES (:rid, :oid, :run, :eid, :passed, :outcome)"
                ),
                {
                    "rid": str(uuid.uuid4()),
                    "oid": str(test_org),
                    "run": str(run_id),
                    "eid": str(eval_id),
                    "passed": passed,
                    "outcome": outcome,
                },
            )

    results = await get_run_evals(rls_session, run_id)
    assert [result.eval_id for result in results] == [normal_id]
    batch_results = await get_evals_for_runs(rls_session, org_id=test_org, run_ids=[run_id])
    assert [result.eval_id for result in batch_results] == [normal_id]


async def test_template_crud_filters_type_category_search_and_preview(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    matching = LibraryPrimitive(
        organisation_id=test_org,
        source="local",
        primitive_type="pipeline_template",
        name="Release Template",
        slug=f"release-{uuid.uuid4().hex}",
        author="tester",
        version="1.0.0",
        category="delivery",
        content_json={
            "agents": [{"id": "a1"}],
            "graph_nodes": [{"id": "n1", "label": "Build"}],
            "edges": [{"source_node_id": "n1", "target_node_id": "n2"}],
        },
        account_id=test_user,
    )
    other_type = LibraryPrimitive(
        organisation_id=test_org,
        source="local",
        primitive_type="agent",
        name="Release Agent",
        slug=f"agent-{uuid.uuid4().hex}",
        author="tester",
        version="1.0.0",
        content_json={},
        account_id=test_user,
    )
    rls_session.add_all([matching, other_type])
    await rls_session.flush()

    page = await list_templates(rls_session, category="delivery", search="release")
    assert page.total == 1
    assert page.items[0].id == matching.id
    assert page.has_more is False
    assert await get_template(rls_session, matching.id) is matching
    assert await get_template(rls_session, other_type.id) is None
    assert _agent_count_from_content(matching.content_json) == 1
    assert _agent_count_from_content({}) == 0
    assert _preview_data_from_content(matching.content_json) == {
        "nodes": [{"id": "n1", "label": "Build", "node_type": "agent"}],
        "edges": [{"source": "n1", "target": "n2", "edge_type": "normal"}],
    }


async def test_org_membership_crud_uses_live_tenant_account(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    membership = await get_membership_by_account_and_org(rls_session, test_user, test_org)
    assert membership is not None
    assert membership.account_id == test_user
    assert await list_memberships_for_account(rls_session, test_user, active_only=True)
    assert membership in await list_memberships_for_org(rls_session, test_org)

    updated = await update_membership_role(rls_session, membership.id, "operator", org_id=test_org)
    assert updated is membership
    assert updated.role == "operator"
    assert await update_membership_role(rls_session, membership.id, "admin", org_id=uuid.uuid4()) is None

    membership.deactivated_at = membership.joined_at
    await rls_session.flush()
    active_memberships = await list_memberships_for_account(rls_session, test_user, active_only=True)
    assert not active_memberships
    assert await resolve_role_from_membership(rls_session, str(test_user), str(test_org)) is None
    reactivated = await reactivate_membership(rls_session, membership, "runner")
    assert reactivated is membership
    assert reactivated.deactivated_at is None
    assert await resolve_role_from_membership(rls_session, str(test_user), str(test_org)) == "runner"

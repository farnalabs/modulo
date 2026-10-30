"""Real-Postgres coverage for the admin trigger-event log's historical join.

The unit tests mock ``session.execute``, so the join, the tenancy predicates and
the soft-delete opt-out are never exercised there. These tests seed real rows in
two organisations and call the endpoint function against a NOBYPASSRLS session,
mirroring production (FAR-2226):

* an event whose pipeline is soft-deleted and one whose trigger is soft-deleted
  must still be returned, with the pipeline name;
* a soft-deleted (or archived) pipeline must be flagged ``pipeline_deleted`` so
  the UI drops the editor link without losing the name;
* ``total`` must count exactly the rows the page query can reach;
* no other organisation's events or pipeline names may leak.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.routes.admin_triggers import TriggerEventListResponse, list_trigger_events
from modulo.auth.jwt import TenantPrincipal

pytestmark = pytest.mark.integration

_MINIMAL_GRAPH_NODES = [
    {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
    }
]


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_account(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": email, "name": f"Admin {email}"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _seed_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
    *,
    deleted: bool = False,
    archived: bool = False,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility, "
                "deleted_at, archived_at) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, (:graph)::json, "
                "'manual_approval', 'org', "
                "CASE WHEN :deleted THEN now() ELSE NULL END, "
                "CASE WHEN :archived THEN now() ELSE NULL END)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(account_id),
                "graph": json.dumps(_MINIMAL_GRAPH_NODES),
                "deleted": deleted,
                "archived": archived,
            },
        )
    return pipeline_id


async def _seed_trigger(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    deleted: bool = False,
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, trigger_type, active, "
                "max_concurrent_runs, config_json, account_id, deleted_at) "
                "VALUES (:id, :oid, :pid, 'webhook', true, 5, '{}'::json, :uid, "
                "CASE WHEN :deleted THEN now() ELSE NULL END)"
            ),
            {
                "id": str(trigger_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "uid": str(account_id),
                "deleted": deleted,
            },
        )
    return trigger_id


async def _seed_event(db_engine: AsyncEngine, org_id: uuid.UUID, trigger_id: uuid.UUID) -> uuid.UUID:
    event_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO trigger_events (id, organisation_id, trigger_id, trigger_type, "
                "raw_payload_hash, validation_result) "
                "VALUES (:id, :oid, :tid, 'webhook', :hash, 'accepted')"
            ),
            {
                "id": str(event_id),
                "oid": str(org_id),
                "tid": str(trigger_id),
                "hash": uuid.uuid4().hex + uuid.uuid4().hex,
            },
        )
    return event_id


@pytest_asyncio.fixture
async def admin_events_session(app_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """A session running as a NOBYPASSRLS role, so row-level security applies."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()


def _principal(org_id: uuid.UUID, account_id: uuid.UUID) -> TenantPrincipal:
    return TenantPrincipal(
        username="integration-admin",
        organisation_id=org_id,
        account_id=account_id,
        org_role="admin",
    )


async def _list(
    session: AsyncSession,
    principal: TenantPrincipal,
) -> TriggerEventListResponse:
    return await list_trigger_events(
        trigger_type=None,
        validation_result=None,
        cursor=None,
        limit=25,
        session=session,
        principal=principal,
    )


class TestAdminTriggerEventHistory:
    async def test_soft_deleted_rows_survive_and_total_matches(
        self,
        db_engine: AsyncEngine,
        admin_events_session: AsyncSession,
    ) -> None:
        """Deleted trigger/pipeline history stays visible; total matches rows.

        Regression for FAR-2226: the join inherited the global soft-delete
        filter, so these events vanished from the log while ``total`` still
        counted them.
        """
        org_a = await _seed_org(db_engine, "far2226-org-a")
        user_a = await _seed_account(db_engine, org_a, f"far2226-a-{org_a.hex[:8]}@example.com")
        live_pipeline = await _seed_pipeline(db_engine, org_a, user_a, "Live pipeline")
        deleted_pipeline = await _seed_pipeline(db_engine, org_a, user_a, "Deleted pipeline", deleted=True)

        live_trigger = await _seed_trigger(db_engine, org_a, live_pipeline, user_a)
        trigger_on_deleted_pipeline = await _seed_trigger(db_engine, org_a, deleted_pipeline, user_a)
        deleted_trigger = await _seed_trigger(db_engine, org_a, live_pipeline, user_a, deleted=True)
        deleted_trigger_on_deleted_pipeline = await _seed_trigger(
            db_engine, org_a, deleted_pipeline, user_a, deleted=True
        )

        event_live = await _seed_event(db_engine, org_a, live_trigger)
        event_deleted_pipeline = await _seed_event(db_engine, org_a, trigger_on_deleted_pipeline)
        event_deleted_trigger = await _seed_event(db_engine, org_a, deleted_trigger)
        event_both_deleted = await _seed_event(db_engine, org_a, deleted_trigger_on_deleted_pipeline)

        org_b = await _seed_org(db_engine, "far2226-org-b")
        user_b = await _seed_account(db_engine, org_b, f"far2226-b-{org_b.hex[:8]}@example.com")
        other_pipeline = await _seed_pipeline(db_engine, org_b, user_b, "Other org pipeline")
        other_trigger = await _seed_trigger(db_engine, org_b, other_pipeline, user_b)
        event_other_org = await _seed_event(db_engine, org_b, other_trigger)

        response = await _list(admin_events_session, _principal(org_a, user_a))

        returned_ids = {item.id for item in response.items}
        assert returned_ids == {
            str(event_live),
            str(event_deleted_pipeline),
            str(event_deleted_trigger),
            str(event_both_deleted),
        }
        # The count must agree with the rows the page query can reach.
        assert response.total == 4

        by_id = {item.id: item for item in response.items}
        deleted_pipeline_item = by_id[str(event_deleted_pipeline)]
        assert deleted_pipeline_item.pipeline_name == "Deleted pipeline"
        assert deleted_pipeline_item.pipeline_deleted is True

        deleted_trigger_item = by_id[str(event_deleted_trigger)]
        assert deleted_trigger_item.pipeline_name == "Live pipeline"
        assert deleted_trigger_item.pipeline_deleted is False

        assert by_id[str(event_live)].pipeline_deleted is False
        assert by_id[str(event_both_deleted)].pipeline_deleted is True

        # Tenancy: the other org's event and pipeline name must not leak.
        assert str(event_other_org) not in returned_ids
        assert "Other org pipeline" not in {item.pipeline_name for item in response.items}

        other_response = await _list(admin_events_session, _principal(org_b, user_b))
        assert [item.id for item in other_response.items] == [str(event_other_org)]
        assert other_response.total == 1
        assert other_response.items[0].pipeline_name == "Other org pipeline"

    async def test_archived_pipeline_is_flagged_without_losing_the_name(
        self,
        db_engine: AsyncEngine,
        admin_events_session: AsyncSession,
    ) -> None:
        """An archived pipeline keeps its label but is not linkable (FAR-2226)."""
        org = await _seed_org(db_engine, "far2226-org-archived")
        user = await _seed_account(db_engine, org, f"far2226-arch-{org.hex[:8]}@example.com")
        archived_pipeline = await _seed_pipeline(db_engine, org, user, "Archived pipeline", archived=True)
        trigger = await _seed_trigger(db_engine, org, archived_pipeline, user)
        event_id = await _seed_event(db_engine, org, trigger)

        response = await _list(admin_events_session, _principal(org, user))

        assert response.total == 1
        assert len(response.items) == 1
        item = response.items[0]
        assert item.id == str(event_id)
        assert item.pipeline_name == "Archived pipeline"
        assert item.pipeline_deleted is True

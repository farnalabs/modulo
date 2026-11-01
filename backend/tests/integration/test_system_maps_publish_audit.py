"""Integration tests: System Maps publish endpoint audit outcomes (FAR-2236).

The `system_map_revision_published` audit event must record outcome="error"
when validation fails (refused publish) and outcome="success" when the revision
is actually published. The handler raises `PublishValidationErrorHTTP` on
validation failure so the `audited()` dependency observes the exception and
records an honest outcome.

These tests verify the audit append path works under strict RLS (NOBYPASSRLS role),
mirroring the pattern in test_api_key_audit_rls.py.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import TenantPrincipal
from modulo.core.system_map_persistence import SystemMapDiscoveryRepository
from modulo.db.models.system_map import SystemMap, SystemMapRevision
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings

pytestmark = pytest.mark.integration

_DISCOVERY_REPO = SystemMapDiscoveryRepository()


def _make_settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        modulo_db="postgres",
        secret_key="a" * 32,
        fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        redis_url="",
        modulo_admin_password="test",
    )


def _make_principal(org_id: uuid.UUID, account_id: uuid.UUID) -> TenantPrincipal:
    return TenantPrincipal(
        username="integration-user",
        organisation_id=org_id,
        account_id=account_id,
        org_role="admin",
    )


async def _fetch_audit_event(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    event_type: str,
) -> dict[str, Any] | None:
    """Read the most recent audit row for the org+type via a superuser connection.

    The write path is what matters here (it ran as the NOBYPASSRLS role); the
    read deliberately bypasses RLS so the assertion is about whether the row was
    actually persisted, not about read visibility.
    """
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id::text, organisation_id::text, event_type, "
                "account_id::text, resource_id::text, payload_json "
                "FROM audit_events WHERE organisation_id = :oid AND event_type = :et "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
            ),
            {"oid": str(org_id), "et": event_type},
        )
        row = result.mappings().first()
    if row is None:
        return None
    row_dict = dict(row)
    # Extract outcome from payload_json
    if row_dict.get("payload_json") and isinstance(row_dict["payload_json"], dict):
        row_dict["outcome"] = row_dict["payload_json"].get("outcome")
    return row_dict


@pytest_asyncio.fixture
async def rls_session(app_engine: AsyncEngine) -> AsyncSession:
    """Session whose connections run as a NOBYPASSRLS role, so RLS applies.

    Mirrors production where the app connects as ``modulo_app`` (NOBYPASSRLS).
    """
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()


async def _setup_draft_with_revision(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> tuple[SystemMap, SystemMapRevision]:
    """Create a draft map with a revision that has validation errors (cycle)."""
    # Create draft map
    map_name = f"Publish Audit Test {uuid.uuid4().hex[:8]}"
    async with rls_session.begin():
        await set_rls_org(rls_session, test_org)
        await set_rls_user_context(rls_session, test_user, "admin")
        draft = await _DISCOVERY_REPO.get_or_create_draft(
            rls_session,
            organisation_id=test_org,
            account_id=test_user,
            name=map_name,
            description="draft for publish audit test",
        )

    # Create a revision with a cycle (node A -> node B -> node A)
    content = {
        "nodes": [
            {"node_key": "node-a", "node_type": "pipeline", "display_name": "Node A", "config_json": {}},
            {"node_key": "node-b", "node_type": "pipeline", "display_name": "Node B", "config_json": {}},
        ],
        "relationships": [
            {"source_node_key": "node-a", "target_node_key": "node-b", "relationship_type": "flow", "config_json": {}},
            {"source_node_key": "node-b", "target_node_key": "node-a", "relationship_type": "flow", "config_json": {}},
        ],
        "loop_configs": [],
        "discovery_fingerprint": "publish-audit-test-cycle",
    }
    async with rls_session.begin():
        await set_rls_org(rls_session, test_org)
        await set_rls_user_context(rls_session, test_user, "admin")
        _, revision_id, _ = await _DISCOVERY_REPO.save_discovery_revision(
            rls_session,
            organisation_id=test_org,
            map_id=draft.id,
            content=content,
            updated_by=test_user,
        )

    # Fetch the created revision
    async with rls_session.begin():
        await set_rls_org(rls_session, test_org)
        await set_rls_user_context(rls_session, test_user, "admin")
        result = await rls_session.execute(
            text("SELECT * FROM system_map_revisions WHERE id = :rid"),
            {"rid": str(revision_id)},
        )
        revision_row = result.mappings().first()

    # Build ORM objects
    draft_obj = SystemMap(
        id=draft.id,
        organisation_id=test_org,
        name=draft.name,
        description=draft.description,
        owner_team_id=None,
        visibility="org",
        default_map=False,
        account_id=test_user,
        updated_by=test_user,
    )

    revision_obj = SystemMapRevision(
        id=revision_row["id"],
        organisation_id=test_org,
        system_map_id=draft.id,
        revision_number=revision_row["revision_number"],
        status=revision_row["status"],
        content_json=revision_row["content_json"],
        created_at=revision_row["created_at"],
        updated_at=revision_row["updated_at"],
    )

    return draft_obj, revision_obj


async def _setup_draft_with_valid_revision(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> tuple[SystemMap, SystemMapRevision]:
    """Create a draft map with a valid revision (no cycles, no missing bindings)."""
    map_name = f"Publish Valid Test {uuid.uuid4().hex[:8]}"
    async with rls_session.begin():
        await set_rls_org(rls_session, test_org)
        await set_rls_user_context(rls_session, test_user, "admin")
        draft = await _DISCOVERY_REPO.get_or_create_draft(
            rls_session,
            organisation_id=test_org,
            account_id=test_user,
            name=map_name,
            description="draft for valid publish audit test",
        )

    # Create a valid revision (simple linear flow, no cycles)
    content = {
        "nodes": [
            {"node_key": "start", "node_type": "pipeline", "display_name": "Start", "config_json": {}},
            {"node_key": "end", "node_type": "pipeline", "display_name": "End", "config_json": {}},
        ],
        "relationships": [
            {"source_node_key": "start", "target_node_key": "end", "relationship_type": "flow", "config_json": {}},
        ],
        "loop_configs": [],
        "discovery_fingerprint": "publish-audit-test-valid",
    }
    async with rls_session.begin():
        await set_rls_org(rls_session, test_org)
        await set_rls_user_context(rls_session, test_user, "admin")
        _, revision_id, _ = await _DISCOVERY_REPO.save_discovery_revision(
            rls_session,
            organisation_id=test_org,
            map_id=draft.id,
            content=content,
            updated_by=test_user,
        )

    async with rls_session.begin():
        await set_rls_org(rls_session, test_org)
        await set_rls_user_context(rls_session, test_user, "admin")
        result = await rls_session.execute(
            text("SELECT * FROM system_map_revisions WHERE id = :rid"),
            {"rid": str(revision_id)},
        )
        revision_row = result.mappings().first()

    draft_obj = SystemMap(
        id=draft.id,
        organisation_id=test_org,
        name=draft.name,
        description=draft.description,
        owner_team_id=None,
        visibility="org",
        default_map=False,
        account_id=test_user,
        updated_by=test_user,
    )

    revision_obj = SystemMapRevision(
        id=revision_row["id"],
        organisation_id=test_org,
        system_map_id=draft.id,
        revision_number=revision_row["revision_number"],
        status=revision_row["status"],
        content_json=revision_row["content_json"],
        created_at=revision_row["created_at"],
        updated_at=revision_row["updated_at"],
    )

    return draft_obj, revision_obj


async def _append_audit_event_direct(
    app_engine: AsyncEngine,
    principal: TenantPrincipal,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> None:
    """Append an audit event directly using the test engine (bypassing audit_session).

    This mirrors what append_audit_event_isolated does but uses the test engine
    directly so the audit row lands in the test database.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        from modulo.db.rls import set_rls_org, set_rls_user_context

        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        from modulo.core.audit_logger import append_audit_event

        await append_audit_event(
            session,
            org_id=principal.organisation_id,
            event_type=event_type,
            actor_user_id=principal.account_id,
            resource_type=resource_type,
            resource_id=resource_id,
            payload_json=payload,
        )


async def test_audit_append_error_outcome_works_under_rls(
    rls_session: AsyncSession,
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Verify the audit append path works under strict RLS for outcome='error'.

    This mirrors what append_audit_event_isolated does when the handler raises
    an exception (refused publish), using the test engine directly.
    """
    principal = _make_principal(test_org, test_user)

    await _append_audit_event_direct(
        app_engine,
        principal,
        event_type="system_map_revision_published",
        resource_type="system_map_revision",
        resource_id=uuid.uuid4(),
        payload={
            "http_method": "POST",
            "path": "/api/v1/system-maps/test/revisions/test/publish",
            "outcome": "error",
        },
    )

    # Verify the row was actually persisted (read via superuser connection bypassing RLS)
    audit_row = await _fetch_audit_event(db_engine, test_org, "system_map_revision_published")
    assert audit_row is not None, "system_map_revision_published audit row missing for error outcome"
    assert audit_row["outcome"] == "error", f"Expected outcome='error', got '{audit_row['outcome']}'"
    assert audit_row["account_id"] == str(test_user)


async def test_audit_append_success_outcome_works_under_rls(
    rls_session: AsyncSession,
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Verify the audit append path works under strict RLS for outcome='success'.

    This mirrors what append_audit_event_isolated does when the handler returns
    normally (successful publish), using the test engine directly.
    """
    principal = _make_principal(test_org, test_user)

    await _append_audit_event_direct(
        app_engine,
        principal,
        event_type="system_map_revision_published",
        resource_type="system_map_revision",
        resource_id=uuid.uuid4(),
        payload={
            "http_method": "POST",
            "path": "/api/v1/system-maps/test/revisions/test/publish",
            "outcome": "success",
        },
    )

    # Verify the row was actually persisted (read via superuser connection bypassing RLS)
    audit_row = await _fetch_audit_event(db_engine, test_org, "system_map_revision_published")
    assert audit_row is not None, "system_map_revision_published audit row missing for success outcome"
    assert audit_row["outcome"] == "success", f"Expected outcome='success', got '{audit_row['outcome']}'"
    assert audit_row["account_id"] == str(test_user)


async def test_audit_append_without_org_context_is_rejected_under_rls(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A fresh-transaction audit append with no org context is rejected by RLS.

    Proves the premise of the fix: the STRICT-RLS ``audit_events`` INSERT fails
    when ``app.organisation_id`` is empty, which is exactly why the route must
    re-establish RLS context inside its fresh audit transaction.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from modulo.core.audit_logger import append_audit_event

    with pytest.raises(SQLAlchemyError):
        async with rls_session.begin():
            await append_audit_event(
                rls_session,
                org_id=test_org,
                event_type="system_map_revision_published",
                actor_user_id=test_user,
                resource_type="system_map_revision",
                resource_id=uuid.uuid4(),
                payload_json={"outcome": "success"},
            )

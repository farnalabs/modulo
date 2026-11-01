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
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.audit_coverage import audit_session
from modulo.core.feature_flags import PlanContext
from modulo.core.system_map_persistence import SystemMapDiscoveryRepository
from modulo.db.models.system_map import SystemMap, SystemMapRevision
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

pytestmark = pytest.mark.integration

_DISCOVERY_REPO = SystemMapDiscoveryRepository()


class _AllFeatures:
    """Plan-context stub that reports every feature as enabled (enterprise tier)."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list[Any]:
        return []

    def list_all_features(self) -> list[Any]:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


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
    # row is guaranteed to be RowMapping here due to the None check above
    row_mapping: dict[str, Any] = dict(row)
    # Extract outcome from payload_json
    if row_mapping.get("payload_json") and isinstance(row_mapping["payload_json"], dict):
        row_mapping["outcome"] = row_mapping["payload_json"].get("outcome")
    return row_mapping


@pytest_asyncio.fixture
async def rls_session(app_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
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
        revision_row_raw = result.mappings().first()
        if revision_row_raw is None:
            raise RuntimeError("Revision not found after creation")
        revision_row: dict[str, Any] = dict(revision_row_raw)

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
        revision_row_raw = result.mappings().first()
        if revision_row_raw is None:
            raise RuntimeError("Revision not found after creation")
        revision_row: dict[str, Any] = dict(revision_row_raw)

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


@pytest_asyncio.fixture
async def integration_client_with_audit(
    db_url: str,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client with audit_session overridden to use a fresh RLS session per request,
    and auth overridden to return the test principal.
    """
    from modulo.api.dependencies import _get_engine

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
        modulo_dev_mode=True,
    )

    test_principal = TenantPrincipal(
        username="integration-user",
        organisation_id=test_org,
        account_id=test_user,
        org_role="admin",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)
        async with factory() as session:
            yield session

    async def override_audit_session() -> AsyncGenerator[AsyncSession, None]:
        # Create a fresh session for each audit write, mirroring the real audit_session
        factory = async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)
        async with factory() as session:
            yield session

    async def _all_features_ctx() -> PlanContext:
        return _AllFeatures()

    async def override_auth() -> TenantPrincipal:
        return test_principal

    async def override_current_user() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="integration-user",
            organisation_id=test_org,
            account_id=test_user,
            org_role="admin",
            is_system_admin=False,
            via_api_key=False,
            client_kind="browser",
        )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = _all_features_ctx
    app.dependency_overrides[audit_session] = override_audit_session
    app.dependency_overrides[get_current_tenant_user_or_api_key] = override_auth
    app.dependency_overrides[get_current_user] = override_current_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


async def test_refused_publish_audit_outcome_error(
    integration_client_with_audit: AsyncClient,
    rls_session: AsyncSession,
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
    db_url: str,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Refused publish (invalid revision) records outcome="error" in audit_events.

    This drives the real publish endpoint via HTTP with a revision that fails
    validation (cycle), so the handler raises PublishValidationErrorHTTP and the
    audited() dependency records outcome="error".
    """
    # Set up a draft with an invalid revision (cycle)
    draft, revision = await _setup_draft_with_revision(rls_session, db_engine, test_org, test_user)

    # Call the real publish endpoint via HTTP
    response = await integration_client_with_audit.post(
        f"/api/v1/system-maps/{draft.id}/revisions/{revision.id}/publish",
    )

    # The endpoint returns 200 with validation errors in the body (wire contract)
    assert response.status_code == 200
    data = response.json()
    assert "validation_errors" in data
    assert len(data["validation_errors"]) > 0

    # Verify the audit row was persisted with outcome="error"
    audit_row = await _fetch_audit_event(db_engine, test_org, "system_map_revision_published")
    assert audit_row is not None, "system_map_revision_published audit row missing for error outcome"
    assert audit_row["outcome"] == "error", f"Expected outcome='error', got '{audit_row['outcome']}'"
    assert audit_row["account_id"] == str(test_user)


async def test_successful_publish_audit_outcome_success(
    integration_client_with_audit: AsyncClient,
    rls_session: AsyncSession,
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
    db_url: str,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Successful publish (valid revision) records outcome="success" in audit_events.

    This drives the real publish endpoint via HTTP with a valid revision, so the
    handler returns normally and the audited() dependency records outcome="success".
    """
    # Set up a draft with a valid revision
    draft, revision = await _setup_draft_with_valid_revision(rls_session, db_engine, test_org, test_user)

    # Call the real publish endpoint via HTTP
    response = await integration_client_with_audit.post(
        f"/api/v1/system-maps/{draft.id}/revisions/{revision.id}/publish",
    )

    # The endpoint returns 200 with the published revision
    assert response.status_code == 200
    data = response.json()
    assert "validation_errors" in data
    assert data["validation_errors"] == []
    assert data["revision"]["id"] == str(revision.id)
    assert data["revision"]["status"] == "published"

    # Verify the audit row was persisted with outcome="success"
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

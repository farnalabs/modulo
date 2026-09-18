"""Integration tests: PRD §8.12 api-key audit events survive real Postgres RLS.

The ``api_key_created`` / ``api_key_revoked`` audit appends run in a FRESH
transaction after the key operation has already committed. ``SET LOCAL`` RLS
context (``set_config(..., is_local=true)``) reverts on COMMIT, so without
re-establishing ``set_rls_org`` + ``set_rls_user_context`` inside that fresh
transaction the STRICT-RLS ``audit_events`` INSERT is rejected by the WITH
CHECK policy and the event is silently dropped (the route swallows the error).

These tests drive the real endpoint functions against a NOBYPASSRLS role — the
production ``modulo_app`` scenario — and assert the rows actually land in
``audit_events``. The unit tests mock ``append_audit_event`` / ``set_rls_org``
so they cannot catch this rejection; only a real Postgres path can.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.routes.api_keys import ApiKeyCreate, create_api_key_endpoint, revoke_api_key_endpoint
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings

pytestmark = pytest.mark.integration


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
    return dict(row) if row is not None else None


async def test_api_key_created_and_revoked_rows_land_in_audit_events(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    db_url: str,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Key create/revoke through the real endpoints persist their audit rows.

    Regression: before the fix the fresh audit transaction ran with an empty
    ``app.organisation_id`` (SET LOCAL reverted on COMMIT), so the STRICT-RLS
    INSERT was rejected and the events were dropped. The row assertions below
    fail on the pre-fix code.
    """
    principal = _make_principal(test_org, test_user)

    created = await create_api_key_endpoint(
        req=ApiKeyCreate(name="RLS audit key", role="operator"),
        session=rls_session,
        principal=principal,
        settings=_make_settings(db_url),
    )

    created_row = await _fetch_audit_event(db_engine, test_org, "api_key_created")
    assert created_row is not None, "api_key_created audit row missing after create"
    assert created_row["resource_id"] == str(created.id)
    assert created_row["account_id"] == str(test_user)
    assert created_row["payload_json"]["name"] == "RLS audit key"
    assert created_row["payload_json"]["role"] == "operator"

    revoked = await revoke_api_key_endpoint(
        key_id=created.id,
        session=rls_session,
        principal=principal,
    )
    assert revoked.revoked is True

    revoked_row = await _fetch_audit_event(db_engine, test_org, "api_key_revoked")
    assert revoked_row is not None, "api_key_revoked audit row missing after revoke"
    assert revoked_row["resource_id"] == str(created.id)
    assert revoked_row["account_id"] == str(test_user)
    assert revoked_row["payload_json"]["revoked_by"] == str(test_user)


async def test_audit_append_without_org_context_is_rejected_under_rls(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A fresh-transaction audit append with no org context is rejected by RLS.

    Proves the premise of the fix: the STRICT-RLS ``audit_events`` INSERT fails
    when ``app.organisation_id`` is empty, which is exactly why the route must
    re-establish RLS context inside its fresh audit transaction. Without this
    guarantee the positive test above could silently pass on a misconfigured
    Postgres where RLS was not actually enforced.
    """
    from modulo.core.audit_logger import append_audit_event

    with pytest.raises(SQLAlchemyError):
        async with rls_session.begin():
            await append_audit_event(
                rls_session,
                org_id=test_org,
                event_type="api_key_created",
                actor_user_id=test_user,
                resource_type="api_key",
                resource_id=uuid.uuid4(),
                payload_json={"name": "no-context"},
            )

"""Integration tests for Schema and SchemaVersion CRUD.

RLS is set to test_org; all ORM changes are rolled back after each test.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.schema import (
    SchemaDeletionProtectedError,
    create_schema,
    create_schema_version,
    delete_schema,
    get_schema,
    get_schema_version,
    list_schema_versions,
    list_schemas,
    update_schema,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def test_create_schema(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    s = await create_schema(rls_session, org_id=test_org, name="MySchema", created_by=test_user)
    assert s.id is not None
    assert s.name == "MySchema"
    assert s.organisation_id == test_org


async def test_get_schema_returns_existing(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID,
) -> None:
    s = await create_schema(rls_session, org_id=test_org, name="FetchSchema", created_by=test_user)
    fetched = await get_schema(rls_session, s.id)
    assert fetched is not None
    assert fetched.id == s.id


async def test_get_schema_returns_none_for_unknown(rls_session: AsyncSession) -> None:
    assert await get_schema(rls_session, uuid.uuid4()) is None


async def test_list_schemas_pagination(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    for i in range(3):
        await create_schema(
            rls_session,
            org_id=test_org,
            name=f"ListSchema-{uuid.uuid4().hex[:6]}",
            created_by=test_user,
        )
    page1 = await list_schemas(rls_session, page=1, page_size=2)
    assert page1.total >= 3
    assert len(page1.items) == 2


async def test_update_schema(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    s = await create_schema(rls_session, org_id=test_org, name="OldSchemaName", created_by=test_user)
    updated = await update_schema(rls_session, s.id, {"description": "Now has description"})
    assert updated is not None
    assert updated.description == "Now has description"


async def test_update_schema_unknown_returns_none(rls_session: AsyncSession) -> None:
    assert await update_schema(rls_session, uuid.uuid4(), {"name": "x"}) is None


async def test_delete_schema(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    s = await create_schema(rls_session, org_id=test_org, name="DeleteSchema", created_by=test_user)
    assert await delete_schema(rls_session, s.id) is True
    assert await get_schema(rls_session, s.id) is None


async def test_delete_schema_unknown_returns_false(rls_session: AsyncSession) -> None:
    assert await delete_schema(rls_session, uuid.uuid4()) is False


async def test_delete_schema_protected_by_agent_reference(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID,
) -> None:
    """delete_schema must raise SchemaDeletionProtectedError when an agent references it."""
    from modulo.db.crud.agent import create_agent
    from modulo.db.crud.model_backend import create_model_backend

    schema = await create_schema(
        rls_session,
        org_id=test_org,
        name=f"ProtectedSchema-{uuid.uuid4().hex[:6]}",
        created_by=test_user,
    )
    sv = await create_schema_version(
        rls_session,
        org_id=test_org,
        schema_id=schema.id,
        version="1.0",
        version_number=1,
        definition_json={"type": "object"},
        created_by=test_user,
    )
    mb = await create_model_backend(
        rls_session,
        org_id=test_org,
        name=f"MB-{uuid.uuid4().hex[:6]}",
        display_name="Test Backend",
        provider="anthropic",
        model_id="stub-model",
        credentials_ciphertext=b"fake-cipher",
        created_by=test_user,
    )
    await create_agent(
        rls_session,
        org_id=test_org,
        name="AgentReferencing",
        created_by=test_user,
        input_schema_id=schema.id,
        input_schema_version=sv.version,
        output_schema_id=schema.id,
        output_schema_version=sv.version,
        prompt_template="Say hi",
        model_backend_id=mb.id,
    )

    with pytest.raises(SchemaDeletionProtectedError) as exc_info:
        await delete_schema(rls_session, schema.id)
    assert exc_info.value.schema_id == schema.id


# ---------------------------------------------------------------------------
# Schema deprecation
# ---------------------------------------------------------------------------


async def test_deprecate_schema(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    s = await create_schema(rls_session, org_id=test_org, name="DeprecateSchema", created_by=test_user)
    assert s.deprecated is False
    assert s.deprecated_at is None

    deprecated = await deprecate_schema(rls_session, s.id)
    assert deprecated is not None
    assert deprecated.deprecated is True
    assert deprecated.deprecated_at is not None


async def test_deprecate_schema_twice_is_idempotent(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID,
) -> None:
    s = await create_schema(rls_session, org_id=test_org, name="DeprecateTwice", created_by=test_user)
    first = await deprecate_schema(rls_session, s.id)
    assert first is not None
    assert first.deprecated is True

    second = await deprecate_schema(rls_session, s.id)
    assert second is not None
    assert second.deprecated is True
    assert second.deprecated_at is not None


# ---------------------------------------------------------------------------
# SchemaVersion
# ---------------------------------------------------------------------------


async def test_create_schema_version(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    s = await create_schema(rls_session, org_id=test_org, name=f"SVSchema-{uuid.uuid4().hex[:6]}", created_by=test_user)
    sv = await create_schema_version(
        rls_session,
        org_id=test_org,
        schema_id=s.id,
        version="1.0",
        version_number=1,
        definition_json={"type": "object", "properties": {}},
        created_by=test_user,
    )
    assert sv.id is not None
    assert sv.schema_id == s.id
    assert sv.version == "1.0"


async def test_get_schema_version_returns_existing(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID,
) -> None:
    s = await create_schema(rls_session, org_id=test_org, name=f"SVFetch-{uuid.uuid4().hex[:6]}", created_by=test_user)
    await create_schema_version(
        rls_session,
        org_id=test_org,
        schema_id=s.id,
        version="2.0",
        version_number=2,
        definition_json={"type": "object"},
        created_by=test_user,
    )
    fetched = await get_schema_version(rls_session, s.id, "2.0")
    assert fetched is not None
    assert fetched.version == "2.0"


async def test_get_schema_version_returns_none_for_unknown(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID,
) -> None:
    s = await create_schema(rls_session, org_id=test_org, name=f"SVMiss-{uuid.uuid4().hex[:6]}", created_by=test_user)
    assert await get_schema_version(rls_session, s.id, "99.0") is None


async def test_list_schema_versions(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    s = await create_schema(rls_session, org_id=test_org, name=f"SVList-{uuid.uuid4().hex[:6]}", created_by=test_user)
    for i in range(1, 4):
        await create_schema_version(
            rls_session,
            org_id=test_org,
            schema_id=s.id,
            version=f"{i}.0",
            version_number=i,
            definition_json={"type": "object"},
            created_by=test_user,
        )
    result = await list_schema_versions(rls_session, s.id, page=1, page_size=10)
    assert result.total == 3
    assert len(result.items) == 3

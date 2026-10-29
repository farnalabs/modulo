"""Real database coverage for the database CRUD batch (FAR-366)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.parameter_schema import (
    create_schema as create_parameter_schema,
)
from modulo.db.crud.parameter_schema import (
    delete_schema as delete_parameter_schema,
)
from modulo.db.crud.parameter_schema import (
    get_schema_references,
    soft_delete_schema,
)
from modulo.db.crud.parameter_schema import (
    list_schemas as list_parameter_schemas,
)
from modulo.db.crud.parameter_schema import (
    restore_schema as restore_parameter_schema,
)
from modulo.db.crud.parameter_schema import (
    update_schema as update_parameter_schema,
)
from modulo.db.crud.parameter_set import create_set
from modulo.db.crud.pipeline import create_pipeline
from modulo.db.crud.pipeline_folder import (
    create_folder as create_pipeline_folder,
)
from modulo.db.crud.pipeline_folder import (
    delete_folder as delete_pipeline_folder,
)
from modulo.db.crud.pipeline_folder import (
    get_folder as get_pipeline_folder,
)
from modulo.db.crud.pipeline_folder import (
    list_folders as list_pipeline_folders,
)
from modulo.db.crud.pipeline_folder import (
    move_pipeline_to_folder,
)
from modulo.db.crud.pipeline_folder import (
    update_folder as update_pipeline_folder,
)
from modulo.db.crud.schema import create_schema
from modulo.db.crud.schema_folder import (
    create_folder as create_schema_folder,
)
from modulo.db.crud.schema_folder import (
    delete_folder as delete_schema_folder,
)
from modulo.db.crud.schema_folder import (
    get_folder as get_schema_folder,
)
from modulo.db.crud.schema_folder import (
    list_folders as list_schema_folders,
)
from modulo.db.crud.schema_folder import (
    move_schema_to_folder,
)
from modulo.db.crud.schema_folder import (
    update_folder as update_schema_folder,
)
from modulo.db.crud.tier_catalog import get_feature_flag, list_feature_flags, list_tiers

pytestmark = pytest.mark.integration


async def test_pipeline_folder_crud_and_tenant_safe_moves(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    parent = await create_pipeline_folder(rls_session, org_id=test_org, name="Parent", account_id=test_user)
    child = await create_pipeline_folder(
        rls_session, org_id=test_org, name="Child", account_id=test_user, parent_id=parent.id
    )
    assert [folder.name for folder in await list_pipeline_folders(rls_session)] == ["Child", "Parent"]
    assert await get_pipeline_folder(rls_session, child.id) is child
    with pytest.raises(ValueError, match="descendants"):
        await update_pipeline_folder(rls_session, parent.id, {"parent_id": child.id})
    updated = await update_pipeline_folder(rls_session, child.id, {"name": "Renamed", "parent_id": None})
    assert updated is child
    assert child.name == "Renamed"
    assert child.parent_id is None

    pipeline = await create_pipeline(rls_session, org_id=test_org, name="Pipeline", account_id=test_user)
    moved = await move_pipeline_to_folder(rls_session, pipeline.id, parent.id)
    assert moved is pipeline
    assert pipeline.folder_id == parent.id
    with pytest.raises(ValueError, match="Folder not found"):
        await move_pipeline_to_folder(rls_session, pipeline.id, uuid.uuid4())
    assert await move_pipeline_to_folder(rls_session, uuid.uuid4(), None) is None

    with pytest.raises(ValueError, match="own parent"):
        await update_pipeline_folder(rls_session, parent.id, {"parent_id": parent.id})
    assert await update_pipeline_folder(rls_session, parent.id, {"parent_id": child.id}) is parent
    assert await update_pipeline_folder(rls_session, uuid.uuid4(), {"name": "missing"}) is None

    assert await delete_pipeline_folder(rls_session, parent.id) is True
    assert pipeline.folder_id is None
    assert child.parent_id is None
    assert await delete_pipeline_folder(rls_session, parent.id) is False


async def test_schema_folder_crud_and_org_ownership_guard(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    parent = await create_schema_folder(rls_session, org_id=test_org, name="Schemas", account_id=test_user)
    child = await create_schema_folder(
        rls_session, org_id=test_org, name="Child", account_id=test_user, parent_id=parent.id
    )
    assert len(await list_schema_folders(rls_session)) == 2
    assert await get_schema_folder(rls_session, child.id) is child
    assert await update_schema_folder(rls_session, child.id, {"name": "Updated", "parent_id": None}) is child
    assert child.name == "Updated"

    schema = await create_schema(rls_session, org_id=test_org, name="Folder schema", account_id=test_user)
    assert await move_schema_to_folder(rls_session, schema.id, parent.id, test_org) is schema
    assert schema.folder_id == parent.id
    assert await move_schema_to_folder(rls_session, uuid.uuid4(), None) is None
    with pytest.raises(ValueError, match="Folder not found"):
        await move_schema_to_folder(rls_session, schema.id, uuid.uuid4(), test_org)
    with pytest.raises(ValueError, match="own parent"):
        await update_schema_folder(rls_session, parent.id, {"parent_id": parent.id})
    assert await update_schema_folder(rls_session, parent.id, {"parent_id": child.id}) is parent
    assert await update_schema_folder(rls_session, uuid.uuid4(), {"name": "missing"}) is None

    assert await delete_schema_folder(rls_session, parent.id) is True
    assert schema.folder_id is None
    assert child.parent_id is None
    assert await delete_schema_folder(rls_session, parent.id) is False


async def test_parameter_schema_crud_lifecycle_and_references(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    schema = await create_parameter_schema(
        rls_session,
        org_id=test_org,
        name="Parameters",
        description="Initial",
        parameters=[{"name": "temperature", "type": "number", "default": 0.5}],
        account_id=test_user,
    )
    page = await list_parameter_schemas(rls_session, org_id=test_org, limit=10)
    assert schema in page.items
    assert page.total >= 1
    assert await get_schema_references(rls_session, schema.id) == {"agents": [], "sets": []}

    updated = await update_parameter_schema(
        rls_session,
        schema.id,
        name="Parameters v2",
        description="Updated",
        parameters=[{"name": "temperature", "type": "number"}],
        version=1,
    )
    assert updated is schema
    assert schema.version == 2
    assert schema.description == "Updated"
    assert await update_parameter_schema(rls_session, schema.id, version=1) is None

    parameter_set = await create_set(
        rls_session,
        parameter_schema_id=schema.id,
        org_id=test_org,
        name="Defaults",
        description=None,
        values={"temperature": 0.2},
        account_id=test_user,
        schema_version=schema.version,
    )
    refs = await get_schema_references(rls_session, schema.id)
    assert refs["sets"] == [parameter_set.id]
    assert await delete_parameter_schema(rls_session, schema.id) is False

    assert await soft_delete_schema(rls_session, schema.id) is schema
    assert schema.deleted_at is not None
    assert await soft_delete_schema(rls_session, schema.id) is None
    assert await restore_parameter_schema(rls_session, schema.id) is schema
    assert schema.deleted_at is None
    assert await delete_parameter_schema(rls_session, uuid.uuid4()) is False


async def test_tier_catalog_reads_real_seeded_catalog(rls_session: AsyncSession) -> None:
    tiers = await list_tiers(rls_session)
    flags = await list_feature_flags(rls_session)
    assert tiers == sorted(tiers, key=lambda tier: tier["rank"])
    assert flags == sorted(flags, key=lambda flag: flag["name"])
    if flags:
        found = await get_feature_flag(flags[0]["name"], rls_session)
        assert found == flags[0]
    else:
        assert await get_feature_flag("definitely-not-a-real-feature", rls_session) is None


def test_evidence_repository_placeholder_imports() -> None:
    import modulo.db.repositories.evidence as evidence_repository

    assert evidence_repository.__doc__ is not None

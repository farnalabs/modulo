"""Unit coverage for database CRUD functions in FAR-366 batch 8.

The tests invoke the production CRUD functions with real model payloads and a
session carrying the tenant context used by the non-Postgres RLS adapter.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud import parameter_schema, pipeline_folder, schema_folder, tier_catalog
from modulo.db.crud.base import PageResult
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.pipeline_folder import PipelineFolder
from modulo.db.models.schema import SchemaFolder
from modulo.db.models.tier_catalog import FeatureFlagCatalog, TierCatalog

ORG = uuid.uuid4()
ACCOUNT = uuid.uuid4()


def result(*, one=None, scalar=None, rows=None) -> MagicMock:
    value = MagicMock()
    value.scalar_one_or_none.return_value = one
    value.scalar_one.return_value = scalar
    scalar_rows = rows or []
    value.scalars.return_value.all.return_value = scalar_rows
    value.scalars.return_value.__iter__.return_value = iter(scalar_rows)
    return value


def session(*results: MagicMock) -> MagicMock:
    db = MagicMock()
    db.info = {"org_id": str(ORG)}
    db.flush = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.delete = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_pipeline_folder_crud_all_paths() -> None:
    parent_id = uuid.uuid4()
    folder = await pipeline_folder.create_folder(
        session(result(one=parent_id), result(one=None)), org_id=ORG, name="x", account_id=ACCOUNT, parent_id=parent_id
    )
    assert isinstance(folder, PipelineFolder)

    existing = PipelineFolder(organisation_id=ORG, name="old", account_id=ACCOUNT)
    folder_db = session(result(one=existing))
    assert await pipeline_folder.get_folder(folder_db, existing.id) is existing
    assert (
        await pipeline_folder.update_folder(
            session(result(one=existing)), existing.id, {"name": "new", "parent_id": None}
        )
        is existing
    )
    assert existing.name == "new"
    assert await pipeline_folder.update_folder(session(result(one=None)), uuid.uuid4(), {}) is None

    pipeline = MagicMock(id=uuid.uuid4(), folder_id=None)
    moved = session(result(one=pipeline), result(one=existing))
    assert await pipeline_folder.move_pipeline_to_folder(moved, pipeline.id, existing.id) is pipeline
    assert pipeline.folder_id == existing.id
    with pytest.raises(ValueError, match="Folder not found"):
        await pipeline_folder.move_pipeline_to_folder(
            session(result(one=pipeline), result(one=None)), pipeline.id, uuid.uuid4()
        )
    assert await pipeline_folder.move_pipeline_to_folder(session(result(one=None)), uuid.uuid4(), None) is None

    listed = session(result(rows=[existing]))
    assert await pipeline_folder.list_folders(listed) == [existing]
    assert await pipeline_folder.delete_folder(session(result(one=None)), uuid.uuid4()) is False
    delete_db = session(result(one=existing), result(rows=[]), result(rows=[]))
    assert await pipeline_folder.delete_folder(delete_db, existing.id) is True
    delete_db.delete.assert_awaited_once_with(existing)


@pytest.mark.asyncio
async def test_schema_folder_crud_all_paths() -> None:
    parent_id = uuid.uuid4()
    folder = await schema_folder.create_folder(
        session(result(one=parent_id), result(one=None)), org_id=ORG, name="x", account_id=ACCOUNT, parent_id=parent_id
    )
    assert isinstance(folder, SchemaFolder)

    existing = SchemaFolder(organisation_id=ORG, name="old", account_id=ACCOUNT)
    assert await schema_folder.get_folder(session(result(one=existing)), existing.id) is existing
    assert (
        await schema_folder.update_folder(
            session(result(one=existing)), existing.id, {"name": "new", "parent_id": None}
        )
        is existing
    )
    assert await schema_folder.update_folder(session(result(one=None)), uuid.uuid4(), {}) is None

    schema = MagicMock(id=uuid.uuid4(), folder_id=None)
    assert (
        await schema_folder.move_schema_to_folder(
            session(result(one=schema), result(one=existing)), schema.id, existing.id, ORG
        )
        is schema
    )
    assert schema.folder_id == existing.id
    with pytest.raises(ValueError, match="Folder not found"):
        await schema_folder.move_schema_to_folder(
            session(result(one=schema), result(one=None)), schema.id, uuid.uuid4(), ORG
        )
    foreign_folder = SimpleNamespace(organisation_id=uuid.uuid4())
    with pytest.raises(ValueError, match="Folder not found"):
        await schema_folder.move_schema_to_folder(
            session(result(one=schema), result(one=foreign_folder)), schema.id, uuid.uuid4(), ORG
        )
    assert await schema_folder.move_schema_to_folder(session(result(one=None)), uuid.uuid4(), None) is None

    assert await schema_folder.list_folders(session(result(rows=[existing]))) == [existing]
    assert await schema_folder.delete_folder(session(result(one=None)), uuid.uuid4()) is False
    delete_db = session(result(one=existing), result(rows=[]), result(rows=[]))
    assert await schema_folder.delete_folder(delete_db, existing.id) is True


@pytest.mark.asyncio
async def test_parameter_schema_crud_all_paths() -> None:
    schema = await parameter_schema.create_schema(
        session(), org_id=ORG, name="schema", description="d", parameters=[{"name": "x"}], account_id=ACCOUNT
    )
    assert isinstance(schema, ParameterSchema)
    assert await parameter_schema.get_schema(session(result(one=schema)), schema.id) is schema

    page_db = session(result(scalar=2), result(rows=[schema, schema]))
    page = await parameter_schema.list_schemas(page_db, org_id=ORG, limit=1)
    assert isinstance(page, PageResult)
    assert page.total == 2
    assert page.has_more is True

    cursor_page = MagicMock(items=[schema], next_cursor="next", has_more=True)
    with patch("modulo.db.crud.parameter_schema.CursorPaginator") as paginator:
        paginator.return_value.paginate = AsyncMock(return_value=cursor_page)
        page = await parameter_schema.list_schemas(session(result(scalar=1)), org_id=ORG, cursor="old", limit=1)
    assert page.next_cursor == "next"
    assert page.has_more is True

    current = ParameterSchema(organisation_id=ORG, name="old", account_id=ACCOUNT, parameters=[])
    current.version = 1
    with patch("modulo.db.crud.row_lock.set_mutation_row_lock_timeout", new=AsyncMock()) as lock:
        updated = await parameter_schema.update_schema(
            session(result(one=current)),
            current.id,
            name="new",
            description="desc",
            parameters=[{"name": "x"}],
            version=1,
        )
    lock.assert_awaited_once()
    assert updated is current
    assert current.version == 2
    with patch("modulo.db.crud.row_lock.set_mutation_row_lock_timeout", new=AsyncMock()):
        assert await parameter_schema.update_schema(session(result(one=None)), uuid.uuid4(), version=1) is None

    active = ParameterSchema(organisation_id=ORG, name="active", account_id=ACCOUNT, parameters=[])
    deleted = ParameterSchema(organisation_id=ORG, name="deleted", account_id=ACCOUNT, parameters=[])
    assert await parameter_schema.soft_delete_schema(session(result(one=active)), active.id) is active
    assert await parameter_schema.restore_schema(session(result(one=deleted)), deleted.id) is deleted

    agent_id, set_id = uuid.uuid4(), uuid.uuid4()
    refs_db = session(result(rows=[agent_id]), result(rows=[set_id]))
    assert await parameter_schema.get_schema_references(refs_db, schema.id) == {"agents": [agent_id], "sets": [set_id]}
    assert await parameter_schema.delete_schema(session(result(scalar=1)), schema.id) is False
    assert await parameter_schema.delete_schema(session(result(scalar=0), result(scalar=1)), schema.id) is False
    assert (
        await parameter_schema.delete_schema(session(result(scalar=0), result(scalar=0), result(one=None)), schema.id)
        is False
    )
    delete_db = session(result(scalar=0), result(scalar=0), result(one=schema))
    assert await parameter_schema.delete_schema(delete_db, schema.id) is True


@pytest.mark.asyncio
async def test_tier_catalog_reads_payloads_and_missing_flag() -> None:
    tier = TierCatalog(tier_id="community", label="Community", rank=0, requires_license=False, description="Free")
    flag = FeatureFlagCatalog(
        name="views", description="Views", tier_id="community", depends_on=["runs"], is_active=True
    )
    assert await tier_catalog.list_tiers(session(result(rows=[tier]))) == [
        {"tier_id": "community", "label": "Community", "rank": 0, "requires_license": False, "description": "Free"}
    ]
    expected = {
        "name": "views",
        "description": "Views",
        "tier_id": "community",
        "depends_on": ["runs"],
        "is_active": True,
    }
    assert await tier_catalog.get_feature_flag("views", session(result(one=flag))) == expected
    assert await tier_catalog.get_feature_flag("missing", session(result(one=None))) is None
    assert await tier_catalog.list_feature_flags(session(result(rows=[flag]))) == [expected]


def test_evidence_repository_module_is_importable() -> None:
    import modulo.db.repositories.evidence as evidence_repository

    assert evidence_repository.__doc__ is not None

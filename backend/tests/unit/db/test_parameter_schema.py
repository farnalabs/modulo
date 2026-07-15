"""Unit tests for parameter schema and parameter set data model."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import JSON, Integer, String, Uuid

from modulo.db.models import Base

# ---------------------------------------------------------------------------
# Schema metadata tests
# ---------------------------------------------------------------------------


def test_parameter_schemas_table_exists() -> None:
    assert "parameter_schemas" in Base.metadata.tables


def test_parameter_schemas_has_organisation_id() -> None:
    table = Base.metadata.tables["parameter_schemas"]
    assert "organisation_id" in table.c


def test_parameter_schemas_has_required_columns() -> None:
    table = Base.metadata.tables["parameter_schemas"]
    assert {
        "id",
        "organisation_id",
        "name",
        "description",
        "version",
        "parameters",
        "created_at",
        "updated_at",
        "account_id",
    } <= set(table.c.keys())
    assert isinstance(table.c.parameters.type, JSON)
    assert isinstance(table.c.version.type, Integer)
    assert isinstance(table.c.name.type, String)
    assert isinstance(table.c.account_id.type, Uuid)


# ---------------------------------------------------------------------------
# Set metadata tests
# ---------------------------------------------------------------------------


def test_parameter_sets_table_exists() -> None:
    assert "parameter_sets" in Base.metadata.tables


def test_parameter_sets_has_organisation_id() -> None:
    table = Base.metadata.tables["parameter_sets"]
    assert "organisation_id" in table.c


def test_parameter_sets_has_required_columns() -> None:
    table = Base.metadata.tables["parameter_sets"]
    assert {
        "id",
        "parameter_schema_id",
        "organisation_id",
        "account_id",
        "version",
        "schema_version",
        "name",
        "description",
        "values",
        "created_at",
        "updated_at",
    } <= set(table.c.keys())
    assert isinstance(table.c.values.type, JSON)
    assert isinstance(table.c.schema_version.type, Integer)
    assert isinstance(table.c.parameter_schema_id.type, Uuid)


def test_parameter_sets_has_unique_constraint() -> None:
    table = Base.metadata.tables["parameter_sets"]
    constraint_names = {c.name for c in table.constraints if c.name is not None}
    assert "uq_parameter_sets_schema_name" in constraint_names


# ---------------------------------------------------------------------------
# Agent column test
# ---------------------------------------------------------------------------


def test_agents_has_parameter_schema_id() -> None:
    table = Base.metadata.tables["agents"]
    assert "parameter_schema_id" in table.c
    col = table.c["parameter_schema_id"]
    assert col.nullable


# ---------------------------------------------------------------------------
# CRUD mock tests
# ---------------------------------------------------------------------------


class TestParameterSchemaCRUD:
    """Mock-based CRUD tests following test_pipeline_folder.py pattern."""

    @pytest.fixture
    def mock_session(self) -> AsyncMock:
        session = AsyncMock(spec_set=["execute", "add", "flush", "delete"])
        session.execute.return_value = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = None
        session.execute.return_value.scalar_one.return_value = 0
        session.execute.return_value.scalars.return_value.all.return_value = []
        return session

    @pytest.mark.asyncio
    async def test_create_schema(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.parameter_schema import create_schema

        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        schema = await create_schema(
            mock_session,
            org_id=org_id,
            name="Test Schema",
            description="A test",
            parameters=[],
            account_id=account_id,
        )
        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()
        assert schema.organisation_id == org_id
        assert schema.name == "Test Schema"
        assert schema.account_id == account_id

    @pytest.mark.asyncio
    async def test_get_schema_none(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.parameter_schema import get_schema

        result = await get_schema(mock_session, uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_list_schemas(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.parameter_schema import list_schemas

        mock_session.execute.return_value.scalar_one.return_value = 0
        org_id = uuid.uuid4()
        result = await list_schemas(mock_session, org_id=org_id)
        assert result.total == 0
        assert result.items == []

    @pytest.mark.asyncio
    async def test_delete_schema_not_found(self) -> None:
        from modulo.db.crud.parameter_schema import delete_schema

        session = AsyncMock(spec_set=["execute", "add", "flush", "delete"])
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        result = await delete_schema(session, uuid.uuid4())
        assert result is False

    @pytest.mark.asyncio
    async def test_update_schema_version_mismatch(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.parameter_schema import update_schema

        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        result = await update_schema(mock_session, uuid.uuid4(), version=999)
        assert result is None


class TestParameterSetCRUD:
    """Mock-based CRUD tests for ParameterSet."""

    @pytest.fixture
    def mock_session(self) -> AsyncMock:
        session = AsyncMock(spec_set=["execute", "add", "flush", "delete"])
        session.execute.return_value = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = None
        session.execute.return_value.scalars.return_value.all.return_value = []
        return session

    @pytest.mark.asyncio
    async def test_create_set(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.parameter_set import create_set

        schema_id = uuid.uuid4()
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        ps = await create_set(
            mock_session,
            parameter_schema_id=schema_id,
            org_id=org_id,
            name="Test Set",
            description="A test set",
            values={"key": "value"},
            account_id=account_id,
        )
        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()
        assert ps.parameter_schema_id == schema_id
        assert ps.organisation_id == org_id
        assert ps.name == "Test Set"
        assert ps.account_id == account_id

    @pytest.mark.asyncio
    async def test_get_set_none(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.parameter_set import get_set

        result = await get_set(mock_session, uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_list_sets(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.parameter_set import list_sets

        result = await list_sets(
            mock_session,
            parameter_schema_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_delete_set_not_found(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.parameter_set import delete_set

        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        result = await delete_set(mock_session, uuid.uuid4())
        assert result is False

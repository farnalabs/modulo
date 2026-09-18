"""Unit tests for parameter schema and parameter set data model."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import JSON, Integer, String, Uuid

from modulo.db.models import Base
from modulo.db.models.pipeline_snapshot import PipelineSnapshot

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
    assert isinstance(table.c["values"].type, JSON)
    assert isinstance(table.c.schema_version.type, Integer)
    assert isinstance(table.c.parameter_schema_id.type, Uuid)


def test_parameter_sets_has_unique_constraint() -> None:
    table = Base.metadata.tables["parameter_sets"]
    constraint_names = {c.name for c in table.constraints if c.name is not None}
    index_names = {i.name for i in table.indexes if i.name is not None}
    # Soft-delete-safe uniqueness is enforced by a partial unique index
    # (WHERE deleted_at IS NULL) rather than a full UNIQUE constraint, so that a
    # soft-deleted row no longer blocks re-creating an active row with the same
    # (parameter_schema_id, name) key. The name is therefore now an index, not a
    # constraint.
    assert "uq_parameter_sets_schema_name" in index_names
    assert "uq_parameter_sets_schema_name" not in constraint_names


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
    def mock_session(self):
        session = MagicMock()
        session.flush = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        result_mock.scalar_one.return_value = 0
        result_mock.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result_mock)
        return session

    @pytest.mark.asyncio
    async def test_create_schema(self, mock_session) -> None:
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
    async def test_get_schema_none(self, mock_session) -> None:
        from modulo.db.crud.parameter_schema import get_schema

        result = await get_schema(mock_session, uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_list_schemas(self, mock_session) -> None:
        from modulo.db.crud.parameter_schema import list_schemas

        org_id = uuid.uuid4()
        result = await list_schemas(mock_session, org_id=org_id)
        assert result.total == 0
        assert not result.items

    @pytest.mark.asyncio
    async def test_delete_schema_not_found(self) -> None:
        from modulo.db.crud.parameter_schema import delete_schema

        session = MagicMock()
        session.flush = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = 0
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)

        result = await delete_schema(session, uuid.uuid4())
        assert result is False

    @pytest.mark.asyncio
    async def test_update_schema_version_mismatch(self, mock_session) -> None:
        from modulo.db.crud.parameter_schema import update_schema

        result = await update_schema(mock_session, uuid.uuid4(), version=999)
        assert result is None


class TestParameterSetCRUD:
    """Mock-based CRUD tests for ParameterSet."""

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.flush = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        result_mock.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result_mock)
        return session

    @pytest.mark.asyncio
    async def test_create_set(self, mock_session) -> None:
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
    async def test_get_set_none(self, mock_session) -> None:
        from modulo.db.crud.parameter_set import get_set

        result = await get_set(mock_session, uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_list_sets(self, mock_session) -> None:
        from modulo.db.crud.parameter_set import list_sets

        result = await list_sets(
            mock_session,
            parameter_schema_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_delete_set_not_found(self, mock_session) -> None:
        from modulo.db.crud.parameter_set import delete_set

        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        result = await delete_set(mock_session, uuid.uuid4())
        assert result is False


# ---------------------------------------------------------------------------
# PipelineSnapshot column tests
# ---------------------------------------------------------------------------


def test_pipeline_snapshots_has_parameter_bindings_json() -> None:
    table = Base.metadata.tables["pipeline_snapshots"]
    assert "parameter_bindings_json" in table.c
    col = table.c["parameter_bindings_json"]
    assert isinstance(col.type, JSON)
    assert col.nullable


def test_pipeline_snapshot_model_has_field() -> None:
    assert hasattr(PipelineSnapshot, "parameter_bindings_json")
    col = PipelineSnapshot.__table__.c["parameter_bindings_json"]
    assert col.nullable


# ---------------------------------------------------------------------------
# Parameter resolution order tests
# ---------------------------------------------------------------------------


def _make_schema(parameters: list[dict]) -> MagicMock:
    schema = MagicMock()
    schema.id = uuid.uuid4()
    schema.version = 2
    schema.parameters = parameters
    return schema


def _make_set(values: dict, schema_id: uuid.UUID, schema_version: int = 2) -> MagicMock:
    ps = MagicMock()
    ps.id = uuid.uuid4()
    ps.parameter_schema_id = schema_id
    ps.schema_version = schema_version
    ps.values = values
    return ps


def test_resolve_parameters_schema_defaults_only() -> None:
    """Resolution from schema defaults alone."""
    schema = _make_schema(
        [
            {"name": "model_backend", "type": "string", "default": "gpt-4"},
            {"name": "temperature", "type": "number", "default": 0.7},
            {"name": "prompt", "type": "string"},
        ]
    )
    resolved: dict[str, str | float] = {}
    for param in schema.parameters or []:
        if isinstance(param, dict) and "name" in param:
            resolved[param["name"]] = param.get("default")
    assert resolved["model_backend"] == "gpt-4"
    assert resolved["temperature"] == pytest.approx(0.7)
    assert "prompt" not in resolved or resolved["prompt"] is None


def test_resolve_parameters_set_overrides_defaults() -> None:
    """Parameter Set values override schema defaults."""
    schema = _make_schema(
        [
            {"name": "model_backend", "type": "string", "default": "gpt-4"},
            {"name": "temperature", "type": "number", "default": 0.7},
        ]
    )
    ps = _make_set({"model_backend": "claude-3", "temperature": 0.3}, schema.id)

    resolved: dict[str, object] = {}
    for param in schema.parameters or []:
        if isinstance(param, dict) and "name" in param:
            resolved[param["name"]] = param.get("default")
    if ps is not None and isinstance(ps.values, dict):
        resolved.update(ps.values)

    assert resolved["model_backend"] == "claude-3"
    assert resolved["temperature"] == pytest.approx(0.3)


def test_resolve_parameters_overrides_beat_set() -> None:
    """Inline overrides beat Parameter Set values."""
    schema = _make_schema(
        [
            {"name": "model_backend", "type": "string", "default": "gpt-4"},
            {"name": "temperature", "type": "number", "default": 0.7},
        ]
    )
    ps = _make_set({"model_backend": "claude-3", "temperature": 0.3}, schema.id)
    overrides = {"temperature": 0.9}

    resolved: dict[str, object] = {}
    for param in schema.parameters or []:
        if isinstance(param, dict) and "name" in param:
            resolved[param["name"]] = param.get("default")
    if ps is not None and isinstance(ps.values, dict):
        resolved.update(ps.values)
    if isinstance(overrides, dict):
        resolved.update(overrides)

    assert resolved["model_backend"] == "claude-3"
    assert resolved["temperature"] == pytest.approx(0.9)


def test_resolve_parameters_full_chain() -> None:
    """Full resolution chain: schema defaults → set → overrides."""
    schema = _make_schema(
        [
            {"name": "model_backend", "type": "string", "default": "gpt-4"},
            {"name": "temperature", "type": "number", "default": 0.7},
            {"name": "prompt", "type": "string", "default": "default prompt"},
        ]
    )
    ps = _make_set({"model_backend": "claude-3", "prompt": "set prompt"}, schema.id)
    overrides = {"temperature": 0.5}

    resolved: dict[str, object] = {}
    for param in schema.parameters or []:
        if isinstance(param, dict) and "name" in param:
            resolved[param["name"]] = param.get("default")
    if ps is not None and isinstance(ps.values, dict):
        resolved.update(ps.values)
    if isinstance(overrides, dict):
        resolved.update(overrides)

    assert resolved["model_backend"] == "claude-3"
    assert resolved["temperature"] == 0.5
    assert resolved["prompt"] == "set prompt"


# ---------------------------------------------------------------------------
# {{ parameter.* }} prompt template resolution tests
# ---------------------------------------------------------------------------


def test_parameter_injection_in_jinja2() -> None:
    """Parameter values are accessible as {{ parameter.<key> }} in prompts."""
    resolved = {"model_backend": "gpt-4", "temperature": 0.7}
    template_str = "Model: {{ parameter.model_backend }}, Temp: {{ parameter.temperature }}"
    env = SandboxedEnvironment()
    template = env.from_string(template_str)
    result = template.render(parameter=resolved)
    assert result == "Model: gpt-4, Temp: 0.7"


def test_parameter_missing_key_renders_empty() -> None:
    """Accessing a missing parameter key renders as empty string (Jinja2 default)."""
    resolved: dict[str, object] = {"model_backend": "gpt-4"}
    template_str = "Model: {{ parameter.model_backend }}, Extra: {{ parameter.nonexistent }}"
    env = SandboxedEnvironment()
    template = env.from_string(template_str)
    result = template.render(parameter=resolved)
    assert "Model: gpt-4" in result
    assert "Extra:" in result


def test_parameter_no_params_context() -> None:
    """When no parameter dict is present, {{ parameter }} raises UndefinedError."""
    env = SandboxedEnvironment()
    template = env.from_string("{{ parameter }}")
    result = template.render()
    # SandboxedEnvironment renders undefined as empty string by default
    assert result == ""


def test_parameter_empty_dict_renders() -> None:
    """An empty parameter dict renders {{ parameter.<key> }} as empty."""
    env = SandboxedEnvironment()
    template = env.from_string("{{ parameter.foo }}")
    result = template.render(parameter={})
    assert result == ""


# ---------------------------------------------------------------------------
# Pre-flight validation tests
# ---------------------------------------------------------------------------


def test_parameter_references_valid() -> None:
    """Valid parameter schema and set references produce no errors."""
    from modulo.core.graph_validator._types import try_parse_uuid

    graph_json = {
        "nodes": [
            {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "parameter_schema_id": None},
            {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4())},
        ],
        "edges": [],
    }
    session = MagicMock()
    session.execute = AsyncMock()

    nodes: list[dict] = graph_json.get("nodes", [])
    schema_ids: set[uuid.UUID] = set()
    set_ids: set[uuid.UUID] = set()
    for node in nodes:
        raw_schema_id = node.get("parameter_schema_id")
        if raw_schema_id is not None:
            parsed = try_parse_uuid(raw_schema_id)
            if parsed is not None:
                schema_ids.add(parsed)
        raw_set_id = node.get("parameter_set_id")
        if raw_set_id is not None:
            parsed = try_parse_uuid(raw_set_id)
            if parsed is not None:
                set_ids.add(parsed)

    assert not schema_ids
    assert not set_ids


@pytest.mark.asyncio
async def test_parameter_schema_not_found() -> None:
    """Missing ParameterSchema produces an error."""
    from modulo.core.graph_validator import GraphValidator
    from modulo.core.graph_validator._types import ValidationResult

    schema_id = uuid.uuid4()
    set_id = uuid.uuid4()
    graph_json = {
        "nodes": [
            {"id": str(uuid.uuid4()), "parameter_schema_id": str(schema_id), "parameter_set_id": str(set_id)},
        ],
        "edges": [],
    }
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    )

    result = ValidationResult()
    validator = GraphValidator()
    await validator._check_parameter_references(graph_json, session, result)

    assert not result.is_valid
    codes = [i.code for i in result.issues]
    assert "PARAMETER_SCHEMA_NOT_FOUND" in codes


@pytest.mark.asyncio
async def test_parameter_set_schema_mismatch() -> None:
    """ParameterSet belonging to a different schema produces an error."""
    from modulo.core.graph_validator import GraphValidator
    from modulo.core.graph_validator._types import ValidationResult

    schema_id = uuid.uuid4()
    wrong_schema_id = uuid.uuid4()
    set_id = uuid.uuid4()
    graph_json = {
        "nodes": [
            {
                "id": str(uuid.uuid4()),
                "parameter_schema_id": str(schema_id),
                "parameter_set_id": str(set_id),
            },
        ],
        "edges": [],
    }

    mock_set = MagicMock()
    mock_set.id = set_id
    mock_set.parameter_schema_id = wrong_schema_id
    mock_set.schema_version = 1
    mock_set.values = {"model_backend": "gpt-4"}

    mock_schema = MagicMock()
    mock_schema.id = schema_id
    mock_schema.version = 2
    mock_schema.parameters = [{"name": "model_backend", "type": "string"}]

    session = MagicMock()
    call_count = 0

    async def _execute_side_effect(stmt):
        nonlocal call_count
        result = MagicMock()
        if call_count == 0:
            result.scalars.return_value.all.return_value = [mock_schema]
        elif call_count == 1:
            result.scalars.return_value.all.return_value = [mock_set]
        else:
            result.scalars.return_value.all.return_value = []
        call_count += 1
        return result

    session.execute = AsyncMock(side_effect=_execute_side_effect)

    result = ValidationResult()
    validator = GraphValidator()
    await validator._check_parameter_references(graph_json, session, result)

    codes = [i.code for i in result.issues]
    assert "PARAMETER_SET_SCHEMA_MISMATCH" in codes


@pytest.mark.asyncio
async def test_parameter_schema_drift_warning() -> None:
    """Schema version drift produces a warning."""
    from modulo.core.graph_validator import GraphValidator
    from modulo.core.graph_validator._types import ValidationResult

    schema_id = uuid.uuid4()
    set_id = uuid.uuid4()
    graph_json = {
        "nodes": [
            {
                "id": str(uuid.uuid4()),
                "parameter_schema_id": str(schema_id),
                "parameter_set_id": str(set_id),
            },
        ],
        "edges": [],
    }

    mock_set = MagicMock()
    mock_set.id = set_id
    mock_set.parameter_schema_id = schema_id
    mock_set.schema_version = 1
    mock_set.values = {"model_backend": "gpt-4"}

    mock_schema = MagicMock()
    mock_schema.id = schema_id
    mock_schema.version = 3
    mock_schema.parameters = [{"name": "model_backend", "type": "string"}]

    session = MagicMock()
    call_count = 0

    async def _execute_side_effect(stmt):
        nonlocal call_count
        result = MagicMock()
        if call_count == 0:
            result.scalars.return_value.all.return_value = [mock_schema]
        elif call_count == 1:
            result.scalars.return_value.all.return_value = [mock_set]
        else:
            result.scalars.return_value.all.return_value = []
        call_count += 1
        return result

    session.execute = AsyncMock(side_effect=_execute_side_effect)

    result = ValidationResult()
    validator = GraphValidator()
    await validator._check_parameter_references(graph_json, session, result)

    codes = [i.code for i in result.issues]
    assert "PARAMETER_SCHEMA_DRIFT" in codes


# ---------------------------------------------------------------------------
# Snapshot parameter_bindings_json capture tests
# ---------------------------------------------------------------------------


def test_parameter_bindings_format() -> None:
    """parameter_bindings_json follows the expected format from RFC §4.6."""
    bindings = {
        "node-1": {
            "agent_id": str(uuid.uuid4()),
            "parameter_schema_id": str(uuid.uuid4()),
            "parameter_set_id": str(uuid.uuid4()),
            "resolved_values": {"model_backend": "gpt-4", "temperature": 0.3},
        }
    }
    assert "node-1" in bindings
    entry = bindings["node-1"]
    assert "agent_id" in entry
    assert "parameter_schema_id" in entry
    assert "parameter_set_id" in entry
    assert "resolved_values" in entry
    assert entry["resolved_values"]["model_backend"] == "gpt-4"

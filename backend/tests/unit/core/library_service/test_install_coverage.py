"""Unit tests lifting new-code coverage on modulo.core.library_service.install.

The backend unit suite (pytest tests/unit/ --cov=src/modulo) is what feeds
backend/coverage.xml to SonarCloud, and SonarCloud's new-code quality gate is
computed from exactly those lines.  The installed-collection install path is
integration-tested (tests/integration/test_library_collection_lifecycle.py) but
integration suites are excluded from the SonarCloud coverage denominator, so the
bundle-building / pin-resolution / persist helpers in install.py were otherwise
uncovered on the PR's new lines.

These tests exercise the pure helpers and the DB-touching service functions with
contract-correct AsyncMock sessions (no live DB), mirroring the style of the
existing TestInstallCollectionService tests.
"""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import workflow_import_export as wix
from modulo.core.library_service import install as install_mod
from modulo.core.library_service._seed_data import MODULO_ORG_ID
from modulo.core.library_service.install import (
    CollectionInstallError,
    CollectionNotPublishedError,
    PinResolutionError,
    _append_agent_pin,
    _append_pipeline_template_pin,
    _build_bundle_from_pins,
    _build_connector_checklist,
    _clone_builtin,
    _connector_ref_type_id,
    _definition_from_field_spec,
    _definition_from_fields,
    _persist_collection_row,
    _resolve_collection,
    _resolve_pin,
    _schema_definition_from_content,
    _transient_primitives,
    install_collection,
)
from modulo.db.models.library_primitive import LibraryPrimitive

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _prim(
    pid: uuid.UUID,
    *,
    source: str = "modulo",
    primitive_type: str = "schema",
    name: str = "n",
    slug: str = "s",
    author: str = "a",
    version: str = "1.0",
    content_json: dict[str, Any] | None = None,
    deleted_at: str | None = None,
    **kw: Any,
) -> LibraryPrimitive:
    kw.setdefault("organisation_id", _ORG_ID)
    return LibraryPrimitive(
        id=pid,
        source=source,
        primitive_type=primitive_type,
        name=name,
        slug=slug,
        author=author,
        version=version,
        content_json=content_json or {},
        deleted_at=deleted_at,
        **kw,
    )


# ---------------------------------------------------------------------------
# Pure schema-definition helpers
# ---------------------------------------------------------------------------


class TestFieldSpecToSchema:
    def test_string_spec(self) -> None:
        assert _definition_from_field_spec("string") == {"type": "string"}

    def test_named_type_maps_and_enum(self) -> None:
        out = _definition_from_field_spec({"type": "integer", "enum": [1, 2, 3]})
        assert out == {"type": "integer", "enum": [1, 2, 3]}

    def test_unknown_type_defaults_to_string(self) -> None:
        assert _definition_from_field_spec({"type": "banana"}) == {"type": "string"}

    def test_non_dict_non_str_falls_back(self) -> None:
        assert _definition_from_field_spec(42) == {"type": "string"}
        assert _definition_from_field_spec(None) == {"type": "string"}

    def test_named_subfield_map_becomes_object(self) -> None:
        out = _definition_from_field_spec({"findings": {"severity": {"type": "string"}}})
        assert out["type"] == "object"
        assert out["properties"]["findings"]["type"] == "object"
        assert out["properties"]["findings"]["properties"]["severity"] == {"type": "string"}

    def test_subfield_required_is_skipped(self) -> None:
        out = _definition_from_field_spec({"findings": {"required": True, "severity": {"type": "string"}}})
        props = out["properties"]["findings"]["properties"]
        assert "required" not in props


class TestFieldsToSchema:
    def test_full_fields(self) -> None:
        fields: list[dict[str, Any]] = [
            {"name": "title", "type": "string", "required": True},
            {"name": "count", "type": "integer"},
            {"name": "notes", "type": "text"},
            {"name": "", "type": "string"},  # empty name skipped
        ]
        definition = _definition_from_fields(fields)
        assert definition["type"] == "object"
        assert definition["required"] == ["title"]
        assert definition["properties"]["title"] == {"type": "string"}
        assert definition["properties"]["count"] == {"type": "integer"}
        assert definition["properties"]["notes"] == {"type": "string"}
        assert set(definition["properties"]) == {"title", "count", "notes"}

    def test_empty_fields(self) -> None:
        definition = _definition_from_fields([])
        assert definition == {"type": "object", "properties": {}}


class TestSchemaDefinitionFromContent:
    def test_definition_json_wins(self) -> None:
        content = {"definition_json": {"type": "object", "properties": {}}}
        out = _schema_definition_from_content(content)
        assert isinstance(out, dict)
        assert out == content["definition_json"]

    def test_fields_fallback(self) -> None:
        content = {"fields": [{"name": "x", "type": "string"}]}
        out = _schema_definition_from_content(content)
        assert isinstance(out, dict)
        assert out["type"] == "object"
        assert out["properties"]["x"] == {"type": "string"}

    def test_neither_returns_none(self) -> None:
        assert _schema_definition_from_content({}) is None
        assert _schema_definition_from_content({"definition_json": {}}) is None


# ---------------------------------------------------------------------------
# Connector checklist helpers
# ---------------------------------------------------------------------------


class TestConnectorRefType:
    def test_plain_string(self) -> None:
        assert _connector_ref_type_id("github") == "github"

    def test_dict_connector_type_id(self) -> None:
        assert _connector_ref_type_id({"connector_type_id": "slack"}) == "slack"

    def test_dict_connector_type(self) -> None:
        assert _connector_ref_type_id({"connector_type": "email"}) == "email"

    def test_dict_type_fallback(self) -> None:
        assert _connector_ref_type_id({"type": "fs"}) == "fs"

    def test_empty_or_missing(self) -> None:
        assert not _connector_ref_type_id({})
        assert not _connector_ref_type_id(None)
        assert not _connector_ref_type_id(5)


class TestConnectorChecklist:
    def test_dedupes_by_type(self) -> None:
        agent_a = _prim(uuid.uuid4(), primitive_type="agent")
        agent_a.content_json = {"connector_type_refs": ["github", {"connector_type_id": "slack"}]}
        agent_b = _prim(uuid.uuid4(), primitive_type="agent")
        agent_b.content_json = {"connector_type_refs": ["github"]}
        schema = _prim(uuid.uuid4(), primitive_type="schema")
        schema.content_json = {}
        checklist = _build_connector_checklist([schema, agent_a, agent_b])
        ids = [c["connector_type_id"] for c in checklist]
        assert ids == ["github", "slack"]
        for c in checklist:
            assert c["status"] == "pending"

    def test_no_agents_means_empty(self) -> None:
        assert not _build_connector_checklist([_prim(uuid.uuid4(), primitive_type="schema")])


# ---------------------------------------------------------------------------
# _transient_primitives / _clone_builtin
# ---------------------------------------------------------------------------


class TestTransientPrimitives:
    def test_filters_to_transient_only(self) -> None:
        transient = _prim(uuid.uuid4())
        persistent = _prim(uuid.uuid4())
        with patch.object(install_mod, "sa_inspect") as mock_inspect:

            def _inspect(p: Any) -> Any:
                m = MagicMock()
                m.transient = p is transient
                return m

            mock_inspect.side_effect = _inspect
            result = _transient_primitives([transient, persistent])
        assert result == [transient]


class TestCloneBuiltin:
    def test_clone_copies_fields(self) -> None:
        src = _prim(
            uuid.uuid4(),
            primitive_type="agent",
            name="Agent",
            slug="agent",
            content_json={"prompt_template": "do"},
            source="modulo",
            tags=["x"],
            description="d",
            verified=True,
            checksum="c",
            ed25519_signature="sig",
            download_count=5,
            average_rating=None,
            review_count=2,
            owner_team_id=None,
            visibility="org",
            tier="native",
            account_id=_USER_ID,
            auto_update=True,
            contribution_status="published",
            status="published",
            manifest_pins=[{"slug": "a", "version": "1.0"}],
        )
        clone = _clone_builtin(src)
        assert clone.id == src.id
        assert clone.source == "modulo"
        assert clone.content_json == {"prompt_template": "do"}
        assert clone.tags == ["x"]
        assert clone.verified is True
        assert clone.download_count == 5
        assert clone.manifest_pins == [{"slug": "a", "version": "1.0"}]


# ---------------------------------------------------------------------------
# _resolve_collection / _resolve_pin
# ---------------------------------------------------------------------------


class TestResolveCollection:
    async def test_org_owned_row_wins(self) -> None:
        coll = _prim(uuid.uuid4(), primitive_type="library_collection", organisation_id=_ORG_ID)
        session = MagicMock()
        session.get = AsyncMock(return_value=coll)
        with patch.object(install_mod, "get_primitive", new=AsyncMock(return_value=None)):
            assert await _resolve_collection(session, _ORG_ID, coll.id) is coll

    async def test_resolver_fallback_visible_everywhere(self) -> None:
        coll = _prim(uuid.uuid4(), primitive_type="library_collection", organisation_id=uuid.uuid4())
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        resolved = _prim(coll.id, primitive_type="library_collection", source="modulo", organisation_id=uuid.uuid4())
        with patch.object(install_mod, "get_primitive", new=AsyncMock(return_value=resolved)):
            assert await _resolve_collection(session, _ORG_ID, coll.id) is resolved

    async def test_resolver_fallback_org_owned(self) -> None:
        coll = _prim(uuid.uuid4(), primitive_type="library_collection", organisation_id=uuid.uuid4())
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        resolved = _prim(coll.id, primitive_type="library_collection", organisation_id=_ORG_ID)
        with patch.object(install_mod, "get_primitive", new=AsyncMock(return_value=resolved)):
            assert await _resolve_collection(session, _ORG_ID, coll.id) is resolved

    async def test_resolver_fallback_cross_org_invisible(self) -> None:
        coll = _prim(uuid.uuid4(), primitive_type="library_collection", organisation_id=uuid.uuid4())
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        resolved = _prim(coll.id, primitive_type="library_collection", source="local", organisation_id=uuid.uuid4())
        with patch.object(install_mod, "get_primitive", new=AsyncMock(return_value=resolved)):
            assert await _resolve_collection(session, _ORG_ID, coll.id) is None

    async def test_resolver_none(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        with patch.object(install_mod, "get_primitive", new=AsyncMock(return_value=None)):
            assert await _resolve_collection(session, _ORG_ID, uuid.uuid4()) is None


class TestResolvePin:
    async def test_resolves_first_matching_type(self) -> None:
        session = MagicMock()
        prim = _prim(uuid.uuid4(), primitive_type="schema", slug="s", version="1.0")
        with patch.object(install_mod, "get_primitive_by_slug", new=AsyncMock(return_value=prim)):
            assert await _resolve_pin(session, _ORG_ID, "s", "1.0") is prim

    async def test_version_mismatch_raises(self) -> None:
        session = MagicMock()
        prim = _prim(uuid.uuid4(), primitive_type="schema", slug="s", version="2.0")
        with (
            patch.object(install_mod, "get_primitive_by_slug", new=AsyncMock(return_value=prim)),
            pytest.raises(PinResolutionError, match="does not match"),
        ):
            await _resolve_pin(session, _ORG_ID, "s", "1.0")

    async def test_deleted_pin_skipped_then_not_found(self) -> None:
        session = MagicMock()
        deleted = _prim(uuid.uuid4(), primitive_type="schema", slug="s", version="1.0", deleted_at="2020-01-01")
        with (
            patch.object(install_mod, "get_primitive_by_slug", new=AsyncMock(side_effect=[deleted, None, None, None])),
            pytest.raises(PinResolutionError, match="does not resolve"),
        ):
            await _resolve_pin(session, _ORG_ID, "s", "1.0")

    async def test_agent_type_resolves(self) -> None:
        session = MagicMock()
        agent = _prim(uuid.uuid4(), primitive_type="agent", slug="a", version="3.0")
        with patch.object(install_mod, "get_primitive_by_slug", new=AsyncMock(side_effect=[None, agent])):
            assert await _resolve_pin(session, _ORG_ID, "a", "3.0") is agent


# ---------------------------------------------------------------------------
# _persist_collection_row
# ---------------------------------------------------------------------------


def _session_with_get_dispatch(**gets: Any) -> MagicMock:
    """AsyncMock session whose ``get`` dispatches on the model class name."""
    session = MagicMock(name="session")
    session.add = MagicMock()
    session.flush = AsyncMock()

    async def _get(model: Any, key: Any) -> Any:
        return gets.get(model.__name__)

    session.get = AsyncMock(side_effect=_get)
    return session


def _execute_returning(value: Any) -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return AsyncMock(return_value=result)


class TestPersistCollectionRow:
    async def test_persistent_row_returned_untouched(self) -> None:
        coll = _prim(uuid.uuid4(), primitive_type="library_collection")
        session = MagicMock(name="session")
        with (
            patch.object(install_mod, "_transient_primitives", new=MagicMock(return_value=[])),
            patch.object(install_mod, "set_rls_org", new=AsyncMock()) as rls,
        ):
            result = await _persist_collection_row(session, _ORG_ID, coll)
        assert result == coll.id
        rls.assert_not_called()
        session.add.assert_not_called()

    async def test_missing_sentinel_org_raises_actionable(self) -> None:
        coll = _collection()
        session = _session_with_get_dispatch(Organisation=None)
        with (
            patch.object(install_mod, "set_rls_org", new=AsyncMock()),
            pytest.raises(CollectionInstallError, match="alembic upgrade head"),
        ):
            await _persist_collection_row(session, _ORG_ID, coll)

    async def test_rls_context_restored_even_on_error(self) -> None:
        coll = _collection()
        session = _session_with_get_dispatch(Organisation=None)
        with (
            patch.object(install_mod, "set_rls_org", new=AsyncMock()) as rls,
            pytest.raises(CollectionInstallError),
        ):
            await _persist_collection_row(session, _ORG_ID, coll)
        assert [call.args[1] for call in rls.call_args_list] == [MODULO_ORG_ID, _ORG_ID]

    async def test_tuple_hit_same_content_reuses_row(self) -> None:
        coll = _collection()
        existing = _prim(
            coll.id,
            primitive_type="library_collection",
            name="My Collection",
            slug="my-collection",
            source="local",
            version="1.0",
            manifest_pins=[],
        )
        session = _session_with_get_dispatch(Organisation=MagicMock())
        session.execute = _execute_returning(existing)
        with patch.object(install_mod, "set_rls_org", new=AsyncMock()):
            result = await _persist_collection_row(session, _ORG_ID, coll)
        assert result == existing.id
        session.add.assert_not_called()
        session.flush.assert_not_awaited()

    async def test_tuple_hit_stale_content_resyncs(self) -> None:
        coll = _collection()
        existing = _prim(
            coll.id,
            primitive_type="library_collection",
            name="My Collection",
            slug="my-collection",
            source="local",
            version="1.0",
            manifest_pins=[],
        )
        existing.checksum = "stale-checksum"
        session = _session_with_get_dispatch(Organisation=MagicMock())
        session.execute = _execute_returning(existing)
        with patch.object(install_mod, "set_rls_org", new=AsyncMock()):
            result = await _persist_collection_row(session, _ORG_ID, coll)
        assert result == existing.id
        assert existing.checksum == coll.checksum
        assert existing.manifest_pins == coll.manifest_pins
        session.add.assert_not_called()
        session.flush.assert_awaited_once()

    async def test_tuple_miss_soft_deleted_pk_holder_reused(self) -> None:
        coll = _collection()
        holder = _prim(coll.id, primitive_type="library_collection", deleted_at="2020-01-01")
        session = _session_with_get_dispatch(Organisation=MagicMock(), LibraryPrimitive=holder)
        session.execute = _execute_returning(None)
        with patch.object(install_mod, "set_rls_org", new=AsyncMock()):
            result = await _persist_collection_row(session, _ORG_ID, coll)
        assert result == coll.id
        session.add.assert_not_called()

    async def test_tuple_and_pk_miss_inserts_clone(self) -> None:
        coll = _collection()
        session = _session_with_get_dispatch(Organisation=MagicMock(), LibraryPrimitive=None)
        session.execute = _execute_returning(None)
        with patch.object(install_mod, "set_rls_org", new=AsyncMock()):
            result = await _persist_collection_row(session, _ORG_ID, coll)
        added = [call.args[0] for call in session.add.call_args_list]
        assert len(added) == 1
        assert added[0].id == coll.id
        assert result == coll.id
        session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# Bundle building
# ---------------------------------------------------------------------------


def _make_schema_pin(
    pid: uuid.UUID, *, fields: list[dict[str, Any]] | None = None, definition_json: dict[str, Any] | None = None
) -> LibraryPrimitive:
    content: dict[str, Any] = {}
    if definition_json is not None:
        content["definition_json"] = definition_json
    if fields is not None:
        content["fields"] = fields
    return _prim(pid, primitive_type="schema", name="Schema", slug="schema", content_json=content)


def _make_agent_pin(
    pid: uuid.UUID,
    *,
    input_schema: str | None = None,
    output_schema: str | None = None,
    connector_refs: list[Any] | None = None,
) -> LibraryPrimitive:
    content = {
        "prompt_template": "do the thing",
        "input_schema": input_schema,
        "output_schema": output_schema,
        "connector_type_refs": connector_refs or [],
        "evals": {"foo": "bar"},
        "retry_policy": {"max": 1},
        "token_budget": 100,
    }
    return _prim(pid, primitive_type="agent", name="Agent", slug="agent", content_json=content)


def _make_pipeline_template_pin(pid: uuid.UUID) -> LibraryPrimitive:
    content = {
        "agents": [
            {"name": "Tpl Agent", "prompt_template": "go", "input_schema": "ctx", "output_schema": "out"},
        ],
        "graph_nodes": [
            {"id": "n0", "node_type": "agent", "agent_index": 0, "position": {"x": 1, "y": 2}},
        ],
        "edges": [
            {"source_node_id": "n0", "target_node_id": "n1", "edge_type": "normal", "hitl_gate_config": {"k": "v"}},
        ],
    }
    return _prim(pid, primitive_type="pipeline_template", name="Tpl", slug="tpl", content_json=content)


def _make_workflow_pin(pid: uuid.UUID) -> LibraryPrimitive:
    content = {
        "bundle": {
            "pipeline": {
                "graph_nodes_json": [{"id": "w0", "node_type": "agent"}],
            },
            "agents": [{"id": "a0", "name": "Wf Agent", "prompt_template": "x"}],
            "schemas": [{"id": "s0", "name": "Wf Schema", "definition_json": {"type": "object"}}],
            "edges": [{"source": "w0", "target": "w1"}],
        }
    }
    return _prim(pid, primitive_type="workflow", name="Wf", slug="wf", content_json=content)


# What ``strip_graph_node_credentials`` records when a v1 export removes node
# credentials (FAR-1181) — the shape ``_warn_redacted_credentials`` renders.
_REDACTED_RECORD: list[dict[str, Any]] = [
    {
        "node_id": "4c7e9a10-8f3a-4d61-9b2c-4a5e6f809012",
        "field": "env_vars",
        "path": "GITHUB_TOKEN",
        "reason": "key-classified credential",
    },
    {
        "node_id": "4c7e9a10-8f3a-4d61-9b2c-4a5e6f809012",
        "field": "parameter_overrides",
        "path": "nested.password",
        "reason": "key-classified credential",
    },
]


def _workflow_pin_with_record(redacted: list[dict[str, Any]] | None) -> LibraryPrimitive:
    """A collection workflow pin whose embedded bundle is a v1 export bundle.

    ``redacted`` is the export's credential-redaction record; ``None`` models a
    bundle with nothing stripped (or one exported before FAR-1181). The bundle
    carries no agents / schemas / edges so the collection-install test below can
    drive the REAL ``materialize_import`` without any DB write beyond the
    pipeline itself.
    """
    bundle: dict[str, Any] = {
        "pipeline": {
            "name": "Shipped",
            "description": "shipped workflow",
            "graph_nodes_json": [{"id": "w0", "node_type": "agent", "position": {"x": 0, "y": 0}}],
        },
        "agents": [],
        "schemas": [],
        "edges": [],
    }
    if redacted is not None:
        bundle["redacted_credentials"] = redacted
    return _prim(
        uuid.uuid4(),
        primitive_type="workflow",
        name="Shipped WF",
        slug="shipped-wf",
        version="1.0",
        content_json={"bundle": bundle},
    )


class TestBuildBundleFromPins:
    def test_schema_pin(self) -> None:
        pid = uuid.uuid4()
        pin = _make_schema_pin(pid, fields=[{"name": "x", "type": "string", "required": True}])
        bundle = _build_bundle_from_pins([pin])
        assert bundle["schemas"][0]["id"] == str(pid)
        assert bundle["schemas"][0]["definition_json"]["type"] == "object"
        assert bundle["pipeline"]["name"] == "Collection Install"

    def test_schema_pin_with_definition_json(self) -> None:
        pid = uuid.uuid4()
        pin = _make_schema_pin(pid, definition_json={"type": "object", "properties": {}})
        bundle = _build_bundle_from_pins([pin])
        assert bundle["schemas"][0]["definition_json"] == {"type": "object", "properties": {}}

    def test_agent_pin_resolves_schema_refs(self) -> None:
        schema_pid = uuid.uuid4()
        agent_pid = uuid.uuid4()
        schema_pin = _make_schema_pin(schema_pid)
        agent_pin = _make_agent_pin(agent_pid, input_schema="schema", output_schema="schema")
        bundle = _build_bundle_from_pins([schema_pin, agent_pin])
        agent = bundle["agents"][0]
        assert agent["input_schema_id"] == str(schema_pid)
        assert agent["output_schema_id"] == str(schema_pid)
        assert agent["prompt_template"] == "do the thing"
        assert bundle["pipeline"]["graph_nodes_json"][0]["agent_id"] == str(agent_pid)

    def test_pipeline_template_pin(self) -> None:
        pid = uuid.uuid4()
        pin = _make_pipeline_template_pin(pid)
        bundle = _build_bundle_from_pins([pin])
        assert len(bundle["agents"]) == 1
        assert bundle["agents"][0]["name"] == "Tpl Agent"
        node = bundle["pipeline"]["graph_nodes_json"][0]
        assert node["agent_id"] == bundle["agents"][0]["id"]
        assert node["position"] == {"x": 1, "y": 2}
        assert bundle["edges"][0]["hitl_gate_config"] == {"k": "v"}

    def test_workflow_pin_merges_bundle(self) -> None:
        pid = uuid.uuid4()
        pin = _make_workflow_pin(pid)
        bundle = _build_bundle_from_pins([pin])
        assert len(bundle["agents"]) == 1
        assert len(bundle["schemas"]) == 1
        assert len(bundle["pipeline"]["graph_nodes_json"]) == 1
        assert len(bundle["edges"]) == 1

    def test_unknown_pin_type_raises(self) -> None:
        pin = _prim(uuid.uuid4(), primitive_type="composite", name="X", slug="x")
        with pytest.raises(CollectionInstallError, match="unsupported primitive"):
            _build_bundle_from_pins([pin])

    def test_workflow_pin_propagates_redaction_record(self) -> None:
        """FAR-1181: the embedded workflow bundle's redaction record survives.

        The v1 export STRIPS node credentials and records what it removed. A
        workflow pin that was exported → imported → shipped into a collection
        carries that record inside its embedded bundle; the synthetic bundle
        must carry it too, or ``materialize_import`` has nothing to warn from.
        """
        bundle = _build_bundle_from_pins([_workflow_pin_with_record(list(_REDACTED_RECORD))])
        paths = [entry["path"] for entry in bundle["redacted_credentials"]]
        assert paths == ["GITHUB_TOKEN", "nested.password"]

    def test_workflow_pin_without_record_carries_none(self) -> None:
        """A bundle with nothing stripped must not fabricate a record."""
        for redacted in (None, []):
            bundle = _build_bundle_from_pins([_workflow_pin_with_record(redacted)])
            assert not bundle["redacted_credentials"]


class TestAppendAgentPin:
    def test_builds_entry_and_node(self) -> None:
        agents: list[dict[str, Any]] = []
        graph_nodes: list[dict[str, Any]] = []
        schema_ids: dict[str, str] = {"schema": str(uuid.uuid4())}
        pin = _make_agent_pin(uuid.uuid4(), input_schema="schema")
        _append_agent_pin(pin, agents, graph_nodes, schema_ids)
        assert agents[0]["input_schema_id"] == schema_ids["schema"]
        assert graph_nodes[0]["node_type"] == "agent"


class TestAppendPipelineTemplatePin:
    def test_builds_entries_nodes_edges(self) -> None:
        agents: list[dict[str, Any]] = []
        graph_nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        schema_ids: dict[str, str] = {"ctx": str(uuid.uuid4()), "out": str(uuid.uuid4())}
        pin = _make_pipeline_template_pin(uuid.uuid4())
        _append_pipeline_template_pin(pin, pin.content_json, agents, graph_nodes, edges, schema_ids)
        assert agents[0]["input_schema_id"] == schema_ids["ctx"]
        assert agents[0]["output_schema_id"] == schema_ids["out"]
        assert graph_nodes[0]["agent_id"] == agents[0]["id"]
        assert edges[0]["hitl_gate_config"] == {"k": "v"}


# ---------------------------------------------------------------------------
# install_collection error + success paths (covers install.py body)
# ---------------------------------------------------------------------------


def _mock_session(
    collection: LibraryPrimitive,
    *,
    tuple_row: Any = None,
    pk_row: Any = None,
    existing_install: Any = None,
) -> MagicMock:
    """Contract-correct session mock for the install_collection flow.

    ``get`` dispatches on the model class: the Organisation lookup in
    _persist_collection_row finds the sentinel, the LibraryPrimitive lookups
    (collection resolution + PK guard) find ``collection`` / ``pk_row``.
    ``execute`` dispatches on the statement text: the collection_installs
    existence check returns ``existing_install``, the library_primitives
    tuple lookup in _persist_collection_row returns ``tuple_row``.
    """
    session = MagicMock(name="session")
    no_autoflush = MagicMock()
    no_autoflush.__enter__ = MagicMock(return_value=None)
    no_autoflush.__exit__ = MagicMock(return_value=False)
    session.no_autoflush = no_autoflush
    session.add = MagicMock()
    session.flush = AsyncMock()

    async def _get(model: Any, key: Any) -> Any:
        if model.__name__ == "Organisation":
            return MagicMock(name="sentinel-org")
        if model.__name__ == "LibraryPrimitive":
            return collection if pk_row is None else pk_row
        return None

    session.get = AsyncMock(side_effect=_get)

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        result = MagicMock()
        is_install_check = "collection_install" in str(stmt)
        result.scalar_one_or_none = MagicMock(return_value=existing_install if is_install_check else tuple_row)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _collection(
    *,
    status: str = "published",
    primitive_type: str = "library_collection",
    manifest_pins: list[dict[str, Any]] | None = None,
    source: str = "local",
) -> LibraryPrimitive:
    return _prim(
        uuid.uuid4(),
        primitive_type=primitive_type,
        name="My Collection",
        slug="my-collection",
        status=status,
        source=source,
        version="1.0",
        manifest_pins=manifest_pins or [],
    )


class TestInstallCollectionPaths:
    async def test_collection_not_found(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        with (
            patch.object(install_mod, "get_primitive", new=AsyncMock(return_value=None)),
            pytest.raises(CollectionInstallError, match="not found"),
        ):
            await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())

    async def test_not_a_collection(self) -> None:
        session = _mock_session(collection=_collection(primitive_type="workflow"))
        with pytest.raises(CollectionInstallError, match="not a collection"):
            await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())

    async def test_not_published(self) -> None:
        session = _mock_session(collection=_collection(status="draft"))
        with pytest.raises(CollectionNotPublishedError):
            await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())

    async def test_pin_missing_slug(self) -> None:
        session = _mock_session(collection=_collection(manifest_pins=[{"slug": "", "version": "1.0"}]))
        with pytest.raises(PinResolutionError, match="missing slug or version"):
            await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())

    async def test_no_pins(self) -> None:
        session = _mock_session(collection=_collection(manifest_pins=[]))
        with pytest.raises(CollectionInstallError, match="no manifest pins"):
            await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())

    async def test_already_installed(self) -> None:
        existing = MagicMock()
        coll = _collection(manifest_pins=[{"slug": "my-schema", "version": "1.0"}])
        session = _mock_session(collection=coll, existing_install=existing)
        pin = _prim(uuid.uuid4(), primitive_type="schema", slug="my-schema", version="1.0")
        with (
            patch.object(install_mod, "_resolve_pin", new=AsyncMock(return_value=pin)),
            pytest.raises(CollectionInstallError, match="already installed"),
        ):
            await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())

    async def test_success_with_mixed_pins(self) -> None:
        schema_pid = uuid.uuid4()
        agent_pid = uuid.uuid4()
        tpl_pid = uuid.uuid4()
        coll = _collection(
            manifest_pins=[
                {"slug": "my-schema", "version": "1.0"},
                {"slug": "my-agent", "version": "1.0"},
                {"slug": "my-tpl", "version": "1.0"},
            ]
        )
        session = _mock_session(collection=coll, existing_install=None)

        async def _resolve(s: Any, org: Any, slug: str, version: str) -> LibraryPrimitive:
            if slug == "my-schema":
                return _make_schema_pin(schema_pid, fields=[{"name": "x", "type": "string"}])
            if slug == "my-agent":
                return _make_agent_pin(agent_pid, input_schema="my-schema")
            return _make_pipeline_template_pin(tpl_pid)

        with (
            patch.object(install_mod, "_resolve_pin", new=AsyncMock(side_effect=_resolve)),
            patch.object(install_mod, "set_rls_org", new=AsyncMock()),
            patch.object(
                install_mod,
                "materialize_import",
                new=AsyncMock(return_value={"schemas": {}, "agents": {}, "pipeline_id": str(uuid.uuid4())}),
            ),
            patch.object(install_mod, "_stamp_install_id", new=AsyncMock(return_value=[])),
            patch.object(install_mod, "_record_entities", new=MagicMock()),
        ):
            install = await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())

        assert install.status == "installed"
        assert install.organisation_id == _ORG_ID
        assert install.collection_id == coll.id
        manifest = install.resolved_manifest
        assert isinstance(manifest, dict)
        assert not manifest["warnings"]
        assert "schemas" in manifest


# ---------------------------------------------------------------------------
# FAR-1181: a collection install must re-surface the export's credential
# redaction record as a warning (the stripped nodes arrive with no value to
# restore, so the warning naming them is the ONLY actionable signal).
# ---------------------------------------------------------------------------


class _AsyncNoopContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def _install_workflow_collection(pin: LibraryPrimitive) -> Any:
    """Run ``install_collection`` end-to-end with the REAL ``materialize_import``.

    Only the DB edges are faked: the pin resolution, the synthetic bundle build,
    and the whole import engine (including the warning mechanism) run for real,
    so the assertion below observes what an operator would actually get back.
    """
    coll = _collection(manifest_pins=[{"slug": pin.slug, "version": pin.version}])
    session = _mock_session(collection=coll, existing_install=None)
    session.begin_nested = MagicMock(return_value=_AsyncNoopContext())

    with (
        patch.object(install_mod, "_resolve_pin", new=AsyncMock(return_value=pin)),
        patch.object(install_mod, "set_rls_org", new=AsyncMock()),
        patch.object(install_mod, "_stamp_install_id", new=AsyncMock(return_value=[])),
        patch.object(install_mod, "_record_entities", new=MagicMock()),
        # The import engine's only DB reads/writes: names lookups + creates.
        patch.object(wix, "get_existing_agent_names", new=AsyncMock(return_value=set())),
        patch.object(wix, "get_existing_pipeline_names", new=AsyncMock(return_value=set())),
        patch.object(wix, "create_pipeline", new=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))),
        patch.object(wix, "create_library_primitive", new=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))),
    ):
        return await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())


class TestInstallCollectionRedactionWarning:
    async def test_install_warns_naming_stripped_keys(self) -> None:
        """A stripped bundle shipped in a collection still warns on install."""
        install = await _install_workflow_collection(_workflow_pin_with_record(list(_REDACTED_RECORD)))
        warnings_text = " | ".join(install.resolved_manifest["warnings"])
        assert "GITHUB_TOKEN" in warnings_text
        assert "nested.password" in warnings_text
        assert "re-provision" in warnings_text

    @pytest.mark.parametrize("redacted", [None, []], ids=["absent-record", "empty-record"])
    async def test_install_without_stripped_credentials_is_silent(self, redacted: list[dict[str, Any]] | None) -> None:
        """Nothing stripped → no warning (the record, not the install, drives it)."""
        install = await _install_workflow_collection(_workflow_pin_with_record(redacted))
        warnings_text = " | ".join(install.resolved_manifest["warnings"])
        assert "re-provision" not in warnings_text

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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.library_service import install as install_mod
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
    _persist_builtin_primitives,
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
# _persist_builtin_primitives
# ---------------------------------------------------------------------------


def _mock_session_for_persist(*, org_exists: bool, prim_exists: bool) -> MagicMock:
    session = MagicMock(name="session")
    no_autoflush = MagicMock()
    no_autoflush.__enter__ = MagicMock(return_value=None)
    no_autoflush.__exit__ = MagicMock(return_value=False)
    session.no_autoflush = no_autoflush
    session.add = MagicMock()
    session.flush = AsyncMock()

    def _get(model: Any, key: Any) -> Any:
        if model.__name__ == "Organisation":
            return None if not org_exists else MagicMock()
        return None if not prim_exists else MagicMock()

    session.get = AsyncMock(side_effect=_get)
    return session


class TestPersistBuiltinPrimitives:
    async def test_empty_returns_early(self) -> None:
        session = MagicMock(name="session")
        session.get = AsyncMock()
        await _persist_builtin_primitives(session, _ORG_ID, [])
        session.get.assert_not_called()

    async def test_creates_org_and_prims(self) -> None:
        prim = _prim(uuid.uuid4(), primitive_type="agent")
        session = _mock_session_for_persist(org_exists=False, prim_exists=False)
        with patch.object(install_mod, "set_rls_org", new=AsyncMock()):
            await _persist_builtin_primitives(session, _ORG_ID, [prim])
        added = [call.args[0] for call in session.add.call_args_list]
        assert any(getattr(a, "__tablename__", None) == "organisations" for a in added)
        assert any(a.id == prim.id for a in added)

    async def test_reuses_existing_rows(self) -> None:
        prim = _prim(uuid.uuid4(), primitive_type="agent")
        session = _mock_session_for_persist(org_exists=True, prim_exists=True)
        with patch.object(install_mod, "set_rls_org", new=AsyncMock()):
            await _persist_builtin_primitives(session, _ORG_ID, [prim])
        session.add.assert_not_called()


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


class TestBuildBundleFromPins:
    async def test_schema_pin(self) -> None:
        pid = uuid.uuid4()
        pin = _make_schema_pin(pid, fields=[{"name": "x", "type": "string", "required": True}])
        bundle = await _build_bundle_from_pins([pin])
        assert bundle["schemas"][0]["id"] == str(pid)
        assert bundle["schemas"][0]["definition_json"]["type"] == "object"
        assert bundle["pipeline"]["name"] == "Collection Install"

    async def test_schema_pin_with_definition_json(self) -> None:
        pid = uuid.uuid4()
        pin = _make_schema_pin(pid, definition_json={"type": "object", "properties": {}})
        bundle = await _build_bundle_from_pins([pin])
        assert bundle["schemas"][0]["definition_json"] == {"type": "object", "properties": {}}

    async def test_agent_pin_resolves_schema_refs(self) -> None:
        schema_pid = uuid.uuid4()
        agent_pid = uuid.uuid4()
        schema_pin = _make_schema_pin(schema_pid)
        agent_pin = _make_agent_pin(agent_pid, input_schema="schema", output_schema="schema")
        bundle = await _build_bundle_from_pins([schema_pin, agent_pin])
        agent = bundle["agents"][0]
        assert agent["input_schema_id"] == str(schema_pid)
        assert agent["output_schema_id"] == str(schema_pid)
        assert agent["prompt_template"] == "do the thing"
        assert bundle["pipeline"]["graph_nodes_json"][0]["agent_id"] == str(agent_pid)

    async def test_pipeline_template_pin(self) -> None:
        pid = uuid.uuid4()
        pin = _make_pipeline_template_pin(pid)
        bundle = await _build_bundle_from_pins([pin])
        assert len(bundle["agents"]) == 1
        assert bundle["agents"][0]["name"] == "Tpl Agent"
        node = bundle["pipeline"]["graph_nodes_json"][0]
        assert node["agent_id"] == bundle["agents"][0]["id"]
        assert node["position"] == {"x": 1, "y": 2}
        assert bundle["edges"][0]["hitl_gate_config"] == {"k": "v"}

    async def test_workflow_pin_merges_bundle(self) -> None:
        pid = uuid.uuid4()
        pin = _make_workflow_pin(pid)
        bundle = await _build_bundle_from_pins([pin])
        assert len(bundle["agents"]) == 1
        assert len(bundle["schemas"]) == 1
        assert len(bundle["pipeline"]["graph_nodes_json"]) == 1
        assert len(bundle["edges"]) == 1

    async def test_unknown_pin_type_raises(self) -> None:
        pin = _prim(uuid.uuid4(), primitive_type="composite", name="X", slug="x")
        with pytest.raises(CollectionInstallError, match="unsupported primitive"):
            await _build_bundle_from_pins([pin])


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
    collection: LibraryPrimitive, *, existing_install: Any = None, execute_result: Any = None
) -> MagicMock:
    session = MagicMock(name="session")
    session.get = AsyncMock(return_value=collection)
    no_autoflush = MagicMock()
    no_autoflush.__enter__ = MagicMock(return_value=None)
    no_autoflush.__exit__ = MagicMock(return_value=False)
    session.no_autoflush = no_autoflush
    session.add = MagicMock()
    session.flush = AsyncMock()

    ex_result = MagicMock()
    ex_result.scalar_one_or_none = MagicMock(return_value=existing_install)
    session.execute = AsyncMock(return_value=execute_result if execute_result is not None else ex_result)
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
            patch.object(install_mod, "_record_entities", new=AsyncMock()),
        ):
            install = await install_collection(session, _ORG_ID, _USER_ID, uuid.uuid4())

        assert install.status == "installed"
        assert install.organisation_id == _ORG_ID
        manifest = install.resolved_manifest
        assert isinstance(manifest, dict)
        assert not manifest["warnings"]
        assert "schemas" in manifest

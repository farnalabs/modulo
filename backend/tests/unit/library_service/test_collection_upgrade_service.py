"""Coverage-focused unit tests for the collection upgrade service (FAR-759).

The integration test (``tests/integration/test_library_collection_lifecycle.py``)
drives the happy path against real Postgres, but only ``tests/unit/`` feeds the
changed-lines coverage gate.  These tests exercise the module's helpers and
orchestrators directly, with a contract-correct fake session, so the upgrade
flow's branching (modified/preserved/warning/legacy paths) is measured.

Unit tier: no DB — rows are lightweight attribute objects and the session is an
``AsyncMock`` dispatching on model type.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.library_service import upgrade
from modulo.core.library_service.upgrade import (
    CollectionUpgradeError,
    NoUpdateAvailableError,
    UpgradeInstallNotFoundError,
    _agent_pin_content_fields,
    _classify_entities,
    _embedded_agent_contents,
    _embedded_schema_contents,
    _entity_exists,
    _legacy_pin_context,
    _load_install,
    _pin_entity_map,
    _pins_from_manifest,
    _pipeline_state,
    _reconcile_agent_entity,
    _reconcile_schema_entity,
    _resolve_pin_primitive,
    _schema_slug_map,
    _snapshot_pins,
    _version_key,
    _workflow_bundle_content,
    build_collection_delta,
    detect_collection_update,
    find_newer_version,
    upgrade_collection,
)
from modulo.db.models.agent import Agent
from modulo.db.models.collection_install import CollectionInstall
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.schema import Schema

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeScalars:
    def __init__(self, values: list) -> None:
        self._values = list(values)

    def __iter__(self):
        return iter(self._values)

    def first(self):
        return self._values[0] if self._values else None


class _FakeResult:
    def __init__(self, values: list) -> None:
        self._values = list(values)

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


def _session(
    *,
    get_map: dict | None = None,
    execute_rows: list | None = None,
    scalar_value: object | None = None,
    execute_side_effect: object | None = None,
) -> AsyncMock:
    session = AsyncMock()

    async def _get(model, obj_id, **_kwargs):
        return (get_map or {}).get((model, obj_id))

    session.get = AsyncMock(side_effect=_get)
    if execute_side_effect is not None:
        session.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        session.execute = AsyncMock(return_value=_FakeResult(execute_rows or []))
    session.scalar = AsyncMock(return_value=scalar_value)
    session.flush = AsyncMock()
    return session


def _prim(
    *,
    row_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    version: str = "1.0",
    status: str = "published",
    deleted_at=None,
    primitive_type: str = "library_collection",
    slug: str = "coll",
    description: str | None = None,
    content_json: dict | None = None,
    manifest_pins: list | None = None,
    version_group_id: uuid.UUID | None = None,
    update_available_version_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id or uuid.uuid4(),
        organisation_id=org_id or uuid.uuid4(),
        version=version,
        status=status,
        deleted_at=deleted_at,
        primitive_type=primitive_type,
        slug=slug,
        description=description,
        content_json=content_json if content_json is not None else {},
        manifest_pins=manifest_pins,
        version_group_id=version_group_id,
        update_available_version_id=update_available_version_id,
        created_at=None,
    )


def _install(
    *,
    install_id: uuid.UUID | None = None,
    collection_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    version: str | None = "1.0",
    manifest: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        install_id=install_id or uuid.uuid4(),
        collection_id=collection_id or uuid.uuid4(),
        organisation_id=org_id or uuid.uuid4(),
        collection_version=version,
        resolved_manifest=manifest if manifest is not None else {},
    )


# ---------------------------------------------------------------------------
# _version_key / _pins_from_manifest
# ---------------------------------------------------------------------------


class TestVersionKey:
    def test_none_and_empty_default(self) -> None:
        assert _version_key(None) == (0,)
        assert _version_key("") == (0,)

    def test_valid_dotted(self) -> None:
        assert _version_key("1.2.3") == (1, 2, 3)

    def test_invalid_segment_defaults(self) -> None:
        assert _version_key("1.x") == (0,)


class TestPinsFromManifest:
    def test_none_returns_empty(self) -> None:
        assert not _pins_from_manifest(None)

    def test_normalises_skips_dupes_and_invalid(self) -> None:
        pins = [
            {"slug": "s1", "version": "1.0"},
            {"slug": "s1", "version": "1.1"},  # duplicate slug skipped
            {"slug": "", "version": "1.0"},  # missing slug skipped
            {"slug": "s2", "version": ""},  # missing version skipped
            {"slug": "s3", "version": "1.0"},
        ]
        assert _pins_from_manifest(pins) == [
            {"slug": "s1", "version": "1.0"},
            {"slug": "s3", "version": "1.0"},
        ]


# ---------------------------------------------------------------------------
# _snapshot_pins / _pin_entity_map / _schema_slug_map
# ---------------------------------------------------------------------------


class TestSnapshotPins:
    def test_prefers_snapshot(self) -> None:
        install = _install(
            manifest={
                "pins": [
                    {"slug": "s1", "version": "1.0", "primitive_type": "schema"},
                    {"slug": "s1", "version": "1.1", "primitive_type": "schema"},  # dupe
                    {"slug": "", "version": "1.0"},  # no slug
                ]
            }
        )
        collection = _prim(manifest_pins=[{"slug": "ignored", "version": "9.9"}])
        out = _snapshot_pins(install, collection)
        assert out == [{"slug": "s1", "version": "1.0", "primitive_type": "schema"}]

    def test_falls_back_to_collection_manifest(self) -> None:
        install = _install(manifest={})
        collection = _prim(manifest_pins=[{"slug": "s2", "version": "2.0"}])
        out = _snapshot_pins(install, collection)
        assert out == [{"slug": "s2", "version": "2.0", "primitive_type": ""}]

    def test_empty_snapshot_list_falls_back(self) -> None:
        install = _install(manifest={"pins": []})
        collection = _prim(content_json={"manifest_pins": [{"slug": "s3", "version": "3.0"}]})
        out = _snapshot_pins(install, collection)
        assert out == [{"slug": "s3", "version": "3.0", "primitive_type": ""}]


class TestPinEntityMap:
    def test_non_dict_returns_empty(self) -> None:
        assert not _pin_entity_map(_install(manifest={"pin_entity_ids": "nope"}))

    def test_maps_and_skips_non_dict_per_type(self) -> None:
        install = _install(
            manifest={
                "pin_entity_ids": {
                    "s1": {"schema": ["e1", "e2"], "bad": "not-a-list"},
                    "s2": "not-a-dict",
                }
            }
        )
        assert _pin_entity_map(install) == {"s1": {"schema": ["e1", "e2"]}}


class TestSchemaSlugMap:
    def test_first_schema_wins(self) -> None:
        sid = uuid.uuid4()
        out = _schema_slug_map({"p1": {"schema": [str(sid)], "agent": ["x"]}, "p2": {"agent": ["y"]}})
        assert out == {"p1": sid}


# ---------------------------------------------------------------------------
# _resolve_pin_primitive
# ---------------------------------------------------------------------------


class TestResolvePinPrimitive:
    async def test_no_visible_row(self) -> None:
        with patch("modulo.core.library_service.get_primitive_by_slug", new_callable=AsyncMock, return_value=None):
            prim, ptype = await _resolve_pin_primitive(AsyncMock(), uuid.uuid4(), "s", "1.0")
        assert prim is None
        assert ptype == ""

    async def test_deleted_row_skipped(self) -> None:
        deleted = _prim(status="published")
        deleted.deleted_at = "2025-01-01"
        with patch(
            "modulo.core.library_service.get_primitive_by_slug",
            new_callable=AsyncMock,
            return_value=deleted,
        ):
            prim, ptype = await _resolve_pin_primitive(AsyncMock(), uuid.uuid4(), "s", "1.0")
        assert prim is None
        assert ptype == ""

    async def test_version_match_returns_prim(self) -> None:
        row = _prim(version="1.0")
        row.deleted_at = None
        with patch(
            "modulo.core.library_service.get_primitive_by_slug",
            new_callable=AsyncMock,
            return_value=row,
        ):
            prim, ptype = await _resolve_pin_primitive(AsyncMock(), uuid.uuid4(), "s", "1.0")
        assert prim is row
        assert ptype == "schema"

    async def test_version_mismatch_returns_type_only(self) -> None:
        row = _prim(version="2.0", primitive_type="agent")
        row.deleted_at = None

        async def _lookup(_s, _o, primitive_type, _slug, **_kw):
            return row if primitive_type == "agent" else None

        with patch("modulo.core.library_service.get_primitive_by_slug", side_effect=_lookup):
            prim, ptype = await _resolve_pin_primitive(AsyncMock(), uuid.uuid4(), "s", "1.0")
        assert prim is None
        assert ptype == "agent"


# ---------------------------------------------------------------------------
# find_newer_version
# ---------------------------------------------------------------------------


class TestFindNewerVersion:
    async def test_stamp_deleted_falls_back_to_scan(self) -> None:
        stamped = _prim(version="1.1")
        stamped.deleted_at = "x"
        newer = _prim(version="1.2")
        install = _install(version="1.0")
        collection = _prim(update_available_version_id=stamped.id)
        session = _session(
            get_map={(LibraryPrimitive, stamped.id): stamped},
            execute_rows=[newer],
        )
        assert await find_newer_version(session, install, collection) is newer

    async def test_stamp_unpublished_falls_back(self) -> None:
        stamped = _prim(version="1.1", status="draft")
        stamped.deleted_at = None
        newer = _prim(version="1.2")
        install = _install(version="1.0")
        collection = _prim(update_available_version_id=stamped.id)
        session = _session(
            get_map={(LibraryPrimitive, stamped.id): stamped},
            execute_rows=[newer],
        )
        assert await find_newer_version(session, install, collection) is newer

    async def test_stamp_valid_returned(self) -> None:
        stamped = _prim(version="1.2")
        stamped.deleted_at = None
        install = _install(version="1.0")
        collection = _prim(update_available_version_id=stamped.id)
        session = _session(get_map={(LibraryPrimitive, stamped.id): stamped})
        assert await find_newer_version(session, install, collection) is stamped

    async def test_scan_keeps_best_when_later_not_newer(self) -> None:
        v12 = _prim(version="1.2")
        v11 = _prim(version="1.1")
        install = _install(version="1.0")
        collection = _prim(update_available_version_id=None)
        session = _session(execute_rows=[v12, v11])
        assert await find_newer_version(session, install, collection) is v12

    async def test_scan_uses_group_and_id_fallback(self) -> None:
        v11 = _prim(version="1.1")
        v10 = _prim(version="1.0")  # equal to installed — skipped
        install = _install(version="1.0")
        collection = _prim(update_available_version_id=None, version_group_id=None)
        session = _session(execute_rows=[v11, v10])
        assert await find_newer_version(session, install, collection) is v11


# ---------------------------------------------------------------------------
# _load_install
# ---------------------------------------------------------------------------


class TestLoadInstall:
    async def test_missing(self) -> None:
        session = _session()
        with pytest.raises(UpgradeInstallNotFoundError):
            await _load_install(session, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    async def test_wrong_collection(self) -> None:
        inst = _install()
        session = _session(get_map={(CollectionInstall, inst.install_id): inst})
        with pytest.raises(UpgradeInstallNotFoundError):
            await _load_install(session, inst.organisation_id, inst.install_id, uuid.uuid4())

    async def test_wrong_org(self) -> None:
        inst = _install()
        session = _session(get_map={(CollectionInstall, inst.install_id): inst})
        with pytest.raises(UpgradeInstallNotFoundError):
            await _load_install(session, uuid.uuid4(), inst.install_id, inst.collection_id)

    async def test_ok(self) -> None:
        inst = _install()
        session = _session(get_map={(CollectionInstall, inst.install_id): inst})
        assert await _load_install(session, inst.organisation_id, inst.install_id, inst.collection_id) is inst


# ---------------------------------------------------------------------------
# build_collection_delta — unknown bucket classification
# ---------------------------------------------------------------------------


class TestBuildCollectionDeltaBuckets:
    def test_unknown_type_with_agent_entity(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "w", "version": "1.0", "primitive_type": "widget"}],
            new_pins=[{"slug": "w", "version": "1.1", "primitive_type": "widget"}],
            pin_entity_ids={"w": {"agent": ["e1"]}},
            entity_states={"e1": "unmodified"},
            pipeline_entity_id=None,
            pipeline_modified=False,
        )
        assert delta["agents"][0]["slug"] == "w"

    def test_unknown_type_with_schema_entity(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "w", "version": "1.0", "primitive_type": "widget"}],
            new_pins=[{"slug": "w", "version": "1.1", "primitive_type": "widget"}],
            pin_entity_ids={"w": {"schema": ["e1"]}},
            entity_states={"e1": "unmodified"},
            pipeline_entity_id=None,
            pipeline_modified=False,
        )
        assert delta["schemas"][0]["slug"] == "w"

    def test_unknown_type_no_entities_goes_to_pipelines(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "w", "version": "1.0", "primitive_type": "widget"}],
            new_pins=[{"slug": "w", "version": "1.1", "primitive_type": "widget"}],
            pin_entity_ids={},
            entity_states={},
            pipeline_entity_id=None,
            pipeline_modified=False,
        )
        assert delta["pipelines"][0]["slug"] == "w"

    def test_pipeline_template_pin_buckets_to_pipelines(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "t", "version": "1.0", "primitive_type": "pipeline_template"}],
            new_pins=[{"slug": "t", "version": "1.1", "primitive_type": "pipeline_template"}],
            pin_entity_ids={"t": {"agent": ["a1"]}},
            entity_states={"a1": "unmodified"},
            pipeline_entity_id="p1",
            pipeline_modified=False,
        )
        entry = delta["pipelines"][0]
        assert entry["entity_type"] == "pipeline_template"
        assert any(e["entity_type"] == "pipeline" for e in entry["entities"])

    def test_new_pin_without_primitive_type_infers_from_entities(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "a1", "version": "1.0", "primitive_type": "agent"}],
            new_pins=[{"slug": "a1", "version": "1.1"}],  # no primitive_type
            pin_entity_ids={"a1": {"agent": ["e1"]}},
            entity_states={"e1": "unmodified"},
            pipeline_entity_id=None,
            pipeline_modified=False,
        )
        # Falls back to the old pin's type.
        assert delta["agents"][0]["slug"] == "a1"


# ---------------------------------------------------------------------------
# _entity_exists / _classify_entities / _pipeline_state
# ---------------------------------------------------------------------------


class TestEntityExists:
    async def test_schema_and_agent_and_unknown(self) -> None:
        sid = uuid.uuid4()
        aid = uuid.uuid4()
        schema = SimpleNamespace(organisation_id=uuid.uuid4())
        agent = SimpleNamespace(organisation_id=uuid.uuid4())
        session = _session(get_map={(Schema, sid): schema, (Agent, aid): agent})
        assert await _entity_exists(session, "schema", sid) is schema
        assert await _entity_exists(session, "agent", aid) is agent
        assert await _entity_exists(session, "widget", sid) is None

    async def test_pipeline_uses_soft_deleted_scalar(self) -> None:
        pid = uuid.uuid4()
        pipeline = SimpleNamespace(id=pid)
        session = _session(scalar_value=pipeline)
        assert await _entity_exists(session, "pipeline", pid) is pipeline

    async def test_pipeline_missing(self) -> None:
        session = _session(scalar_value=None)
        assert await _entity_exists(session, "pipeline", uuid.uuid4()) is None


class TestClassifyEntities:
    async def test_missing_modified_and_unmodified(self) -> None:
        org = uuid.uuid4()
        missing_id, mod_id, unmod_id, detach_id = (str(uuid.uuid4()) for _ in range(4))
        install = _install(org_id=org, manifest={"entity_fingerprints": {mod_id: "old"}})
        install.install_id = uuid.uuid4()
        schema_unmod = SimpleNamespace(organisation_id=org, collection_install_id=install.install_id)
        schema_mod = SimpleNamespace(organisation_id=org, collection_install_id=install.install_id)
        schema_detached = SimpleNamespace(organisation_id=org, collection_install_id=uuid.uuid4())

        session = _session(
            get_map={
                (Schema, uuid.UUID(unmod_id)): schema_unmod,
                (Schema, uuid.UUID(mod_id)): schema_mod,
                (Schema, uuid.UUID(detach_id)): schema_detached,
            }
        )
        pin_entity_ids = {
            "p1": {"schema": [missing_id, mod_id, unmod_id, detach_id]},
        }

        async def _fingerprint(_s, _o, _etype, eid):
            return "new" if str(eid) == mod_id else "old"

        with patch.object(upgrade, "compute_entity_fingerprint", side_effect=_fingerprint):
            states = await _classify_entities(session, org, install, pin_entity_ids)

        assert states[missing_id] == "missing"
        assert states[mod_id] == "modified"  # fingerprint drift
        assert states[unmod_id] == "unmodified"
        assert states[detach_id] == "modified"  # provenance detached

    async def test_duplicate_entity_id_processed_once(self) -> None:
        org = uuid.uuid4()
        install = _install(org_id=org, manifest={})
        eid = str(uuid.uuid4())
        session = _session(
            get_map={
                (Agent, uuid.UUID(eid)): SimpleNamespace(organisation_id=org, collection_install_id=install.install_id)
            }
        )
        states = await _classify_entities(session, org, install, {"a": {"agent": [eid]}, "b": {"agent": [eid]}})
        assert states == {eid: "unmodified"}


class TestPipelineState:
    async def test_no_pipeline(self) -> None:
        assert await _pipeline_state(_session(), _install(), None) == (None, False)

    async def test_missing_pipeline_is_modified(self) -> None:
        pid = str(uuid.uuid4())
        session = _session(scalar_value=None)
        assert await _pipeline_state(session, _install(), pid) == (pid, True)

    async def test_modified_pipeline(self) -> None:
        pid = str(uuid.uuid4())
        install = _install()
        pipeline = SimpleNamespace(collection_install_id=uuid.uuid4())
        session = _session(scalar_value=pipeline)
        assert await _pipeline_state(session, install, pid) == (pid, True)

    async def test_unmodified_pipeline(self) -> None:
        pid = str(uuid.uuid4())
        install = _install()
        pipeline = SimpleNamespace(collection_install_id=install.install_id)
        session = _session(scalar_value=pipeline)
        assert await _pipeline_state(session, install, pid) == (pid, False)


# ---------------------------------------------------------------------------
# _legacy_pin_context
# ---------------------------------------------------------------------------


class TestLegacyPinContext:
    async def test_maps_schema_and_agent(self) -> None:
        org = uuid.uuid4()
        install = _install(org_id=org, manifest={"schemas": {"prim-s": "local-s"}, "agents": {"prim-a": "local-a"}})
        collection = _prim()
        old_pins = [
            {"slug": "s", "version": "1.0", "primitive_type": "schema"},
            {"slug": "a", "version": "1.0", "primitive_type": "agent"},
            {"slug": "x", "version": "1.0", "primitive_type": "schema"},  # unresolved
        ]

        schema_prim = _prim(slug="s")
        schema_prim.id = "prim-s"
        agent_prim = _prim(slug="a")
        agent_prim.id = "prim-a"

        async def _resolve(_s, _o, slug, _v):
            if slug == "s":
                return schema_prim, "schema"
            if slug == "a":
                return agent_prim, "agent"
            return None, "schema"

        with patch.object(upgrade, "_resolve_pin_primitive", side_effect=_resolve):
            out = await _legacy_pin_context(AsyncMock(), org, install, collection, old_pins)
        assert out == {"s": {"schema": ["local-s"]}, "a": {"agent": ["local-a"]}}

    async def test_unmapped_local_id_skipped(self) -> None:
        org = uuid.uuid4()
        install = _install(org_id=org, manifest={"schemas": {}})
        prim = _prim()
        prim.id = "pid"

        async def _resolve(_s, _o, _slug, _v):
            return prim, "schema"

        with patch.object(upgrade, "_resolve_pin_primitive", side_effect=_resolve):
            out = await _legacy_pin_context(
                AsyncMock(), org, install, _prim(), [{"slug": "s", "version": "1.0", "primitive_type": ""}]
            )
        assert out == {}

    async def test_agent_without_local_mapping_and_other_type_skipped(self) -> None:
        org = uuid.uuid4()
        install = _install(org_id=org, manifest={"agents": {}})
        agent_prim = _prim()
        agent_prim.id = "a-prim"
        wf_prim = _prim()
        wf_prim.id = "wf-prim"

        async def _resolve(_s, _o, slug, _v):
            if slug == "a":
                return agent_prim, "agent"
            return wf_prim, "workflow"

        with patch.object(upgrade, "_resolve_pin_primitive", side_effect=_resolve):
            out = await _legacy_pin_context(
                AsyncMock(),
                org,
                install,
                _prim(),
                [
                    {"slug": "a", "version": "1.0", "primitive_type": ""},
                    {"slug": "wf", "version": "1.0", "primitive_type": ""},
                ],
            )
        assert out == {}


# ---------------------------------------------------------------------------
# content extraction helpers
# ---------------------------------------------------------------------------


class TestContentHelpers:
    def test_agent_pin_content_fields(self) -> None:
        fields = _agent_pin_content_fields(
            {
                "prompt_template": "p",
                "description": "d",
                "connector_type_refs": ["c"],
                "evals": ["e"],
                "retry_policy": {"x": 1},
                "token_budget": 5,
                "input_schema": "in",
                "output_schema": "out",
            }
        )
        assert fields["prompt_template"] == "p"
        assert fields["input_schema_slug"] == "in"
        assert fields["output_schema_slug"] == "out"

    def test_agent_pin_content_fields_defaults(self) -> None:
        fields = _agent_pin_content_fields({})
        assert not fields["prompt_template"]
        assert not fields["connector_type_refs"]

    def test_workflow_bundle(self) -> None:
        assert _workflow_bundle_content({"bundle": {"agents": []}}) == {"agents": []}
        assert _workflow_bundle_content({"agents": []}) == {"agents": []}

    def test_embedded_agent_contents(self) -> None:
        wf = _prim(primitive_type="workflow", content_json={"bundle": {"agents": [{"a": 1}, "skip"]}})
        tmpl = _prim(primitive_type="pipeline_template", content_json={"agents": [{"b": 1}]})
        other = _prim(primitive_type="schema", content_json={"agents": [{"c": 1}]})
        assert _embedded_agent_contents(wf) == [{"a": 1}]
        assert _embedded_agent_contents(tmpl) == [{"b": 1}]
        assert not _embedded_agent_contents(other)

    def test_embedded_schema_contents(self) -> None:
        wf = _prim(primitive_type="workflow", content_json={"bundle": {"schemas": [{"s": 1}, "skip"]}})
        tmpl = _prim(primitive_type="pipeline_template", content_json={"schemas": [{"s": 2}]})
        assert _embedded_schema_contents(wf) == [{"s": 1}]
        assert not _embedded_schema_contents(tmpl)


# ---------------------------------------------------------------------------
# _reconcile_schema_entity / _reconcile_agent_entity
# ---------------------------------------------------------------------------


class TestReconcileSchemaEntity:
    async def test_missing_schema_warns(self) -> None:
        warnings: list[str] = []
        ok = await _reconcile_schema_entity(
            _session(), uuid.uuid4(), uuid.uuid4(), {"type": "object"}, None, None, warnings
        )
        assert ok is False
        assert warnings

    async def test_wrong_org_warns(self) -> None:
        eid = uuid.uuid4()
        warnings: list[str] = []
        session = _session(get_map={(Schema, eid): SimpleNamespace(organisation_id=uuid.uuid4())})
        ok = await _reconcile_schema_entity(session, uuid.uuid4(), eid, {"type": "object"}, None, None, warnings)
        assert ok is False
        assert warnings

    async def test_no_definition_warns(self) -> None:
        eid = uuid.uuid4()
        org = uuid.uuid4()
        warnings: list[str] = []
        session = _session(get_map={(Schema, eid): SimpleNamespace(organisation_id=org)})
        ok = await _reconcile_schema_entity(session, org, eid, None, None, None, warnings)
        assert ok is False
        assert warnings

    async def test_no_version_row_warns(self) -> None:
        eid = uuid.uuid4()
        org = uuid.uuid4()
        warnings: list[str] = []
        session = _session(
            get_map={(Schema, eid): SimpleNamespace(organisation_id=org)},
            scalar_value=None,
        )
        ok = await _reconcile_schema_entity(session, org, eid, {"type": "object"}, None, None, warnings)
        assert ok is False
        assert warnings

    async def test_success_reconciles_definition_and_fields(self) -> None:
        eid = uuid.uuid4()
        org = uuid.uuid4()
        warnings: list[str] = []
        schema = SimpleNamespace(organisation_id=org, description="old", abstract_name="old")
        version_row = SimpleNamespace(definition_json={"old": True})
        session = _session(
            get_map={(Schema, eid): schema},
            scalar_value=version_row,
        )
        ok = await _reconcile_schema_entity(session, org, eid, {"type": "object"}, "new-desc", "new-abs", warnings)
        assert ok is True
        assert version_row.definition_json == {"type": "object"}
        assert schema.description == "new-desc"
        assert schema.abstract_name == "new-abs"
        assert warnings == []

    async def test_success_without_optional_fields(self) -> None:
        eid = uuid.uuid4()
        org = uuid.uuid4()
        schema = SimpleNamespace(organisation_id=org, description="keep", abstract_name="keep")
        version_row = SimpleNamespace(definition_json={})
        session = _session(get_map={(Schema, eid): schema}, scalar_value=version_row)
        ok = await _reconcile_schema_entity(session, org, eid, {"type": "object"}, None, None, [])
        assert ok is True
        assert version_row.definition_json == {"type": "object"}
        assert schema.description == "keep"
        assert schema.abstract_name == "keep"


class TestReconcileAgentEntity:
    def _schema_map(self):
        sid = uuid.uuid4()
        return {"in": sid, "out": sid}, sid

    async def test_missing_agent_warns(self) -> None:
        warnings: list[str] = []
        ok = await _reconcile_agent_entity(_session(), uuid.uuid4(), uuid.uuid4(), {}, {}, warnings)
        assert ok is False
        assert warnings

    async def test_git_content_rejected_warns(self) -> None:
        eid = uuid.uuid4()
        org = uuid.uuid4()
        warnings: list[str] = []
        agent = SimpleNamespace(organisation_id=org)
        session = _session(get_map={(Agent, eid): agent})
        from modulo.core.pipeline_engine.git_content import GitContentRefError

        with patch(
            "modulo.core.pipeline_engine.git_content.validate_agent_git_content_values",
            side_effect=GitContentRefError("bad ref"),
        ):
            ok = await _reconcile_agent_entity(session, org, eid, {"prompt_template": "bad"}, {}, warnings)
        assert ok is False
        assert warnings

    async def test_success_reconciles_fields_and_schema_refs(self) -> None:
        eid = uuid.uuid4()
        org = uuid.uuid4()
        warnings: list[str] = []
        agent = SimpleNamespace(
            organisation_id=org,
            prompt_template="old",
            description="old",
            connector_type_refs=[],
            evals=None,
            retry_policy={},
            token_budget=None,
            input_schema_id=None,
            output_schema_id=None,
        )
        session = _session(get_map={(Agent, eid): agent})
        schema_map, sid = self._schema_map()
        content = {
            "prompt_template": "new-prompt",
            "description": "new-desc",
            "connector_type_refs": ["github"],
            "evals": ["e"],
            "retry_policy": {"max": 3},
            "token_budget": 10,
            "input_schema": "in",
            "output_schema": "out",
        }
        with patch("modulo.core.pipeline_engine.git_content.validate_agent_git_content_values"):
            ok = await _reconcile_agent_entity(session, org, eid, content, schema_map, warnings)
        assert ok is True
        assert agent.prompt_template == "new-prompt"
        assert agent.connector_type_refs == ["github"]
        assert agent.input_schema_id == sid
        assert agent.output_schema_id == sid
        assert warnings == []

    async def test_unmapped_schema_slugs_leave_refs(self) -> None:
        eid = uuid.uuid4()
        org = uuid.uuid4()
        agent = SimpleNamespace(
            organisation_id=org,
            prompt_template="",
            description=None,
            connector_type_refs=[],
            evals=None,
            retry_policy={},
            token_budget=None,
            input_schema_id="keep-in",
            output_schema_id="keep-out",
        )
        session = _session(get_map={(Agent, eid): agent})
        with patch("modulo.core.pipeline_engine.git_content.validate_agent_git_content_values"):
            ok = await _reconcile_agent_entity(
                session, org, eid, {"input_schema": "missing", "output_schema": "missing"}, {}, []
            )
        assert ok is True
        assert agent.input_schema_id == "keep-in"
        assert agent.output_schema_id == "keep-out"


# ---------------------------------------------------------------------------
# detect_collection_update
# ---------------------------------------------------------------------------


class TestDetectCollectionUpdate:
    async def test_full_delta_with_pin_type_fill(self) -> None:
        org = uuid.uuid4()
        schema_id = str(uuid.uuid4())
        install = _install(
            org_id=org,
            version="1.0",
            manifest={
                "pins": [{"slug": "s1", "version": "1.0"}],  # no primitive_type → filled
                "pin_entity_ids": {"s1": {"schema": [schema_id]}},
                "entity_fingerprints": {schema_id: "fp"},
                "pipeline_id": str(uuid.uuid4()),
            },
        )
        collection = _prim(
            org_id=org,
            version="1.0",
            update_available_version_id=None,
            manifest_pins=[{"slug": "s1", "version": "1.0"}],
        )
        newer = _prim(org_id=org, version="1.1", manifest_pins=[{"slug": "s1", "version": "1.1"}])

        session = _session(
            get_map={
                (CollectionInstall, install.install_id): install,
                (LibraryPrimitive, install.collection_id): collection,
                (Schema, uuid.UUID(schema_id)): SimpleNamespace(
                    organisation_id=org, collection_install_id=install.install_id, description=None, abstract_name=None
                ),
            },
            execute_rows=[newer],
            scalar_value=SimpleNamespace(collection_install_id=install.install_id),
        )

        newer_prim = _prim(org_id=org, version="1.1", primitive_type="schema")

        async def _resolve(_s, _o, _slug, _v):
            return newer_prim, "schema"

        async def _fp(_s, _o, _etype, _eid):
            return "fp"

        with (
            patch.object(upgrade, "_resolve_pin_primitive", side_effect=_resolve),
            patch.object(upgrade, "compute_entity_fingerprint", side_effect=_fp),
        ):
            report = await detect_collection_update(session, org, install.install_id, install.collection_id)

        assert report["update_available"] is True
        assert report["available_version"] == "1.1"
        assert report["delta"]["schemas"][0]["change"] == "changed"

    async def test_legacy_pin_context_used_when_no_map(self) -> None:
        org = uuid.uuid4()
        install = _install(
            org_id=org,
            version="1.0",
            manifest={
                "pins": [{"slug": "s1", "version": "1.0", "primitive_type": "schema"}],
                "schemas": {},  # legacy context yields nothing
            },
        )
        collection = _prim(
            org_id=org,
            version="1.0",
            update_available_version_id=None,
            manifest_pins=[{"slug": "s1", "version": "1.0"}],
        )
        newer = _prim(org_id=org, version="1.1", manifest_pins=[{"slug": "s1", "version": "1.1"}])
        session = _session(
            get_map={
                (CollectionInstall, install.install_id): install,
                (LibraryPrimitive, install.collection_id): collection,
            },
            execute_rows=[newer],
        )

        with (
            patch.object(upgrade, "_resolve_pin_primitive", new_callable=AsyncMock, return_value=(None, "schema")),
            patch.object(upgrade, "_legacy_pin_context", new_callable=AsyncMock, return_value={}) as legacy,
        ):
            report = await detect_collection_update(session, org, install.install_id, install.collection_id)
        legacy.assert_awaited_once()
        assert report["update_available"] is True

    async def test_no_update_when_newer_none(self) -> None:
        org = uuid.uuid4()
        install = _install(org_id=org, version="1.0")
        collection = _prim(org_id=org, version="1.0", update_available_version_id=None)
        session = _session(
            get_map={
                (CollectionInstall, install.install_id): install,
                (LibraryPrimitive, install.collection_id): collection,
            },
            execute_rows=[],
        )
        report = await detect_collection_update(session, org, install.install_id, install.collection_id)
        assert report["update_available"] is False
        assert report["delta"] is None
        assert report["current_version"] == "1.0"


# ---------------------------------------------------------------------------
# upgrade_collection — full reconciliation branching
# ---------------------------------------------------------------------------


def _upgrade_manifest(pin_entity_ids: dict, fingerprints: dict | None = None) -> dict:
    return {
        "pins": [],
        "pin_entity_ids": pin_entity_ids,
        "entity_fingerprints": fingerprints or {},
        "pipeline_id": str(uuid.uuid4()),
    }


class TestUpgradeCollection:
    async def test_reconciles_preserves_reports_and_warns(self) -> None:
        org = uuid.uuid4()
        pipeline_id = str(uuid.uuid4())
        s_chg, s_mod = str(uuid.uuid4()), str(uuid.uuid4())
        a_chg, a_mod = str(uuid.uuid4()), str(uuid.uuid4())
        wf_a, wf_s = str(uuid.uuid4()), str(uuid.uuid4())

        install = _install(
            org_id=org,
            version="1.0",
            manifest={
                "pins": [],
                "pin_entity_ids": {
                    "s_chg": {"schema": [s_chg]},
                    "s_mod": {"schema": [s_mod]},
                    "a_chg": {"agent": [a_chg]},
                    "a_mod": {"agent": [a_mod]},
                    "wf": {"agent": [wf_a], "schema": [wf_s]},
                },
                "entity_fingerprints": {},
                "pipeline_id": pipeline_id,
            },
        )
        collection = _prim(
            org_id=org,
            version="1.0",
            update_available_version_id=None,
            manifest_pins=[{"slug": "old-only", "version": "1.0"}],
        )
        newer = _prim(org_id=org, version="1.1")

        old_pins = [
            {"slug": "s_add", "version": "1.0", "primitive_type": "schema"},  # removed
            {"slug": "s_chg", "version": "1.0", "primitive_type": "schema"},
            {"slug": "s_mod", "version": "1.0", "primitive_type": "schema"},
            {"slug": "a_chg", "version": "1.0", "primitive_type": "agent"},
            {"slug": "a_mod", "version": "1.0", "primitive_type": "agent"},
            {"slug": "wf", "version": "1.0", "primitive_type": "workflow"},
            {"slug": "s_noprim", "version": "1.0", "primitive_type": "schema"},
        ]
        new_pins = [
            {"slug": "s_chg", "version": "1.1", "primitive_type": "schema"},
            {"slug": "s_mod", "version": "1.1", "primitive_type": "schema"},
            {"slug": "a_chg", "version": "1.1", "primitive_type": "agent"},
            {"slug": "a_mod", "version": "1.1", "primitive_type": "agent"},
            {"slug": "wf", "version": "1.1", "primitive_type": "workflow"},
            {"slug": "s_noprim", "version": "1.1", "primitive_type": "schema"},
            {"slug": "a_add", "version": "1.0", "primitive_type": "agent"},  # added
        ]
        # s_add removed → its entity is only in old; a_add added → no entities.
        pin_entity_ids = install.resolved_manifest["pin_entity_ids"]
        pin_entity_ids["s_add"] = {"schema": [str(uuid.uuid4())]}
        newer.manifest_pins = new_pins

        session = _session(
            get_map={
                (CollectionInstall, install.install_id): install,
                (LibraryPrimitive, install.collection_id): collection,
            }
        )

        def _resolve(*, new_prim_map):
            async def _side(_s, _o, slug, _v):
                if slug == "s_noprim":
                    return None, "schema"
                return new_prim_map.get(slug, _prim(org_id=org, version="1.1")), _TYPE_BY_SLUG.get(slug, "schema")

            return _side

        new_prim_map = {
            slug: _prim(org_id=org, version="1.1", primitive_type=_TYPE_BY_SLUG.get(slug, "schema"))
            for slug in ("s_chg", "s_mod", "a_chg", "a_mod", "wf")
        }
        # workflow content so embedded agent/schema align with old ids
        new_prim_map["wf"].content_json = {
            "bundle": {"agents": [{"prompt_template": "wf-a"}], "schemas": [{"definition_json": {"wf": 1}}]}
        }

        async def _classify(*_a, **_k):
            return {
                s_mod: "modified",
                a_mod: "modified",
            }

        async def _pipeline_state(*_a, **_k):
            return pipeline_id, False

        with (
            patch.object(upgrade, "find_newer_version", new_callable=AsyncMock, return_value=newer),
            patch.object(upgrade, "_resolve_pin_primitive", side_effect=_resolve(new_prim_map=new_prim_map)),
            patch.object(upgrade, "_classify_entities", side_effect=_classify),
            patch.object(upgrade, "_pipeline_state", side_effect=_pipeline_state),
            patch.object(upgrade, "compute_entity_fingerprint", new_callable=AsyncMock, return_value="fp"),
            patch.object(upgrade, "_reconcile_schema_entity", new_callable=AsyncMock, return_value=True),
            patch.object(upgrade, "_reconcile_agent_entity", new_callable=AsyncMock, return_value=True),
            patch.object(upgrade, "_snapshot_pins", return_value=old_pins),
        ):
            result = await upgrade_collection(
                session,
                org_id=org,
                created_by=uuid.uuid4(),
                install_id=install.install_id,
                collection_id=install.collection_id,
            )

        assert result["from_version"] == "1.0"
        assert result["to_version"] == "1.1"
        reconciled = {(r["slug"], r["entity_type"]) for r in result["reconciled"]}
        assert ("s_chg", "schema") in reconciled
        assert ("a_chg", "agent") in reconciled
        assert ("wf", "agent") in reconciled
        assert ("wf", "schema") in reconciled
        preserved = {(p["slug"], p["entity_type"]) for p in result["preserved"]}
        assert ("s_mod", "schema") in preserved
        assert ("a_mod", "agent") in preserved
        assert ("wf", "pipeline") in preserved
        reported = {(r["slug"], r["change"]) for r in result["reported"]}
        assert ("a_add", "added") in reported
        assert any("could not be resolved" in w for w in result["warnings"])
        # install repointed at the new version and snapshot refreshed
        assert install.collection_version == "1.1"
        assert install.collection_id == newer.id
        session.flush.assert_awaited()

    async def test_collection_invisible_raises(self) -> None:
        org = uuid.uuid4()
        install = _install(org_id=org)
        session = _session(get_map={(CollectionInstall, install.install_id): install})
        with pytest.raises(CollectionUpgradeError):
            await upgrade_collection(session, org, uuid.uuid4(), install.install_id, install.collection_id)

    async def test_no_update_raises(self) -> None:
        org = uuid.uuid4()
        install = _install(org_id=org)
        collection = _prim(org_id=org)
        session = _session(
            get_map={
                (CollectionInstall, install.install_id): install,
                (LibraryPrimitive, install.collection_id): collection,
            },
            execute_rows=[],
        )
        with pytest.raises(NoUpdateAvailableError):
            await upgrade_collection(session, org, uuid.uuid4(), install.install_id, install.collection_id)

    async def test_clears_stale_update_stamp(self) -> None:
        org = uuid.uuid4()
        install = _install(org_id=org, version="1.0", manifest={})
        collection = _prim(org_id=org, version="1.0", update_available_version_id=uuid.uuid4())
        newer = _prim(org_id=org, version="1.1")
        session = _session(
            get_map={
                (CollectionInstall, install.install_id): install,
                (LibraryPrimitive, install.collection_id): collection,
            }
        )
        with (
            patch.object(upgrade, "find_newer_version", new_callable=AsyncMock, return_value=newer),
            patch.object(upgrade, "_resolve_pin_primitive", new_callable=AsyncMock, return_value=(None, "")),
            patch.object(upgrade, "_snapshot_pins", return_value=[]),
            patch.object(upgrade, "_pin_entity_map", return_value={}),
            patch.object(upgrade, "_legacy_pin_context", new_callable=AsyncMock, return_value={}),
            patch.object(upgrade, "_classify_entities", new_callable=AsyncMock, return_value={}),
            patch.object(upgrade, "_pipeline_state", new_callable=AsyncMock, return_value=(None, False)),
        ):
            result = await upgrade_collection(
                session,
                org_id=org,
                created_by=uuid.uuid4(),
                install_id=install.install_id,
                collection_id=install.collection_id,
            )
        assert collection.update_available_version_id is None
        assert not result["reconciled"]

    async def test_embedded_misalignment_warns(self) -> None:
        org = uuid.uuid4()
        pipeline_id = str(uuid.uuid4())
        wf_a = str(uuid.uuid4())
        install = _install(
            org_id=org,
            version="1.0",
            manifest={
                "pin_entity_ids": {"wf": {"agent": [wf_a]}},
                "entity_fingerprints": {},
                "pipeline_id": pipeline_id,
            },
        )
        collection = _prim(org_id=org, version="1.0", update_available_version_id=None)
        newer = _prim(org_id=org, version="1.1")
        old_pins = [{"slug": "wf", "version": "1.0", "primitive_type": "workflow"}]
        new_pins = [{"slug": "wf", "version": "1.1", "primitive_type": "workflow"}]
        newer.manifest_pins = new_pins
        # New workflow pin embeds zero agents → alignment fails for wf_a.
        wf_prim = _prim(org_id=org, version="1.1", primitive_type="workflow", content_json={"bundle": {"agents": []}})

        session = _session(
            get_map={
                (CollectionInstall, install.install_id): install,
                (LibraryPrimitive, install.collection_id): collection,
            }
        )

        with (
            patch.object(upgrade, "find_newer_version", new_callable=AsyncMock, return_value=newer),
            patch.object(upgrade, "_resolve_pin_primitive", new_callable=AsyncMock, return_value=(wf_prim, "workflow")),
            patch.object(upgrade, "_snapshot_pins", return_value=old_pins),
            patch.object(upgrade, "_classify_entities", new_callable=AsyncMock, return_value={}),
            patch.object(upgrade, "_pipeline_state", new_callable=AsyncMock, return_value=(pipeline_id, False)),
            patch.object(upgrade, "compute_entity_fingerprint", new_callable=AsyncMock, return_value="fp"),
        ):
            result = await upgrade_collection(
                session,
                org_id=org,
                created_by=uuid.uuid4(),
                install_id=install.install_id,
                collection_id=install.collection_id,
            )
        assert any("not aligned" in w for w in result["warnings"])
        assert ("wf", "pipeline") in {(p["slug"], p["entity_type"]) for p in result["preserved"]}


# Values matching the pin primitive types used in TestUpgradeCollection.
_TYPE_BY_SLUG = {
    "s_chg": "schema",
    "s_mod": "schema",
    "s_noprim": "schema",
    "a_chg": "agent",
    "a_mod": "agent",
    "a_add": "agent",
    "wf": "workflow",
}


# ---------------------------------------------------------------------------
# upgrade_collection — branch-complete reconciliation (patched delta)
# ---------------------------------------------------------------------------


def _entry(ptype, slug, change, entities, from_v="1.0", to_v="1.1"):
    return {
        "entity_type": ptype,
        "slug": slug,
        "from_version": from_v,
        "to_version": to_v,
        "change": change,
        "action": "upgrade",
        "entities": entities,
    }


def _ent(etype, eid, modified):
    return {"entity_type": etype, "entity_id": eid, "modified": modified}


async def _run_upgrade(
    *,
    org,
    delta,
    old_pins=None,
    resolve_map=None,
    classify_states=None,
    pipeline=(None, False),
    reconcile_schema=None,
    reconcile_agent=None,
    fingerprint=None,
    new_manifest_pins=None,
    pin_entity_ids=None,
):
    install = _install(
        org_id=org,
        version="1.0",
        manifest={"pin_entity_ids": pin_entity_ids or {}, "entity_fingerprints": {}, "pipeline_id": None},
    )
    collection = _prim(org_id=org, version="1.0", update_available_version_id=None)
    newer = _prim(org_id=org, version="1.1", manifest_pins=new_manifest_pins or [])

    session = _session(
        get_map={
            (CollectionInstall, install.install_id): install,
            (LibraryPrimitive, install.collection_id): collection,
        }
    )

    async def _resolve(_s, _o, slug, _v):
        return (resolve_map or {}).get(slug, (None, ""))

    async def _classify(*_a, **_k):
        return classify_states or {}

    async def _pstate(*_a, **_k):
        return pipeline

    if callable(fingerprint):
        fp_side = fingerprint
    else:

        async def fp_side(*_a, **_k):
            return fingerprint

    rec_schema = reconcile_schema or (lambda eid: True)
    rec_agent = reconcile_agent or (lambda eid: True)

    async def _rec_schema(_s, _o, eid, *_a, **_k):
        return rec_schema(str(eid))

    async def _rec_agent(_s, _o, eid, *_a, **_k):
        return rec_agent(str(eid))

    with (
        patch.object(upgrade, "find_newer_version", new_callable=AsyncMock, return_value=newer),
        patch.object(upgrade, "_resolve_pin_primitive", side_effect=_resolve),
        patch.object(upgrade, "_classify_entities", side_effect=_classify),
        patch.object(upgrade, "_pipeline_state", side_effect=_pstate),
        patch.object(upgrade, "_snapshot_pins", return_value=old_pins or []),
        patch.object(upgrade, "build_collection_delta", return_value=delta),
        patch.object(upgrade, "compute_entity_fingerprint", side_effect=fp_side),
        patch.object(upgrade, "_reconcile_schema_entity", side_effect=_rec_schema),
        patch.object(upgrade, "_reconcile_agent_entity", side_effect=_rec_agent),
    ):
        return await upgrade_collection(
            session,
            org_id=org,
            created_by=uuid.uuid4(),
            install_id=install.install_id,
            collection_id=install.collection_id,
        )


class TestUpgradeCollectionBranches:
    async def test_schema_branch_paths(self) -> None:
        org = uuid.uuid4()
        e_ok, e_false, e_nofp, e_mod, e_other, e_x, e_u = (str(uuid.uuid4()) for _ in range(7))
        delta = {
            "schemas": [
                _entry("schema", "s_ok", "changed", [_ent("schema", e_ok, False)]),
                _entry("schema", "s_false", "changed", [_ent("schema", e_false, False)]),
                _entry("schema", "s_nofp", "changed", [_ent("schema", e_nofp, False)]),
                _entry("schema", "s_mod", "changed", [_ent("schema", e_mod, True)]),
                _entry("schema", "s_notschema", "changed", [_ent("agent", e_other, False)]),
                _entry("schema", "s_noprim", "changed", [_ent("schema", e_x, False)]),
                _entry("schema", "s_unchanged", "unchanged", [_ent("schema", e_u, False)]),
            ],
            "agents": [],
            "pipelines": [],
        }
        resolve_map = {
            s: (_prim(org_id=org, version="1.1"), "schema")
            for s in ("s_ok", "s_false", "s_nofp", "s_mod", "s_notschema")
        }
        resolve_map["s_noprim"] = (None, "schema")

        async def _fp(_s, _o, _etype, eid):
            return None if str(eid) == e_nofp else "fp"

        result = await _run_upgrade(
            org=org,
            delta=delta,
            resolve_map=resolve_map,
            reconcile_schema=lambda eid: eid in (e_ok, e_nofp),
            fingerprint=_fp,
            new_manifest_pins=[{"slug": s, "version": "1.1"} for s in resolve_map],
        )
        reconciled = {(r["slug"], r["entity_type"]) for r in result["reconciled"]}
        assert ("s_ok", "schema") in reconciled
        assert ("s_nofp", "schema") in reconciled
        preserved = {(p["slug"], p["entity_type"]) for p in result["preserved"]}
        assert ("s_mod", "schema") in preserved
        assert any("could not be resolved" in w for w in result["warnings"])

    async def test_agent_branch_paths(self) -> None:
        org = uuid.uuid4()
        a_ok, a_false, a_nofp, a_mod, a_other, a_x = (str(uuid.uuid4()) for _ in range(6))
        delta = {
            "schemas": [],
            "agents": [
                _entry("agent", "a_ok", "changed", [_ent("agent", a_ok, False)]),
                _entry("agent", "a_false", "changed", [_ent("agent", a_false, False)]),
                _entry("agent", "a_nofp", "changed", [_ent("agent", a_nofp, False)]),
                _entry("agent", "a_mod", "changed", [_ent("agent", a_mod, True)]),
                _entry("agent", "a_notagent", "changed", [_ent("schema", a_other, False)]),
                _entry("agent", "a_noprim", "changed", [_ent("agent", a_x, False)]),
            ],
            "pipelines": [],
        }
        resolve_map = {
            s: (_prim(org_id=org, version="1.1"), "agent") for s in ("a_ok", "a_false", "a_nofp", "a_mod", "a_notagent")
        }
        resolve_map["a_noprim"] = (None, "agent")

        async def _fp(_s, _o, _etype, eid):
            return None if str(eid) == a_nofp else "fp"

        result = await _run_upgrade(
            org=org,
            delta=delta,
            resolve_map=resolve_map,
            reconcile_agent=lambda eid: eid in (a_ok, a_nofp),
            fingerprint=_fp,
            new_manifest_pins=[{"slug": s, "version": "1.1"} for s in resolve_map],
        )
        reconciled = {(r["slug"], r["entity_type"]) for r in result["reconciled"]}
        assert ("a_ok", "agent") in reconciled
        assert ("a_nofp", "agent") in reconciled
        preserved = {(p["slug"], p["entity_type"]) for p in result["preserved"]}
        assert ("a_mod", "agent") in preserved
        assert any("could not be resolved" in w for w in result["warnings"])

    async def test_workflow_branch_paths(self) -> None:
        org = uuid.uuid4()
        pipe, e_wa, e_ws, e_wmod, e_wn, e_wf, e_wmis = (str(uuid.uuid4()) for _ in range(7))
        delta = {
            "schemas": [],
            "agents": [],
            "pipelines": [
                _entry(
                    "workflow",
                    "w_ok",
                    "changed",
                    [
                        _ent("pipeline", pipe, False),
                        _ent("agent", e_wa, False),
                        _ent("schema", e_ws, False),
                    ],
                ),
                _entry("workflow", "w_mod", "changed", [_ent("agent", e_wmod, True)]),
                _entry("workflow", "w_nofp", "changed", [_ent("agent", e_wn, False)]),
                _entry("workflow", "w_false", "changed", [_ent("agent", e_wf, False)]),
                _entry("workflow", "w_mis", "changed", [_ent("schema", e_wmis, False)]),
                _entry("workflow", "w_noprim", "changed", [_ent("agent", str(uuid.uuid4()), False)]),
                _entry("pipeline_template", "added", "added", []),
                _entry("widget", "w_unknown", "changed", [_ent("agent", str(uuid.uuid4()), False)]),
            ],
        }
        wf_ok = _prim(
            org_id=org,
            version="1.1",
            primitive_type="workflow",
            content_json={"bundle": {"agents": [{"prompt_template": "a"}], "schemas": [{"definition_json": {"s": 1}}]}},
        )
        wf_mod = _prim(org_id=org, version="1.1", primitive_type="workflow", content_json={"bundle": {}})
        wf_nofp = _prim(
            org_id=org, version="1.1", primitive_type="workflow", content_json={"bundle": {"agents": [{"a": 1}]}}
        )
        wf_false = _prim(
            org_id=org, version="1.1", primitive_type="workflow", content_json={"bundle": {"agents": [{"a": 1}]}}
        )
        wf_mis = _prim(org_id=org, version="1.1", primitive_type="workflow", content_json={"bundle": {"schemas": []}})
        resolve_map = {
            "w_ok": (wf_ok, "workflow"),
            "w_mod": (wf_mod, "workflow"),
            "w_nofp": (wf_nofp, "workflow"),
            "w_false": (wf_false, "workflow"),
            "w_mis": (wf_mis, "workflow"),
            "w_noprim": (None, "workflow"),
            "added": (_prim(org_id=org, version="1.1", primitive_type="pipeline_template"), "pipeline_template"),
        }

        async def _fp(_s, _o, _etype, eid):
            return None if str(eid) == e_wn else "fp"

        result = await _run_upgrade(
            org=org,
            delta=delta,
            resolve_map=resolve_map,
            reconcile_agent=lambda eid: str(eid) != e_wf,
            pipeline=(pipe, False),
            fingerprint=_fp,
            pin_entity_ids={
                "w_ok": {"agent": [e_wa], "schema": [e_ws]},
                "w_nofp": {"agent": [e_wn]},
                "w_false": {"agent": [e_wf]},
                "w_mis": {"schema": [e_wmis]},
            },
            new_manifest_pins=[{"slug": s, "version": "1.1"} for s in resolve_map],
        )
        preserved = {(p["slug"], p["entity_type"]) for p in result["preserved"]}
        assert ("w_ok", "pipeline") in preserved  # structural
        assert ("w_mod", "agent") in preserved  # modified embedded agent
        reconciled = {(r["slug"], r["entity_type"]) for r in result["reconciled"]}
        assert ("w_ok", "agent") in reconciled
        assert ("w_ok", "schema") in reconciled
        warnings = result["warnings"]
        assert any("could not be resolved" in w for w in warnings)
        assert any("not aligned" in w for w in warnings)
        reported = {(r["slug"], r["change"]) for r in result["reported"]}
        assert ("added", "added") in reported

    async def test_fills_missing_primitive_type_on_old_pins(self) -> None:
        org = uuid.uuid4()
        eid = str(uuid.uuid4())
        delta = {
            "schemas": [_entry("schema", "s1", "changed", [_ent("schema", eid, False)])],
            "agents": [],
            "pipelines": [],
        }
        resolve_map = {"s1": (_prim(org_id=org, version="1.1", primitive_type="schema"), "schema")}
        result = await _run_upgrade(
            org=org,
            delta=delta,
            old_pins=[{"slug": "s1", "version": "1.0"}],  # no primitive_type → filled
            resolve_map=resolve_map,
            new_manifest_pins=[{"slug": "s1", "version": "1.1"}],
        )
        assert ("s1", "schema") in {(r["slug"], r["entity_type"]) for r in result["reconciled"]}

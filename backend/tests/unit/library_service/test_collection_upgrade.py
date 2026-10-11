"""Unit tests for the collection upgrade service (FAR-759).

Covers the pure delta builder, the newer-version lookup (stamp preference +
version-group scan), detection/upgrade error paths, and the content
fingerprints that let an upgrade tell operator-modified entities from
unmodified ones.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.library_service.install import agent_entity_fingerprint, schema_entity_fingerprint
from modulo.core.library_service.upgrade import (
    NoUpdateAvailableError,
    UpgradeInstallNotFoundError,
    build_collection_delta,
    detect_collection_update,
    find_newer_version,
    upgrade_collection,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install(
    *,
    install_id: uuid.UUID | None = None,
    collection_id: uuid.UUID | None = None,
    version: str = "1.0",
    manifest: dict | None = None,
) -> MagicMock:
    inst = MagicMock()
    inst.install_id = install_id or uuid.uuid4()
    inst.collection_id = collection_id or uuid.uuid4()
    inst.collection_version = version
    inst.organisation_id = uuid.uuid4()
    inst.resolved_manifest = manifest if manifest is not None else {}
    return inst


def _collection(
    *,
    row_id: uuid.UUID | None = None,
    version: str = "1.0",
    version_group_id: uuid.UUID | None = None,
    update_available_version_id: uuid.UUID | None = None,
    pins: list[dict] | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = row_id or uuid.uuid4()
    row.organisation_id = uuid.uuid4()
    row.version = version
    row.version_group_id = version_group_id
    row.update_available_version_id = update_available_version_id
    row.status = "published"
    row.deleted_at = None
    row.manifest_pins = pins if pins is not None else [{"slug": "s1", "version": "1.0"}]
    row.content_json = {}
    return row


def _prim(*, row_id: uuid.UUID | None = None, version: str = "1.1", status: str = "published") -> MagicMock:
    row = MagicMock()
    row.id = row_id or uuid.uuid4()
    row.version = version
    row.status = status
    row.deleted_at = None
    return row


class _FakeScalars:
    def __init__(self, values: list) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)

    def first(self):
        return self._values[0] if self._values else None


def _scalars_result(values: list) -> MagicMock:
    result = MagicMock()
    result.scalars = MagicMock(return_value=_FakeScalars(values))
    return result


# ---------------------------------------------------------------------------
# build_collection_delta (pure)
# ---------------------------------------------------------------------------


class TestBuildCollectionDelta:
    def test_changed_pin_maps_to_entity_with_upgrade_action(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "s1", "version": "1.0", "primitive_type": "schema"}],
            new_pins=[{"slug": "s1", "version": "1.1", "primitive_type": "schema"}],
            pin_entity_ids={"s1": {"schema": ["e1"]}},
            entity_states={"e1": "unmodified"},
            pipeline_entity_id=None,
            pipeline_modified=False,
        )
        entry = delta["schemas"][0]
        assert entry["change"] == "changed"
        assert entry["action"] == "upgrade"
        assert entry["from_version"] == "1.0"
        assert entry["to_version"] == "1.1"
        assert entry["entities"][0]["entity_id"] == "e1"
        assert entry["entities"][0]["modified"] is False

    def test_modified_entity_is_not_upgradable(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "s1", "version": "1.0", "primitive_type": "schema"}],
            new_pins=[{"slug": "s1", "version": "1.1", "primitive_type": "schema"}],
            pin_entity_ids={"s1": {"schema": ["e1"]}},
            entity_states={"e1": "modified"},
            pipeline_entity_id=None,
            pipeline_modified=False,
        )
        entry = delta["schemas"][0]
        assert entry["entities"][0]["modified"] is True
        assert entry["action"] != "upgrade"

    def test_added_and_removed_pins_are_reported(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "old", "version": "1.0", "primitive_type": "agent"}],
            new_pins=[{"slug": "new", "version": "1.0", "primitive_type": "agent"}],
            pin_entity_ids={"old": {"agent": ["e-old"]}},
            entity_states={"e-old": "unmodified"},
            pipeline_entity_id=None,
            pipeline_modified=False,
        )
        by_slug = {e["slug"]: e for e in delta["agents"]}
        assert by_slug["old"]["change"] == "removed"
        assert by_slug["old"]["action"] == "report"
        assert by_slug["new"]["change"] == "added"
        assert by_slug["new"]["action"] == "report"

    def test_unchanged_pin_keeps_unchanged_action(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "s1", "version": "1.0", "primitive_type": "schema"}],
            new_pins=[{"slug": "s1", "version": "1.0", "primitive_type": "schema"}],
            pin_entity_ids={"s1": {"schema": ["e1"]}},
            entity_states={"e1": "unmodified"},
            pipeline_entity_id=None,
            pipeline_modified=False,
        )
        assert delta["schemas"][0]["change"] == "unchanged"
        assert delta["schemas"][0]["action"] == "unchanged"

    def test_workflow_pin_carries_pipeline_and_embedded_entities(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "wf", "version": "1.0", "primitive_type": "workflow"}],
            new_pins=[{"slug": "wf", "version": "1.1", "primitive_type": "workflow"}],
            pin_entity_ids={"wf": {"agent": ["a1"], "schema": ["sc1"]}},
            entity_states={"a1": "unmodified", "sc1": "unmodified"},
            pipeline_entity_id="p1",
            pipeline_modified=False,
        )
        entry = delta["pipelines"][0]
        assert entry["change"] == "changed"
        kinds = {(e["entity_type"], e["entity_id"]) for e in entry["entities"]}
        assert ("pipeline", "p1") in kinds
        assert ("agent", "a1") in kinds
        assert ("schema", "sc1") in kinds
        assert entry["action"] == "upgrade"

    def test_synthesized_pipeline_surfaces_even_without_workflow_pins(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "a1", "version": "1.0", "primitive_type": "agent"}],
            new_pins=[{"slug": "a1", "version": "1.1", "primitive_type": "agent"}],
            pin_entity_ids={"a1": {"agent": ["e1"]}},
            entity_states={"e1": "unmodified"},
            pipeline_entity_id="pipe",
            pipeline_modified=False,
        )
        pipeline_entries = delta["pipelines"]
        assert len(pipeline_entries) == 1
        entry = pipeline_entries[0]
        assert entry["slug"] is None
        assert entry["change"] == "unchanged"
        assert entry["entities"][0]["entity_id"] == "pipe"

    def test_added_agent_pin_marks_synthesized_pipeline_structural(self) -> None:
        delta = build_collection_delta(
            old_pins=[{"slug": "a1", "version": "1.0", "primitive_type": "agent"}],
            new_pins=[
                {"slug": "a1", "version": "1.0", "primitive_type": "agent"},
                {"slug": "a2", "version": "1.0", "primitive_type": "agent"},
            ],
            pin_entity_ids={"a1": {"agent": ["e1"]}},
            entity_states={"e1": "unmodified"},
            pipeline_entity_id="pipe",
            pipeline_modified=False,
        )
        pipeline_entry = next(e for e in delta["pipelines"] if e["slug"] is None)
        assert pipeline_entry["change"] == "changed"
        assert pipeline_entry["action"] == "manual"


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


class TestFingerprints:
    def test_schema_fingerprint_is_stable_and_content_sensitive(self) -> None:
        a = schema_entity_fingerprint("d", "abs", {"type": "object"})
        b = schema_entity_fingerprint("d", "abs", {"type": "object"})
        c = schema_entity_fingerprint("d", "abs", {"type": "object", "properties": {}})
        assert a == b
        assert a != c

    def test_agent_fingerprint_changes_with_prompt(self) -> None:
        agent = MagicMock()
        agent.name = "A"
        agent.description = None
        agent.prompt_template = "one"
        agent.connector_type_refs = []
        agent.evals = None
        agent.retry_policy = {}
        agent.token_budget = None
        agent.input_schema_id = None
        agent.output_schema_id = None
        agent.input_schema_version = None
        agent.output_schema_version = None
        before = agent_entity_fingerprint(agent)
        agent.prompt_template = "two"
        after = agent_entity_fingerprint(agent)
        assert before != after


# ---------------------------------------------------------------------------
# find_newer_version
# ---------------------------------------------------------------------------


class TestFindNewerVersion:
    async def test_prefers_update_available_stamp(self) -> None:
        stamped = _prim(version="1.2")
        install = _install(version="1.0")
        collection = _collection(update_available_version_id=stamped.id)
        session = AsyncMock()
        session.get = AsyncMock(return_value=stamped)

        result = await find_newer_version(session, install, collection)
        assert result is stamped

    async def test_stamp_older_than_installed_falls_back_to_scan(self) -> None:
        stamped = _prim(version="1.0")  # not newer than installed 1.0
        newer = _prim(version="1.1")
        install = _install(version="1.0")
        collection = _collection(update_available_version_id=stamped.id)

        session = AsyncMock()
        session.get = AsyncMock(return_value=stamped)
        session.execute = AsyncMock(return_value=_scalars_result([newer]))

        result = await find_newer_version(session, install, collection)
        assert result is newer

    async def test_scan_picks_highest_published_newer_version(self) -> None:
        v11 = _prim(version="1.1")
        v12 = _prim(version="1.2")
        draft = _prim(version="1.3", status="draft")
        install = _install(version="1.0")
        collection = _collection(update_available_version_id=None)

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_scalars_result([v11, v12, draft]))

        result = await find_newer_version(session, install, collection)
        assert result is v12

    async def test_none_when_installed_is_newest(self) -> None:
        same = _prim(version="1.0")
        install = _install(version="1.0")
        collection = _collection(update_available_version_id=None)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_scalars_result([same]))

        result = await find_newer_version(session, install, collection)
        assert result is None


# ---------------------------------------------------------------------------
# detect_collection_update / upgrade_collection error paths
# ---------------------------------------------------------------------------


class TestDetectAndUpdateErrors:
    async def test_detect_404_on_unknown_install(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        with pytest.raises(UpgradeInstallNotFoundError):
            await detect_collection_update(session, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    async def test_detect_no_update_when_collection_invisible(self) -> None:
        install = _install()

        async def _get(model, obj_id, **kwargs):
            from modulo.db.models.collection_install import CollectionInstall

            if model is CollectionInstall:
                return install
            return None

        session = AsyncMock()
        session.get = AsyncMock(side_effect=_get)
        report = await detect_collection_update(
            session, install.organisation_id, install.install_id, install.collection_id
        )
        assert report["update_available"] is False
        assert report["delta"] is None

    async def test_upgrade_404_on_unknown_install(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        with pytest.raises(UpgradeInstallNotFoundError):
            await upgrade_collection(session, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    async def test_upgrade_conflict_when_no_update(self) -> None:
        from modulo.db.models.collection_install import CollectionInstall
        from modulo.db.models.library_primitive import LibraryPrimitive

        install = _install()
        collection = _collection(row_id=install.collection_id)

        async def _get(model, obj_id, **kwargs):
            if model is CollectionInstall:
                return install
            if model is LibraryPrimitive:
                return collection
            return None

        session = AsyncMock()
        session.get = AsyncMock(side_effect=_get)
        # No stamped update, empty version-group scan.
        session.execute = AsyncMock(return_value=_scalars_result([]))

        with pytest.raises(NoUpdateAvailableError):
            await upgrade_collection(
                session,
                install.organisation_id,
                uuid.uuid4(),
                install.install_id,
                install.collection_id,
            )

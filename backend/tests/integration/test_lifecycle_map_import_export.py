"""Integration tests for lifecycle-map export/import against real persistence.

FAR-174 review: the BDD steps and unit tests mock the service/CRUD layers, so
nothing proved a real exported envelope round-trips through the real import
path. These tests run ``build_export_envelope`` → ``import_lifecycle_map_envelope``
on a real Postgres session and assert the resulting rows.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.lifecycle_map.import_export import build_export_envelope, import_lifecycle_map_envelope
from modulo.core.lifecycle_map.service import create_lifecycle_map, get_lifecycle_map, save_map_version
from modulo.db.crud.library_primitive import list_library_primitives

pytestmark = pytest.mark.integration


async def test_export_import_round_trip_with_real_persistence(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A real exported envelope re-imports through the real import path.

    The imported name collides with the source map, so the real dedupe renames
    it; content survives validation; a ``lifecycle_map`` library primitive is
    persisted with the full creator hex as ``author``. The envelope is
    ``format_version: 2`` and the ``versions`` array is carried through the
    round-trip.
    """
    source = await create_lifecycle_map(
        rls_session,
        org_id=test_org,
        name="SDLC Workflow",
        account_id=test_user,
        description="Q3 delivery",
        content_json={"stages": [{"id": "s1", "name": "Inbox", "stage_type": "manual"}], "edges": []},
    )

    envelope = build_export_envelope(source)
    assert envelope["primitive_type"] == "lifecycle_map"
    assert envelope["format_version"] == "2"
    assert len(envelope["versions"]) == 1
    assert envelope["versions"][0]["version"] == 1
    assert envelope["versions"][0]["stages"][0]["type"] == "manual"
    assert envelope["versions"][0]["stages"][0]["id"] == "s1"

    imported = await import_lifecycle_map_envelope(
        rls_session,
        org_id=test_org,
        account_id=test_user,
        envelope=envelope,
    )

    assert imported.id != source.id
    assert imported.name == "SDLC Workflow (imported)"
    assert imported.description == "Q3 delivery"
    assert imported.version == len(envelope["versions"])
    assert imported.content_json["stages"][0]["type"] == "manual"
    assert imported.content_json["edges"] == []

    persisted = await get_lifecycle_map(rls_session, imported.id)
    assert persisted is not None
    assert persisted.name == imported.name
    assert persisted.version == len(envelope["versions"])

    primitives = await list_library_primitives(rls_session, org_id=test_org, primitive_type="lifecycle_map")
    imported_prim = next((p for p in primitives.items if p.name == imported.name), None)
    assert imported_prim is not None
    assert imported_prim.author == test_user.hex
    assert imported_prim.visibility == "org"
    assert imported_prim.tags == ["imported"]
    assert imported_prim.content_json["export"]["name"] == imported.name
    assert imported_prim.content_json["export"]["content_json"]["stages"][0]["type"] == "manual"
    assert imported_prim.content_json["export"]["format_version"] == "2"
    assert len(imported_prim.content_json["export"]["versions"]) == 1
    assert imported_prim.content_json["export"]["versions"][0]["stages"][0]["type"] == "manual"


async def test_import_second_time_renumbers_dedupe(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Importing the same envelope twice dedupes to a numbered name."""
    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "1",
        "name": "Launch Flow",
        "description": None,
        "content_json": {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}], "edges": []},
    }

    first = await import_lifecycle_map_envelope(rls_session, org_id=test_org, account_id=test_user, envelope=envelope)
    second = await import_lifecycle_map_envelope(rls_session, org_id=test_org, account_id=test_user, envelope=envelope)

    assert first.name == "Launch Flow"
    assert second.name == "Launch Flow (imported)"


async def test_import_v2_envelope_recreates_version_chain_with_real_persistence(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A multi-version v2 envelope round-trips through the real import path.

    The envelope is assembled from real exports captured at each version of a
    map saved through ``save_map_version``, then imported through the real
    ``import_lifecycle_map_envelope``. Import replays the chain through the real
    ``save_map_version`` — bumping the counter, validating content, and
    re-deriving the junction projection against real persistence — so the
    imported map's ``version`` equals the exported chain length.
    """
    source = await create_lifecycle_map(
        rls_session,
        org_id=test_org,
        name="Release SDLC",
        account_id=test_user,
        description="Q4 delivery",
        content_json={"stages": [{"id": "s1", "name": "Inbox", "stage_type": "manual"}], "edges": []},
    )
    snapshots = [build_export_envelope(source)["versions"][0]]

    current = await save_map_version(
        rls_session,
        source.id,
        stages=[
            {"id": "s1", "name": "Inbox", "type": "manual"},
            {"id": "s2", "name": "Review", "type": "manual"},
        ],
        edges=[{"id": "e1", "source": "s1", "target": "s2"}],
        notes="second pass",
    )
    assert current is not None
    snapshots.append(build_export_envelope(current)["versions"][0])

    current = await save_map_version(
        rls_session,
        source.id,
        stages=[
            {"id": "s1", "name": "Inbox", "type": "manual"},
            {"id": "s2", "name": "Review", "type": "modulo"},
        ],
        edges=[{"id": "e1", "source": "s1", "target": "s2"}],
        notes="third pass",
    )
    assert current is not None
    snapshots.append(build_export_envelope(current)["versions"][0])

    assert [entry["version"] for entry in snapshots] == [1, 2, 3]

    envelope = build_export_envelope(current)
    envelope["versions"] = snapshots

    imported = await import_lifecycle_map_envelope(
        rls_session,
        org_id=test_org,
        account_id=test_user,
        envelope=envelope,
    )

    assert imported.id != source.id
    assert imported.name == "Release SDLC (imported)"
    assert imported.version == len(snapshots)
    assert imported.content_json["stages"][1]["type"] == "modulo"
    assert imported.content_json["edges"][0]["source"] == "s1"
    assert imported.content_json["notes"] == "third pass"

    persisted = await get_lifecycle_map(rls_session, imported.id)
    assert persisted is not None
    assert persisted.version == len(snapshots)

    primitives = await list_library_primitives(rls_session, org_id=test_org, primitive_type="lifecycle_map")
    imported_prim = next((p for p in primitives.items if p.name == imported.name), None)
    assert imported_prim is not None
    assert imported_prim.content_json["export"]["format_version"] == "2"
    assert imported_prim.content_json["export"]["versions"][-1]["version"] == len(snapshots)
    assert imported_prim.content_json["export"]["versions"][-1]["stages"][1]["type"] == "modulo"

"""Unit tests for lifecycle-map bundle export / import / library-primitive support.

FAR-174 — lifecycle maps can be exported as a JSON envelope, imported to create
a new map (validated with the same rules as an editor save), and materialized
from a ``lifecycle_map`` library primitive.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.lifecycle_map.import_export import (
    PRIMITIVE_TYPE,
    LifecycleMapBundleError,
    build_export_envelope,
    get_existing_lifecycle_map_names,
    import_lifecycle_map_envelope,
    materialize_map_from_primitive,
)
from modulo.core.lifecycle_map.validation import LifecycleMapContentError
from modulo.db.models.lifecycle_map import LifecycleMap

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


@pytest.fixture
def session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock()
    return session


def _make_map(**overrides: object) -> MagicMock:
    m = MagicMock(spec=LifecycleMap)
    m.id = overrides.get("id", _MAP_ID)
    m.organisation_id = overrides.get("organisation_id", _ORG_ID)
    m.name = overrides.get("name", "SDLC Workflow")
    m.description = overrides.get("description")
    m.owner_team_id = overrides.get("owner_team_id")
    m.visibility = overrides.get("visibility", "org")
    m.version = overrides.get("version", 1)
    m.content_json = overrides.get(
        "content_json",
        {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}]},
    )
    m.archived_at = overrides.get("archived_at")
    m.account_id = overrides.get("account_id", _ACCOUNT_ID)
    m.deleted_at = overrides.get("deleted_at")
    return m


def _make_primitive(**overrides: object) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", uuid.uuid4())
    p.primitive_type = overrides.get("primitive_type", PRIMITIVE_TYPE)
    p.name = overrides.get("name", "SDLC Workflow")
    p.description = overrides.get("description")
    p.content_json = overrides.get("content_json", {})
    return p


def _name_rows(*names: str) -> list[tuple[str]]:
    return [(name,) for name in names]


# ---------------------------------------------------------------------------
# get_existing_lifecycle_map_names
# ---------------------------------------------------------------------------


async def test_get_existing_lifecycle_map_names(session: AsyncMock) -> None:
    session.execute.return_value = _name_rows("A", "B")

    names = await get_existing_lifecycle_map_names(session, _ORG_ID)

    assert names == {"A", "B"}
    statement = session.execute.await_args.args[0]
    assert "lifecycle_maps.name" in str(statement.compile(compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# build_export_envelope
# ---------------------------------------------------------------------------


def test_build_export_envelope_returns_canonical_primitive_shape() -> None:
    lm = _make_map(
        name="SDLC Workflow",
        description="Q3",
        content_json={
            "stages": [{"id": "s1", "name": "Inbox", "stage_type": "manual", "pipeline_id": None}],
            "edges": [{"id": "e1", "source_stage_id": "s1", "target_stage_id": "s2"}],
            "notes": "n",
        },
    )

    envelope = build_export_envelope(lm)

    assert envelope["primitive_type"] == "lifecycle_map"
    assert envelope["format_version"] == "1"
    assert envelope["name"] == "SDLC Workflow"
    assert envelope["description"] == "Q3"
    # The editor alias is canonicalised to the stored shape (same as a save).
    assert envelope["content_json"]["stages"][0]["type"] == "manual"
    assert "stage_type" not in envelope["content_json"]["stages"][0]
    assert envelope["content_json"]["edges"][0]["source"] == "s1"
    assert envelope["content_json"]["edges"][0]["target"] == "s2"
    assert envelope["content_json"]["notes"] == "n"


def test_build_export_envelope_empty_content() -> None:
    envelope = build_export_envelope(_make_map(content_json={}))

    assert envelope["content_json"] == {}
    assert envelope["name"] == "SDLC Workflow"


# ---------------------------------------------------------------------------
# import_lifecycle_map_envelope
# ---------------------------------------------------------------------------


def _valid_envelope() -> dict:
    return {
        "primitive_type": PRIMITIVE_TYPE,
        "format_version": "1",
        "name": "SDLC Workflow",
        "description": "Q3 delivery",
        "content_json": {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}]},
    }


async def test_import_envelope_creates_map_and_primitive(session: AsyncMock) -> None:
    session.execute.return_value = _name_rows()
    created = _make_map(name="SDLC Workflow")
    with (
        patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created) as model_cls,
        patch("modulo.core.lifecycle_map.import_export.create_library_primitive", new=AsyncMock()) as prim_cls,
    ):
        result = await import_lifecycle_map_envelope(
            session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, envelope=_valid_envelope()
        )

    assert result is created
    assert model_cls.call_args.kwargs["name"] == "SDLC Workflow"
    assert model_cls.call_args.kwargs["description"] == "Q3 delivery"
    assert model_cls.call_args.kwargs["content_json"]["stages"][0]["type"] == "manual"
    prim_cls.assert_awaited_once()
    prim_kwargs = prim_cls.await_args.kwargs
    assert prim_kwargs["primitive_type"] == PRIMITIVE_TYPE
    assert prim_kwargs["name"] == "SDLC Workflow"
    assert prim_kwargs["content_json"]["lifecycle_map_id"] == str(created.id)
    assert prim_kwargs["content_json"]["export"]["primitive_type"] == PRIMITIVE_TYPE


async def test_import_envelope_dedupes_colliding_name(session: AsyncMock) -> None:
    session.execute.return_value = _name_rows("SDLC Workflow")
    created = _make_map(name="SDLC Workflow (imported)")
    with (
        patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created) as model_cls,
        patch("modulo.core.lifecycle_map.import_export.create_library_primitive", new=AsyncMock()),
    ):
        await import_lifecycle_map_envelope(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, envelope=_valid_envelope())

    assert model_cls.call_args.kwargs["name"] == "SDLC Workflow (imported)"


async def test_import_envelope_rejects_wrong_primitive_type(session: AsyncMock) -> None:
    envelope = _valid_envelope()
    envelope["primitive_type"] = "workflow"

    with pytest.raises(LifecycleMapBundleError):
        await import_lifecycle_map_envelope(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, envelope=envelope)


async def test_import_envelope_rejects_wrong_format_version(session: AsyncMock) -> None:
    envelope = _valid_envelope()
    envelope["format_version"] = "2"

    with pytest.raises(LifecycleMapBundleError):
        await import_lifecycle_map_envelope(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, envelope=envelope)


async def test_import_envelope_rejects_missing_name(session: AsyncMock) -> None:
    envelope = _valid_envelope()
    envelope.pop("name")

    with pytest.raises(LifecycleMapBundleError):
        await import_lifecycle_map_envelope(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, envelope=envelope)


async def test_import_envelope_rejects_missing_content_json(session: AsyncMock) -> None:
    envelope = _valid_envelope()
    envelope.pop("content_json")

    with pytest.raises(LifecycleMapBundleError):
        await import_lifecycle_map_envelope(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, envelope=envelope)


async def test_import_envelope_rejects_invalid_content_shape(session: AsyncMock) -> None:
    """Malformed graph content is rejected with the editor-save validation error."""
    session.execute.return_value = _name_rows()
    envelope = _valid_envelope()
    envelope["content_json"] = {"stages": "not-a-list"}

    with (
        patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=_make_map()),
        patch("modulo.core.lifecycle_map.import_export.create_library_primitive", new=AsyncMock()),
        pytest.raises(LifecycleMapContentError),
    ):
        await import_lifecycle_map_envelope(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, envelope=envelope)


async def test_import_envelope_uses_real_validation_and_creates_map(session: AsyncMock) -> None:
    """Round-trip proof: an exported envelope re-imports through the real create path."""
    source = _make_map(
        name="SDLC Workflow",
        content_json={"stages": [{"id": "s1", "name": "Inbox", "stage_type": "manual"}], "edges": []},
    )
    envelope = build_export_envelope(source)

    session.execute.return_value = _name_rows()
    created = _make_map(name="SDLC Workflow")
    with (
        patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created),
        patch("modulo.core.lifecycle_map.import_export.create_library_primitive", new=AsyncMock()),
    ):
        result = await import_lifecycle_map_envelope(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, envelope=envelope)

    assert result is created
    # The alias normalised on export survives the import round-trip.
    assert envelope["content_json"]["stages"][0]["type"] == "manual"


# ---------------------------------------------------------------------------
# materialize_map_from_primitive
# ---------------------------------------------------------------------------


async def test_materialize_from_primitive_uses_export_envelope(session: AsyncMock) -> None:
    session.execute.return_value = _name_rows()
    primitive = _make_primitive(
        name="Shared SDLC",
        description="shared desc",
        content_json={
            "lifecycle_map_id": str(uuid.uuid4()),
            "export": {
                "primitive_type": PRIMITIVE_TYPE,
                "format_version": "1",
                "name": "Shared SDLC",
                "description": "shared desc",
                "content_json": {"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}]},
            },
        },
    )
    created = _make_map(name="Shared SDLC")
    with patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created) as model_cls:
        result = await materialize_map_from_primitive(
            session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, primitive=primitive
        )

    assert result is created
    assert model_cls.call_args.kwargs["name"] == "Shared SDLC"
    assert model_cls.call_args.kwargs["description"] == "shared desc"
    assert model_cls.call_args.kwargs["content_json"]["stages"][0]["type"] == "manual"


async def test_materialize_from_primitive_treats_content_as_graph(session: AsyncMock) -> None:
    """Primitives whose content_json IS the graph (stages/edges) still materialize."""
    session.execute.return_value = _name_rows()
    primitive = _make_primitive(
        name="Inline Map",
        description="inline",
        content_json={"stages": [{"id": "s1", "name": "Inbox", "type": "placeholder"}], "edges": []},
    )
    created = _make_map(name="Inline Map")
    with patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created) as model_cls:
        result = await materialize_map_from_primitive(
            session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, primitive=primitive
        )

    assert result is created
    assert model_cls.call_args.kwargs["name"] == "Inline Map"
    assert model_cls.call_args.kwargs["description"] == "inline"


async def test_materialize_from_primitive_dedupes_name(session: AsyncMock) -> None:
    session.execute.return_value = _name_rows("Inline Map")
    primitive = _make_primitive(
        name="Inline Map",
        content_json={"stages": [{"id": "s1", "name": "Inbox", "type": "manual"}]},
    )
    created = _make_map(name="Inline Map (imported)")
    with patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created) as model_cls:
        await materialize_map_from_primitive(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, primitive=primitive)

    assert model_cls.call_args.kwargs["name"] == "Inline Map (imported)"


async def test_materialize_from_primitive_rejects_bad_export_envelope(session: AsyncMock) -> None:
    session.execute.return_value = _name_rows()
    primitive = _make_primitive(content_json={"export": {"name": "X"}})

    with pytest.raises(LifecycleMapBundleError):
        await materialize_map_from_primitive(session, org_id=_ORG_ID, account_id=_ACCOUNT_ID, primitive=primitive)

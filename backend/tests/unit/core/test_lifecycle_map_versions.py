"""Tests for lifecycle-map content validation, versioning and junction projection."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.lifecycle_map.service import (
    delete_lifecycle_map,
    derive_lifecycle_map_stages,
    graduate_stage,
    restore_lifecycle_map,
    save_map_version,
    update_lifecycle_map,
)
from modulo.core.lifecycle_map.validation import (
    LifecycleMapContentError,
    LifecycleMapPipelineConflictError,
    normalize_content,
)
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_PIPE_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")


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
    m.content_json = overrides.get("content_json", {})
    m.archived_at = overrides.get("archived_at")
    m.account_id = overrides.get("account_id", _ACCOUNT_ID)
    m.deleted_at = overrides.get("deleted_at")
    m.created_at = overrides.get("created_at", datetime.now(UTC))
    m.updated_at = overrides.get("updated_at", datetime.now(UTC))
    return m


def _stmt(session: AsyncMock, index: int = 0) -> object:
    return session.execute.await_args_list[index].args[0]


def _compiled_sql(statement: object) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# normalize_content — pure validation
# ---------------------------------------------------------------------------


def test_normalize_content_empty_payload_stays_empty() -> None:
    assert normalize_content({}) == {}
    assert normalize_content(None) == {}


def test_normalize_content_accepts_canonical_shape() -> None:
    content = {
        "stages": [
            {"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)},
            {"id": "s2", "name": "Approve", "type": "manual", "pipeline_id": None},
        ],
        "edges": [{"id": "e1", "source": "s1", "target": "s2", "trigger_type": "pipeline_completed"}],
        "notes": "first cut",
    }
    result = normalize_content(content)
    assert result == content


def test_normalize_content_normalizes_editor_aliases() -> None:
    result = normalize_content(
        {
            "stages": [
                {
                    "id": "s1",
                    "name": "Build",
                    "stage_type": "external",
                    "pipeline_id": str(_PIPE_ID),
                    "description": "ci",
                    "external_url": "https://ci.example.com",
                    "owner": "platform",
                    "graduated": True,
                }
            ],
            "edges": [{"id": "e1", "source_stage_id": "s1", "target_stage_id": "s2", "trigger_type": "manual"}],
        }
    )
    assert result["stages"][0]["type"] == "external"
    assert "stage_type" not in result["stages"][0]
    assert result["stages"][0]["description"] == "ci"
    assert result["stages"][0]["graduated"] is True
    assert result["edges"][0]["source"] == "s1"
    assert result["edges"][0]["target"] == "s2"
    assert "source_stage_id" not in result["edges"][0]
    assert "target_stage_id" not in result["edges"][0]


def test_normalize_content_accepts_from_to_edge_keys() -> None:
    result = normalize_content({"edges": [{"id": "e1", "from_stage_id": "a", "to_stage_id": "b"}]})
    assert result["edges"][0]["source"] == "a"
    assert result["edges"][0]["target"] == "b"


def test_normalize_content_rejects_bad_stage_type() -> None:
    with pytest.raises(LifecycleMapContentError, match="'type' must be one of"):
        normalize_content({"stages": [{"id": "s1", "name": "Build", "type": "bogus"}]})


def test_normalize_content_rejects_missing_stage_type() -> None:
    with pytest.raises(LifecycleMapContentError, match="'type' must be one of"):
        normalize_content({"stages": [{"id": "s1", "name": "Build"}]})


def test_normalize_content_rejects_invalid_pipeline_id_shape() -> None:
    with pytest.raises(LifecycleMapContentError, match="'pipeline_id' must be a string or null"):
        normalize_content({"stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": 42}]})


def test_normalize_content_rejects_non_uuid_pipeline_id() -> None:
    with pytest.raises(LifecycleMapContentError, match="not a valid UUID"):
        normalize_content({"stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": "not-a-uuid"}]})


def test_normalize_content_rejects_non_array_edges() -> None:
    with pytest.raises(LifecycleMapContentError, match="edges/transitions must be an array"):
        normalize_content({"edges": {"id": "e1"}})


def test_normalize_content_rejects_non_array_stages() -> None:
    with pytest.raises(LifecycleMapContentError, match="stages must be an array"):
        normalize_content({"stages": {"id": "s1"}})


def test_normalize_content_rejects_non_object_stage() -> None:
    with pytest.raises(LifecycleMapContentError, match="stage #0 must be an object"):
        normalize_content({"stages": ["Build"]})


def test_normalize_content_rejects_non_string_notes() -> None:
    with pytest.raises(LifecycleMapContentError, match="notes must be a string"):
        normalize_content({"stages": [], "notes": 5})


def test_normalize_content_rejects_non_object_payload() -> None:
    with pytest.raises(LifecycleMapContentError, match="content_json must be an object"):
        normalize_content(["stages"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# save_map_version
# ---------------------------------------------------------------------------


async def test_save_map_version_bumps_version_and_derives_junction(session: AsyncMock) -> None:
    lm = _make_map(version=2)
    session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=lm),
        all=MagicMock(return_value=[]),
    )

    result = await save_map_version(
        session,
        _MAP_ID,
        stages=[
            {"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)},
            {"id": "s2", "name": "Approve", "type": "manual"},
        ],
        edges=[{"id": "e1", "source": "s1", "target": "s2"}],
        notes="release cut",
    )

    assert result is lm
    assert lm.version == 3
    assert lm.content_json["stages"][0]["type"] == "modulo"
    assert lm.content_json["notes"] == "release cut"

    derived = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], LifecycleMapStage)]
    assert len(derived) == 2
    assert derived[0].map_id == _MAP_ID
    assert derived[0].position == 0
    assert derived[0].pipeline_id == _PIPE_ID
    assert derived[1].pipeline_id is None
    assert derived[1].stage_name == "Approve"
    session.flush.assert_awaited()


async def test_save_map_version_returns_none_when_missing(session: AsyncMock) -> None:
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await save_map_version(session, _MAP_ID, stages=[], edges=[], notes="") is None


async def test_save_map_version_rejects_pipeline_registered_elsewhere(session: AsyncMock) -> None:
    lm = _make_map(version=1)
    session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=lm),
        all=MagicMock(return_value=[(_PIPE_ID,)]),
    )

    with pytest.raises(LifecycleMapPipelineConflictError, match="already a stage of another active lifecycle map"):
        await save_map_version(
            session,
            _MAP_ID,
            stages=[{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)}],
            edges=[],
            notes="",
        )


async def test_save_map_version_validates_and_rejects_bad_stage_type(session: AsyncMock) -> None:
    lm = _make_map()
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    with pytest.raises(LifecycleMapContentError, match="'type' must be one of"):
        await save_map_version(
            session,
            _MAP_ID,
            stages=[{"id": "s1", "name": "Build", "type": "nope"}],
            edges=[],
            notes="",
        )


# ---------------------------------------------------------------------------
# graduate_stage
# ---------------------------------------------------------------------------


async def test_graduate_stage_marks_graduated_and_links_pipeline(session: AsyncMock) -> None:
    lm = _make_map(
        version=1,
        content_json={
            "stages": [{"id": "s1", "name": "Approve", "type": "manual", "pipeline_id": None}],
            "edges": [],
        },
    )
    session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=lm),
        all=MagicMock(return_value=[]),
    )

    result = await graduate_stage(session, _MAP_ID, stage_id="s1", pipeline_id=str(_PIPE_ID))

    assert result is lm
    assert lm.version == 2
    stage = lm.content_json["stages"][0]
    assert stage["graduated"] is True
    assert stage["type"] == "modulo"
    assert stage["pipeline_id"] == str(_PIPE_ID)


async def test_graduate_stage_returns_none_when_map_missing(session: AsyncMock) -> None:
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await graduate_stage(session, _MAP_ID, stage_id="s1", pipeline_id=str(_PIPE_ID)) is None


async def test_graduate_stage_raises_when_stage_unknown(session: AsyncMock) -> None:
    lm = _make_map(content_json={"stages": [{"id": "s1", "name": "Approve", "type": "manual"}]})
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    with pytest.raises(LifecycleMapContentError, match="stage 'missing' not found"):
        await graduate_stage(session, _MAP_ID, stage_id="missing", pipeline_id=str(_PIPE_ID))


# ---------------------------------------------------------------------------
# junction lifecycle: soft-delete frees pipelines, restore re-derives
# ---------------------------------------------------------------------------


async def test_delete_lifecycle_map_removes_junction_rows(session: AsyncMock) -> None:
    lm = _make_map()
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    assert await delete_lifecycle_map(session, _MAP_ID) is True
    assert lm.deleted_at is not None

    delete_stmt = _stmt(session, 1)
    sql = _compiled_sql(delete_stmt)
    assert "DELETE FROM lifecycle_map_stages" in sql
    assert _MAP_ID.hex in sql


async def test_restore_lifecycle_map_rederives_junction_rows(session: AsyncMock) -> None:
    lm = _make_map(
        deleted_at=datetime.now(UTC),
        content_json={"stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)}]},
    )
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    assert await restore_lifecycle_map(session, _MAP_ID) is lm
    assert lm.deleted_at is None

    derived = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], LifecycleMapStage)]
    assert len(derived) == 1
    assert derived[0].pipeline_id == _PIPE_ID


# ---------------------------------------------------------------------------
# update_lifecycle_map — content_json path validates + derives
# ---------------------------------------------------------------------------


async def test_update_lifecycle_map_normalizes_content_and_derives(session: AsyncMock) -> None:
    lm = _make_map()
    session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=lm),
        all=MagicMock(return_value=[]),
    )

    result = await update_lifecycle_map(
        session,
        _MAP_ID,
        {"content_json": {"stages": [{"id": "s1", "name": "Build", "stage_type": "modulo"}]}},
    )

    assert result is lm
    assert lm.content_json["stages"][0]["type"] == "modulo"
    assert "stage_type" not in lm.content_json["stages"][0]
    derived = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], LifecycleMapStage)]
    assert len(derived) == 1


# ---------------------------------------------------------------------------
# derive_lifecycle_map_stages — tolerant of shape-incompatible rows
# ---------------------------------------------------------------------------


async def test_derive_skips_shape_incompatible_rows(session: AsyncMock) -> None:
    lm = _make_map(
        content_json={
            "stages": [
                {"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)},
                "not-a-dict",
                {"id": "s2", "name": "External", "type": "external", "pipeline_id": "not-a-uuid"},
            ]
        }
    )

    await derive_lifecycle_map_stages(session, lm)

    derived = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], LifecycleMapStage)]
    assert len(derived) == 1
    assert derived[0].stage_id == "s1"

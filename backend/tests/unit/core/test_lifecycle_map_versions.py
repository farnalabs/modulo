"""Tests for lifecycle-map content validation, versioning and junction projection."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from modulo.api.dependencies import get_db_session
from modulo.api.routes.lifecycle_maps import (
    GraduateStageRequest,
    LifecycleMapCreate,
    LifecycleMapUpdate,
    VersionSaveRequest,
    create_lifecycle_map_endpoint,
    graduate_lifecycle_map_stage_endpoint,
    save_lifecycle_map_version_endpoint,
    update_lifecycle_map_endpoint,
)
from modulo.api.routes.lifecycle_maps import (
    router as lifecycle_maps_router,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.lifecycle_map.service import (
    _check_pipeline_uniqueness,
    create_lifecycle_map,
    delete_lifecycle_map,
    derive_lifecycle_map_stages,
    get_lifecycle_map,
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
from modulo.db.models.base import Base
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.models.organisation import Organisation

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


def test_normalize_content_rejects_overlong_stage_name() -> None:
    with pytest.raises(LifecycleMapContentError, match="'name' must be at most 200 characters"):
        normalize_content({"stages": [{"id": "s1", "name": "x" * 201, "type": "manual"}]})


def test_normalize_content_rejects_overlong_stage_id() -> None:
    with pytest.raises(LifecycleMapContentError, match="'id' must be at most 255 characters"):
        normalize_content({"stages": [{"id": "x" * 256, "name": "Build", "type": "manual"}]})


def test_normalize_content_accepts_stage_at_length_caps() -> None:
    result = normalize_content({"stages": [{"id": "x" * 255, "name": "y" * 200, "type": "manual"}]})
    assert result["stages"][0]["id"] == "x" * 255
    assert result["stages"][0]["name"] == "y" * 200


def test_normalize_content_rejects_empty_stage_name() -> None:
    with pytest.raises(LifecycleMapContentError, match="'name' must be a non-empty string"):
        normalize_content({"stages": [{"id": "s1", "name": "   ", "type": "manual"}]})


def test_normalize_content_rejects_empty_stage_id() -> None:
    with pytest.raises(LifecycleMapContentError, match="'id' must be a non-empty string"):
        normalize_content({"stages": [{"id": "", "name": "Build", "type": "manual"}]})


def test_normalize_content_rejects_non_object_edge() -> None:
    with pytest.raises(LifecycleMapContentError, match="edge/transition #0 must be an object"):
        normalize_content({"edges": ["e1"]})


def test_normalize_content_rejects_edge_without_source() -> None:
    with pytest.raises(LifecycleMapContentError, match="'source' must be a non-empty string"):
        normalize_content({"edges": [{"id": "e1", "target": "s2"}]})


def test_normalize_content_rejects_edge_without_target() -> None:
    with pytest.raises(LifecycleMapContentError, match="'target' must be a non-empty string"):
        normalize_content({"edges": [{"id": "e1", "source": "s1"}]})


def test_normalize_content_accepts_transitions_alias() -> None:
    result = normalize_content({"transitions": [{"id": "e1", "source": "s1", "target": "s2"}]})
    assert result["edges"] == [{"id": "e1", "source": "s1", "target": "s2"}]
    assert "transitions" not in result


def test_normalize_content_drops_transitions_when_edges_absent() -> None:
    result = normalize_content({"transitions": [], "notes": "plan"})
    assert result["edges"] == []
    assert "transitions" not in result
    assert result["notes"] == "plan"


def test_normalize_content_accepts_acyclic_transition_chain() -> None:
    result = normalize_content(
        {
            "stages": [
                {"id": "s1", "name": "Build", "type": "modulo"},
                {"id": "s2", "name": "Approve", "type": "manual"},
                {"id": "s3", "name": "Deploy", "type": "external"},
            ],
            "edges": [
                {"id": "e1", "source": "s1", "target": "s2"},
                {"id": "e2", "source": "s2", "target": "s3"},
            ],
        }
    )
    assert [e["target"] for e in result["edges"]] == ["s2", "s3"]


def test_normalize_content_rejects_circular_transitions() -> None:
    with pytest.raises(LifecycleMapContentError, match="transitions form a cycle"):
        normalize_content(
            {
                "stages": [
                    {"id": "s1", "name": "Build", "type": "modulo"},
                    {"id": "s2", "name": "Approve", "type": "manual"},
                ],
                "edges": [
                    {"id": "e1", "source": "s1", "target": "s2"},
                    {"id": "e2", "source": "s2", "target": "s1"},
                ],
            }
        )


def test_normalize_content_rejects_self_loop_transition() -> None:
    with pytest.raises(LifecycleMapContentError, match="transitions form a cycle"):
        normalize_content({"edges": [{"id": "e1", "source": "s1", "target": "s1"}]})


def test_normalize_content_rejects_cycle_in_transitions_alias() -> None:
    with pytest.raises(LifecycleMapContentError, match="transitions form a cycle"):
        normalize_content(
            {
                "transitions": [
                    {"id": "e1", "source": "a", "target": "b"},
                    {"id": "e2", "source": "b", "target": "a"},
                ]
            }
        )


def test_normalize_content_rejects_three_node_cycle() -> None:
    with pytest.raises(LifecycleMapContentError, match=r"cycle: s1 -> s2 -> s3 -> s1"):
        normalize_content(
            {
                "edges": [
                    {"id": "e1", "source": "s1", "target": "s2"},
                    {"id": "e2", "source": "s2", "target": "s3"},
                    {"id": "e3", "source": "s3", "target": "s1"},
                ]
            }
        )


def test_normalize_content_cycle_error_names_the_path() -> None:
    with pytest.raises(LifecycleMapContentError, match=r"cycle: s2 -> s3 -> s2"):
        normalize_content(
            {
                "edges": [
                    {"id": "e1", "source": "s1", "target": "s2"},
                    {"id": "e2", "source": "s2", "target": "s3"},
                    {"id": "e3", "source": "s3", "target": "s2"},
                ]
            }
        )


def test_normalize_content_accepts_unconnected_and_parallel_edges() -> None:
    result = normalize_content(
        {
            "edges": [
                {"id": "e1", "source": "s1", "target": "s2"},
                {"id": "e2", "source": "s3", "target": "s4"},
                {"id": "e3", "source": "s1", "target": "s2"},
            ]
        }
    )
    assert len(result["edges"]) == 3


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


async def test_graduate_stage_raises_when_map_has_no_stages(session: AsyncMock) -> None:
    lm = _make_map(content_json={})
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    with pytest.raises(LifecycleMapContentError, match="no stages; nothing to graduate"):
        await graduate_stage(session, _MAP_ID, stage_id="s1", pipeline_id=str(_PIPE_ID))


async def test_graduate_stage_raises_when_content_is_not_dict(session: AsyncMock) -> None:
    lm = _make_map(content_json=None)
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    with pytest.raises(LifecycleMapContentError, match="no stages; nothing to graduate"):
        await graduate_stage(session, _MAP_ID, stage_id="s1", pipeline_id=str(_PIPE_ID))


async def test_check_pipeline_uniqueness_skips_non_dict_stage(session: AsyncMock) -> None:
    lm = _make_map(content_json={"stages": ["not-a-dict"]})
    await _check_pipeline_uniqueness(session, lm)
    session.execute.assert_not_awaited()


async def test_check_pipeline_uniqueness_skips_invalid_pipeline_uuid(session: AsyncMock) -> None:
    lm = _make_map(
        content_json={"stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": "not-a-uuid"}]}
    )
    await _check_pipeline_uniqueness(session, lm)
    session.execute.assert_not_awaited()


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


async def test_restore_lifecycle_map_propagates_integrity_error_for_re_registered_pipeline(
    session: AsyncMock,
) -> None:
    """Restoring a map whose stage pipeline was re-registered in another active
    map fires the partial unique index; the IntegrityError must propagate so the
    restore route maps it to 409 (not a generic SQLAlchemyError 503)."""
    lm = _make_map(
        deleted_at=datetime.now(UTC),
        content_json={"stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)}]},
    )
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))
    session.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("duplicate key")))

    with pytest.raises(IntegrityError):
        await restore_lifecycle_map(session, _MAP_ID)


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
# Concurrent-save semantics — atomic version counter via SELECT ... FOR UPDATE
#
# FAR-176: two agents saving versions of the same map concurrently must never
# produce duplicate version numbers. Every version-bumping write path must fetch
# the row with FOR UPDATE so, on Postgres (READ COMMITTED), the later save
# blocks, re-reads the earlier save's committed version and bumps from there.
# These tests assert the compiled SQL carries the lock — they FAIL if a write
# path regresses back to a plain read-then-increment.
# ---------------------------------------------------------------------------


async def test_save_map_version_locks_row_for_update(session: AsyncMock) -> None:
    lm = _make_map(version=2)
    session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=lm),
        all=MagicMock(return_value=[]),
    )

    await save_map_version(
        session,
        _MAP_ID,
        stages=[{"id": "s1", "name": "Build", "type": "manual"}],
        edges=[],
        notes="",
    )

    sql = _compiled_sql(_stmt(session, 0))
    assert "FOR UPDATE" in sql.upper(), f"save must lock the map row, got: {sql}"


async def test_graduate_stage_locks_row_for_update(session: AsyncMock) -> None:
    lm = _make_map(
        version=1,
        content_json={"stages": [{"id": "s1", "name": "Approve", "type": "manual"}], "edges": []},
    )
    session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=lm),
        all=MagicMock(return_value=[]),
    )

    await graduate_stage(session, _MAP_ID, stage_id="s1", pipeline_id=None)

    sql = _compiled_sql(_stmt(session, 0))
    assert "FOR UPDATE" in sql.upper(), f"graduate must lock the map row, got: {sql}"


async def test_update_lifecycle_map_content_path_locks_row_for_update(session: AsyncMock) -> None:
    lm = _make_map(version=1)
    session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=lm),
        all=MagicMock(return_value=[]),
    )

    await update_lifecycle_map(
        session,
        _MAP_ID,
        {"content_json": {"stages": [{"id": "s1", "name": "Build", "type": "modulo"}]}},
    )

    sql = _compiled_sql(_stmt(session, 0))
    assert "FOR UPDATE" in sql.upper(), f"content update must lock the map row, got: {sql}"


async def test_update_lifecycle_map_metadata_only_reads_without_lock(session: AsyncMock) -> None:
    lm = _make_map(version=4)
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    await update_lifecycle_map(session, _MAP_ID, {"description": "renamed only"})

    sql = _compiled_sql(_stmt(session, 0))
    assert "FOR UPDATE" not in sql.upper(), f"metadata update must not take a write lock, got: {sql}"


async def test_update_lifecycle_map_bumps_version_internally_on_content_change(session: AsyncMock) -> None:
    """The service owns the version bump under the row lock — the caller must
    not pre-compute it (a stale pre-computed value reintroduces duplicates)."""
    lm = _make_map(version=2)
    session.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=lm),
        all=MagicMock(return_value=[]),
    )

    result = await update_lifecycle_map(
        session,
        _MAP_ID,
        {"content_json": {"stages": []}, "version": 99},
    )

    assert result is lm
    assert lm.version == 3, "the service must bump from the locked row, not trust a caller-supplied version"


async def test_update_lifecycle_map_metadata_only_keeps_version(session: AsyncMock) -> None:
    lm = _make_map(version=5)
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    result = await update_lifecycle_map(session, _MAP_ID, {"description": "docs only"})

    assert result is lm
    assert lm.version == 5


# ---------------------------------------------------------------------------
# Concurrent-save semantics — real DB (SQLite) pin of the documented behaviour
#
# SQLite serialises writes and ignores FOR UPDATE, so true interleaving is only
# provable on Postgres (see the integration test). These tests pin the *result*
# semantics on a real DB: two saves produce strictly increasing unique version
# numbers, the active version is last-write-wins, and a version-list read never
# observes a partially-written map.
# ---------------------------------------------------------------------------


class TestLifecycleMapConcurrentSaves:
    async def _engine(self, tmp_path: Path) -> AsyncEngine:
        return create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'lm_concurrency.db'}",
            connect_args={"timeout": 30},
            poolclass=NullPool,
        )

    async def _seed(self, engine: AsyncEngine, *, version: int, content: dict[str, object] | None = None) -> None:
        tables: list[Table] = cast(
            list[Table],
            [Organisation.__table__, LifecycleMap.__table__, LifecycleMapStage.__table__],
        )
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as s, s.begin():
            s.add(Organisation(id=_ORG_ID, name="org", slug="org"))
            s.add(
                LifecycleMap(
                    id=_MAP_ID,
                    organisation_id=_ORG_ID,
                    name="SDLC",
                    account_id=_ACCOUNT_ID,
                    visibility="org",
                    version=version,
                    content_json=content if content is not None else {"stages": [], "edges": [], "notes": ""},
                )
            )

    async def test_sequential_saves_yield_strictly_increasing_unique_versions(self, tmp_path: Path) -> None:
        engine = await self._engine(tmp_path)
        try:
            await self._seed(engine, version=1)
            maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)

            async def _save(stage_id: str) -> int:
                async with maker() as s, s.begin():
                    lm = await save_map_version(
                        s,
                        _MAP_ID,
                        stages=[{"id": stage_id, "name": stage_id, "type": "manual"}],
                        edges=[],
                        notes=stage_id,
                    )
                    assert lm is not None
                    return lm.version

            first = await _save("a")
            second = await _save("b")
            assert (first, second) == (2, 3), "saves must produce strictly increasing unique version numbers"

            async with maker() as s, s.begin():
                final = await get_lifecycle_map(s, _MAP_ID)
            assert final is not None
            assert final.version == 3
            # Last-write-wins: the active content is exactly the second save's.
            assert final.content_json["stages"] == [{"id": "b", "name": "b", "type": "manual"}]
        finally:
            await engine.dispose()

    async def test_read_during_open_write_transaction_sees_committed_snapshot(self, tmp_path: Path) -> None:
        """A version-list read concurrent with an uncommitted save must see the
        last committed snapshot — never a half-written map."""
        engine = await self._engine(tmp_path)
        try:
            await self._seed(engine, version=2)
            maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)

            async with maker() as session_a, session_a.begin():
                lm = await save_map_version(
                    session_a,
                    _MAP_ID,
                    stages=[{"id": "s1", "name": "Build", "type": "manual"}],
                    edges=[],
                    notes="v3",
                )
                assert lm is not None
                assert lm.version == 3

                # Transaction A is still open (save flushed but not committed):
                # a reader on a separate connection must observe the committed v2.
                async with maker() as session_b, session_b.begin():
                    snapshot = await get_lifecycle_map(session_b, _MAP_ID)
                assert snapshot is not None
                assert snapshot.version == 2, "reader must not see the uncommitted save"
                assert snapshot.content_json["stages"] == []

            async with maker() as s, s.begin():
                final = await get_lifecycle_map(s, _MAP_ID)
            assert final is not None
            assert final.version == 3
            assert final.content_json["stages"] == [{"id": "s1", "name": "Build", "type": "manual"}]
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# create_lifecycle_map — friendly pipeline-uniqueness pre-check
# ---------------------------------------------------------------------------
async def test_create_lifecycle_map_rejects_pipeline_registered_elsewhere(session: AsyncMock) -> None:
    """The create path runs the friendly pipeline-uniqueness pre-check, so a
    duplicate pipeline-in-active-map fails with a clear conflict error before
    the DB partial unique index fires (which would surface as a generic 409)."""
    created = _make_map(
        content_json={"stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)}]}
    )
    session.execute.return_value = MagicMock(all=MagicMock(return_value=[(_PIPE_ID,)]))

    with (
        patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created),
        pytest.raises(LifecycleMapPipelineConflictError, match="already a stage of another active lifecycle map"),
    ):
        await create_lifecycle_map(
            session,
            org_id=_ORG_ID,
            name="Release Plan",
            account_id=_ACCOUNT_ID,
            content_json={"stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)}]},
        )


async def test_create_lifecycle_map_without_pipeline_ids_skips_uniqueness_query(session: AsyncMock) -> None:
    created = _make_map(content_json={"stages": [{"id": "s1", "name": "Manual", "type": "manual"}]})

    with patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created) as model_cls:
        result = await create_lifecycle_map(
            session,
            org_id=_ORG_ID,
            name="No Pipelines",
            account_id=_ACCOUNT_ID,
            content_json={"stages": [{"id": "s1", "name": "Manual", "type": "manual"}]},
        )

    assert result is created
    model_cls.assert_called_once()
    session.add.assert_any_call(created)
    session.flush.assert_awaited()


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


# ---------------------------------------------------------------------------
# Route exception ordering — a pipeline conflict must surface as 409, not 422
#
# LifecycleMapPipelineConflictError subclasses LifecycleMapContentError, so a
# handler that lists the parent clause first swallows the conflict and returns
# 422. These tests drive the conflict through the ROUTE handlers (the exact
# except-clause ordering that was dead code), not the service layer — a
# regression that reintroduces the dead 409 branch fails here.
# ---------------------------------------------------------------------------

_CONFLICT_MSG = "pipeline(s) already a stage of another active lifecycle map"


class _RoutePrincipal:
    """Minimal tenant principal exposing the attributes the handlers read."""

    organisation_id = _ORG_ID
    account_id = _ACCOUNT_ID
    org_role = "admin"


def _route_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


async def _assert_conflict_409(handler: object, *args: object, **kwargs: object) -> None:
    with (
        patch("modulo.api.routes.lifecycle_maps.set_rls_org", AsyncMock()),
        patch("modulo.api.routes.lifecycle_maps.set_rls_user_context", AsyncMock()),
        pytest.raises(HTTPException) as excinfo,
    ):
        await handler(*args, **kwargs)  # type: ignore[misc]
    assert excinfo.value.status_code == status.HTTP_409_CONFLICT
    assert _CONFLICT_MSG in excinfo.value.detail


async def test_save_version_route_maps_pipeline_conflict_to_409() -> None:
    with patch(
        "modulo.api.routes.lifecycle_maps.save_map_version",
        AsyncMock(side_effect=LifecycleMapPipelineConflictError(_CONFLICT_MSG)),
    ):
        await _assert_conflict_409(
            save_lifecycle_map_version_endpoint,
            lifecycle_map_id=_MAP_ID,
            req=VersionSaveRequest(stages=[], edges=[]),
            session=_route_session(),
            principal=_RoutePrincipal(),
        )


async def test_update_route_maps_pipeline_conflict_to_409() -> None:
    with (
        patch(
            "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
            AsyncMock(return_value=_make_map()),
        ),
        patch(
            "modulo.api.routes.lifecycle_maps.update_lifecycle_map",
            AsyncMock(side_effect=LifecycleMapPipelineConflictError(_CONFLICT_MSG)),
        ),
    ):
        await _assert_conflict_409(
            update_lifecycle_map_endpoint,
            lifecycle_map_id=_MAP_ID,
            req=LifecycleMapUpdate(content_json={"stages": []}),
            session=_route_session(),
            principal=_RoutePrincipal(),
        )


async def test_graduate_route_maps_pipeline_conflict_to_409() -> None:
    with patch(
        "modulo.api.routes.lifecycle_maps.graduate_stage",
        AsyncMock(side_effect=LifecycleMapPipelineConflictError(_CONFLICT_MSG)),
    ):
        await _assert_conflict_409(
            graduate_lifecycle_map_stage_endpoint,
            lifecycle_map_id=_MAP_ID,
            version_id=_MAP_ID,
            stage_id="s1",
            req=GraduateStageRequest(pipeline_id=str(_PIPE_ID)),
            session=_route_session(),
            principal=_RoutePrincipal(),
        )


async def test_create_route_maps_pipeline_conflict_to_409() -> None:
    with patch(
        "modulo.api.routes.lifecycle_maps.create_lifecycle_map",
        AsyncMock(side_effect=LifecycleMapPipelineConflictError(_CONFLICT_MSG)),
    ):
        await _assert_conflict_409(
            create_lifecycle_map_endpoint,
            req=LifecycleMapCreate(name="Release Plan", content_json={"stages": []}),
            session=_route_session(),
            principal=_RoutePrincipal(),
        )


async def test_save_version_route_maps_content_error_to_422() -> None:
    """A plain content-validation error still maps to 422 after the conflict
    clause moved ahead of the parent class — the parent clause must remain
    reachable for genuine shape errors."""
    with (
        patch(
            "modulo.api.routes.lifecycle_maps.save_map_version",
            AsyncMock(side_effect=LifecycleMapContentError("content_json.stages must be an array")),
        ),
        patch("modulo.api.routes.lifecycle_maps.set_rls_org", AsyncMock()),
        patch("modulo.api.routes.lifecycle_maps.set_rls_user_context", AsyncMock()),
        pytest.raises(HTTPException) as excinfo,
    ):
        await save_lifecycle_map_version_endpoint(
            lifecycle_map_id=_MAP_ID,
            req=VersionSaveRequest(stages=[], edges=[]),
            session=_route_session(),
            principal=_RoutePrincipal(),
        )
    assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# PUT route round-trip — prove-the-fix regression for the update 500
#
# The map PUT committed + version-bumped correctly but returned HTTP 500: the
# ``updated_at`` column carries ``onupdate=func.current_timestamp()``, so the
# flush leaves it as an expired postfetch attribute, and reading it via
# ``LifecycleMapResponse.model_validate`` AFTER the ``session.begin()`` commit
# triggered a lazy refresh on a session with ``autobegin=False``
# (``InvalidRequestError``). The fix refreshes the map inside the transaction.
# This test drives the REAL route against a REAL SQLite session and asserts a
# 200 + the updated fields + the version bump — it FAILS (500) without the fix.
# ---------------------------------------------------------------------------


def _make_route_app(engine: AsyncEngine) -> FastAPI:
    app = FastAPI()
    app.include_router(lifecycle_maps_router)
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_session
    return app


class TestUpdateLifecycleMapRoute:
    async def test_put_returns_200_with_updated_fields_and_version_bump(self) -> None:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        tables: list[Table] = cast(
            list[Table],
            [Organisation.__table__, LifecycleMap.__table__, LifecycleMapStage.__table__],
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
            async with async_sessionmaker(engine, expire_on_commit=False, autobegin=False)() as s, s.begin():
                s.add(Organisation(id=_ORG_ID, name="org", slug="org"))
                s.add(
                    LifecycleMap(
                        id=_MAP_ID,
                        organisation_id=_ORG_ID,
                        name="SDLC",
                        account_id=_ACCOUNT_ID,
                        visibility="org",
                        version=1,
                        content_json={"stages": []},
                    )
                )

            app = _make_route_app(engine)
            with TestClient(app, raise_server_exceptions=False) as c:
                resp = c.put(
                    f"/api/v1/lifecycle-maps/{_MAP_ID}",
                    json={
                        "name": "SDLC v2",
                        "content_json": {
                            "stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": str(_PIPE_ID)}]
                        },
                    },
                )
            app.dependency_overrides.clear()

            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["id"] == str(_MAP_ID)
            assert body["name"] == "SDLC v2"
            assert body["version"] == 2
            assert body["content_json"]["stages"][0]["name"] == "Build"
        finally:
            await engine.dispose()

    async def test_put_without_content_keeps_version(self) -> None:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        tables: list[Table] = cast(
            list[Table],
            [Organisation.__table__, LifecycleMap.__table__, LifecycleMapStage.__table__],
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
            async with async_sessionmaker(engine, expire_on_commit=False, autobegin=False)() as s, s.begin():
                s.add(Organisation(id=_ORG_ID, name="org", slug="org"))
                s.add(
                    LifecycleMap(
                        id=_MAP_ID,
                        organisation_id=_ORG_ID,
                        name="SDLC",
                        account_id=_ACCOUNT_ID,
                        visibility="org",
                        version=3,
                        content_json={"stages": []},
                    )
                )

            app = _make_route_app(engine)
            with TestClient(app, raise_server_exceptions=False) as c:
                resp = c.put(f"/api/v1/lifecycle-maps/{_MAP_ID}", json={"description": "renamed only"})
            app.dependency_overrides.clear()

            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["description"] == "renamed only"
            assert body["version"] == 3  # no content_json -> no bump
        finally:
            await engine.dispose()

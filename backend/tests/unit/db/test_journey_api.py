"""Lifecycle-map journeys API tests (FAR-144).

Two layers, mirroring the repo's existing patterns:

* service-layer tests against an in-memory SQLite database with the REAL
  models (no mocks of the functions under test) â€” list correctness, keyset
  pagination round-trip, kind/ref filter, team-scope owner_team_id, journey
  detail with run history, empty history after a run purge.
* route-layer tests through a minimal FastAPI app with a mocked session â€”
  permission denial (403 without run.list), 404 for a missing map/journey,
  team-scope wiring, and DB-failure â†' 503 mapping.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.dependencies import get_db_session
from modulo.api.routes.lifecycle_maps import router as lifecycle_maps_router
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.lifecycle_map.journeys import (
    decode_cursor,
    encode_cursor,
    get_map_journey,
    list_journey_runs,
    list_map_journeys,
)
from modulo.db.models.base import Base
from modulo.db.models.journey import Journey
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run
from tests.unit.api.mock_session import configure_mock_session

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_MAP2 = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_STAGE_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_OTHER_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000c2")

_TABLES: list[Table] = cast(
    list[Table],
    [
        Organisation.__table__,
        LifecycleMap.__table__,
        LifecycleMapStage.__table__,
        Journey.__table__,
        Run.__table__,
    ],
)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_org(session: AsyncSession, org_id: uuid.UUID = _ORG) -> None:
    session.add(Organisation(id=org_id, name="test org", slug=f"test-{org_id}"))
    await session.flush()


async def _seed_map(
    session: AsyncSession,
    *,
    map_id: uuid.UUID = _MAP,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
) -> LifecycleMap:
    lm = LifecycleMap(
        id=map_id,
        organisation_id=_ORG,
        name="SDLC",
        account_id=_ACCOUNT,
        owner_team_id=owner_team_id,
        visibility=visibility,
        version=2,
        content_json={"stages": []},
    )
    session.add(lm)
    await session.flush()
    return lm


async def _seed_stage(
    session: AsyncSession,
    *,
    map_id: uuid.UUID = _MAP,
    stage_id: str = "review",
    pipeline_id: uuid.UUID | None = _STAGE_PIPELINE,
    position: int = 0,
) -> LifecycleMapStage:
    stage = LifecycleMapStage(
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        map_id=map_id,
        version=2,
        stage_id=stage_id,
        stage_name="Review",
        position=position,
        stage_type="modulo",
        pipeline_id=pipeline_id,
    )
    session.add(stage)
    await session.flush()
    return stage


async def _seed_journey(
    session: AsyncSession,
    *,
    kind: str,
    ref: str,
    org_id: uuid.UUID = _ORG,
    map_id: uuid.UUID | None = None,
    updated_at: datetime | None = None,
    owner_team_id: uuid.UUID | None = None,
    latest_status: str | None = None,
    latest_provenance: str | None = None,
    run_count: int = 0,
    latest_terminal_run_id: uuid.UUID | None = None,
    stage_id: str | None = None,
    canonical_work_item_id: uuid.UUID | None = None,
) -> Journey:
    from modulo.db.lifecycle_refs import canonical_work_item_id as _canonical

    journey = Journey(
        organisation_id=org_id,
        owner_team_id=owner_team_id,
        kind=kind,
        ref=ref,
        canonical_work_item_id=canonical_work_item_id or _canonical(org_id, kind, ref),
        latest_terminal_run_id=latest_terminal_run_id,
        map_id=map_id,
        stage_id=stage_id,
        stage_name="Review" if stage_id else None,
        position=0 if stage_id else None,
        map_version=2 if stage_id else None,
        latest_status=latest_status,
        latest_provenance=latest_provenance,
        run_count=run_count,
    )
    if updated_at is not None:
        journey.updated_at = updated_at
    session.add(journey)
    await session.flush()
    return journey


async def _seed_run(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID = _STAGE_PIPELINE,
    kind: str = "github_issue",
    ref: str = "a/b#5",
    completed_at: datetime | None = None,
    status: str = "complete",
    work_item_id: uuid.UUID | None = None,
) -> Run:
    run = Run(
        organisation_id=_ORG,
        pipeline_id=pipeline_id,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        status=status,
        run_number=len((await session.execute(select(Run))).all()) + 1,
        input_hash="hash",
        langgraph_thread_id=f"thread-{uuid.uuid4()}",
        work_item_refs=[{"kind": kind, "ref": ref, "source": "derived"}],
        work_item_id=work_item_id,
        completed_at=completed_at,
    )
    session.add(run)
    await session.flush()
    return run


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Service layer â€” list
# ---------------------------------------------------------------------------


class TestListMapJourneys:
    async def test_list_returns_map_scoped_journeys(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        # Journey with stage identity on this map.
        stage_journey = await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP)
        # Journey with NO stage identity, but its (kind, ref) ran through the map's stage pipeline.
        orphan_journey = await _seed_journey(session, kind="linear", ref="FAR-1", map_id=None)
        await _seed_run(session, kind="linear", ref="FAR-1")
        # Journey on a DIFFERENT map must not appear.
        await _seed_map(session, map_id=_MAP2)
        await _seed_stage(session, map_id=_MAP2, pipeline_id=_OTHER_PIPELINE)
        other_journey = await _seed_journey(session, kind="jira", ref="JIRA-1", map_id=_MAP2)
        # Journey with no stage identity and no matching run must not appear.
        await _seed_journey(session, kind="jira", ref="JIRA-9", map_id=None)

        items, next_cursor = await list_map_journeys(session, map_id=_MAP)

        ids = {j.id for j in items}
        assert ids == {stage_journey.id, orphan_journey.id}
        assert other_journey.id not in ids
        assert next_cursor is None

    async def test_list_orphan_only_matches_its_own_kind_ref(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        # A run on this map's stage pipeline stamps (kind=github_issue, ref="a/b#5").
        await _seed_run(session)
        # A journey with the SAME kind but a DIFFERENT ref is NOT orphan-relevant.
        await _seed_journey(session, kind="github_issue", ref="x/y#9", map_id=None)

        items, _ = await list_map_journeys(session, map_id=_MAP)
        assert items == []

    async def test_list_kind_ref_filter_returns_single_journey(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        target = await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP)
        await _seed_journey(session, kind="github_issue", ref="c/d#7", map_id=_MAP)

        items, _ = await list_map_journeys(session, map_id=_MAP, kind="github_issue", ref="a/b#5")
        assert [j.id for j in items] == [target.id]

    async def test_list_kind_filter_without_ref(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        a = await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP)
        b = await _seed_journey(session, kind="github_issue", ref="c/d#7", map_id=_MAP)
        await _seed_journey(session, kind="linear", ref="FAR-1", map_id=_MAP)

        items, _ = await list_map_journeys(session, map_id=_MAP, kind="github_issue")
        assert {j.id for j in items} == {a.id, b.id}

    async def test_list_owner_team_id_filter(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        team_a = await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP, owner_team_id=_TEAM_A)
        await _seed_journey(session, kind="github_issue", ref="c/d#7", map_id=_MAP, owner_team_id=_TEAM_B)

        items, _ = await list_map_journeys(session, map_id=_MAP, owner_team_id=_TEAM_A)
        assert [j.id for j in items] == [team_a.id]


class TestListPagination:
    async def test_pagination_respects_limit_and_cursor_roundtrip(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        created = []
        for i in range(5):
            journey = await _seed_journey(
                session,
                kind="github_issue",
                ref=f"a/b#{i}",
                map_id=_MAP,
                updated_at=datetime(2026, 1, 1 + i, 0, 0, tzinfo=UTC),
            )
            created.append(journey)
        # expected order: newest updated_at first
        expected_order = [j.id for j in reversed(created)]

        page1, cursor1 = await list_map_journeys(session, map_id=_MAP, limit=2)
        assert [j.id for j in page1] == expected_order[:2]
        assert cursor1 is not None

        page2, cursor2 = await list_map_journeys(session, map_id=_MAP, limit=2, cursor=cursor1)
        assert [j.id for j in page2] == expected_order[2:4]
        assert cursor2 is not None

        page3, cursor3 = await list_map_journeys(session, map_id=_MAP, limit=2, cursor=cursor2)
        assert [j.id for j in page3] == expected_order[4:]
        assert cursor3 is None

    def test_cursor_encode_decode_roundtrip(self) -> None:
        ts = datetime(2026, 3, 15, 12, 30, 45, tzinfo=UTC)
        jid = uuid.uuid4()
        cursor = encode_cursor(ts, jid)
        decoded_ts, decoded_id = decode_cursor(cursor)
        assert decoded_ts == ts
        assert decoded_id == jid

    def test_cursor_decode_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="invalid pagination cursor"):
            decode_cursor("not-a-cursor")


class TestGetMapJourney:
    async def test_get_stage_identity_journey(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        journey = await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP)

        found = await get_map_journey(session, map_id=_MAP, kind="github_issue", ref="a/b#5")
        assert found is not None
        assert found.id == journey.id

    async def test_get_orphan_journey_with_matching_run(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        journey = await _seed_journey(session, kind="linear", ref="FAR-1", map_id=None)
        await _seed_run(session, kind="linear", ref="FAR-1")

        found = await get_map_journey(session, map_id=_MAP, kind="linear", ref="FAR-1")
        assert found is not None
        assert found.id == journey.id

    async def test_get_journey_on_other_map_returns_none(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP2)

        found = await get_map_journey(session, map_id=_MAP, kind="github_issue", ref="a/b#5")
        assert found is None

    async def test_get_orphan_journey_without_matching_run_returns_none(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        await _seed_journey(session, kind="linear", ref="FAR-9", map_id=None)

        found = await get_map_journey(session, map_id=_MAP, kind="linear", ref="FAR-9")
        assert found is None

    async def test_get_missing_journey_returns_none(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        found = await get_map_journey(session, map_id=_MAP, kind="linear", ref="FAR-404")
        assert found is None

    async def test_get_respects_owner_team_id(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP, owner_team_id=_TEAM_B)

        found = await get_map_journey(session, map_id=_MAP, kind="github_issue", ref="a/b#5", owner_team_id=_TEAM_A)
        assert found is None


class TestJourneyRunHistory:
    async def test_detail_returns_recent_runs_newest_first(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        journey = await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP)
        older = await _seed_run(session, completed_at=datetime(2026, 1, 2, tzinfo=UTC))
        newer = await _seed_run(session, completed_at=datetime(2026, 1, 3, tzinfo=UTC))
        # Unrelated journey/run must not leak in.
        await _seed_run(session, kind="jira", ref="JIRA-1", completed_at=datetime(2026, 1, 4, tzinfo=UTC))

        runs = await list_journey_runs(session, journey=journey)
        assert [r.id for r in runs] == [newer.id, older.id]

    async def test_detail_matches_by_work_item_id_anchor(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        from modulo.db.lifecycle_refs import canonical_work_item_id as _canonical

        journey = await _seed_journey(session, kind="linear", ref="FAR-1", map_id=_MAP)
        anchor = _canonical(_ORG, "linear", "FAR-1")
        run = await _seed_run(
            session,
            kind="linear",
            ref="FAR-1",
            work_item_id=anchor,
            completed_at=datetime(2026, 1, 5, tzinfo=UTC),
        )

        runs = await list_journey_runs(session, journey=journey)
        assert [r.id for r in runs] == [run.id]

    async def test_empty_history_when_runs_purged(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        journey = await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP)

        runs = await list_journey_runs(session, journey=journey)
        assert runs == []

    async def test_never_completed_runs_sort_last(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map(session)
        await _seed_stage(session)
        journey = await _seed_journey(session, kind="github_issue", ref="a/b#5", map_id=_MAP)
        completed = await _seed_run(
            session,
            status="complete",
            completed_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        never_completed = await _seed_run(session, status="running", completed_at=None)

        runs = await list_journey_runs(session, journey=journey)
        assert [r.id for r in runs] == [completed.id, never_completed.id]


# ---------------------------------------------------------------------------
# Route layer â€” permissions, team scope, errors, wire shape
# ---------------------------------------------------------------------------


def _make_app(*, org_role: str = "admin") -> FastAPI:
    app = FastAPI()
    app.include_router(lifecycle_maps_router)
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role=org_role,
    )
    return app


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def mock_session() -> AsyncMock:
    return _make_mock_session()


@pytest.fixture
def client(mock_session: AsyncMock) -> AsyncGenerator[TestClient, None]:
    app = _make_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_map_mock(*, visibility: str = "org", owner_team_id: uuid.UUID | None = None) -> MagicMock:
    m = MagicMock()
    m.id = _MAP
    m.organisation_id = _ORG
    m.visibility = visibility
    m.owner_team_id = owner_team_id
    return m


def _make_journey_mock(**overrides: Any) -> MagicMock:
    j = MagicMock()
    j.kind = overrides.get("kind", "github_issue")
    j.ref = overrides.get("ref", "a/b#5")
    j.canonical_work_item_id = overrides.get("canonical_work_item_id", uuid.uuid4())
    j.map_id = overrides.get("map_id", _MAP)
    j.map_version = overrides.get("map_version", 2)
    j.stage_id = overrides.get("stage_id", "review")
    j.stage_name = overrides.get("stage_name", "Review")
    j.position = overrides.get("position", 0)
    j.latest_status = overrides.get("latest_status", "running")
    j.latest_provenance = overrides.get("latest_provenance", "manual")
    j.run_count = overrides.get("run_count", 3)
    j.latest_terminal_run_id = overrides.get("latest_terminal_run_id", uuid.uuid4())
    j.updated_at = overrides.get("updated_at", datetime(2026, 1, 1, tzinfo=UTC))
    return j


class TestRoutes:
    def test_list_requires_run_list_permission(self, mock_session: AsyncMock) -> None:
        app = _make_app(org_role="viewer")  # viewer < runner â†' run.list denied

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys")
        app.dependency_overrides.clear()

        assert resp.status_code == 403

    def test_list_unknown_map_404(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        with (
            patch("modulo.api.routes.lifecycle_maps.get_lifecycle_map", new=AsyncMock(return_value=None)),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys")
        app.dependency_overrides.clear()

        assert resp.status_code == 404

    def test_list_team_scoped_map_passes_owner_team_id(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        list_mock = AsyncMock(return_value=([], None))
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(return_value=_make_map_mock(visibility="team", owner_team_id=_TEAM_A)),
            ),
            patch("modulo.api.routes.lifecycle_maps.list_map_journeys", new=list_mock),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert list_mock.await_args.kwargs["owner_team_id"] == _TEAM_A

    def test_list_org_scoped_map_passes_none_owner_team_id(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        list_mock = AsyncMock(return_value=([], None))
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(return_value=_make_map_mock(visibility="org")),
            ),
            patch("modulo.api.routes.lifecycle_maps.list_map_journeys", new=list_mock),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert list_mock.await_args.kwargs["owner_team_id"] is None

    def test_list_db_failure_maps_to_503(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(return_value=_make_map_mock()),
            ),
            patch(
                "modulo.api.routes.lifecycle_maps.list_map_journeys",
                new=AsyncMock(side_effect=SQLAlchemyError("boom")),
            ),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys")
        app.dependency_overrides.clear()

        assert resp.status_code == 503

    def test_list_wire_shape(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        journey = _make_journey_mock()
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(return_value=_make_map_mock()),
            ),
            patch(
                "modulo.api.routes.lifecycle_maps.list_map_journeys",
                new=AsyncMock(return_value=([journey], "cursor-x")),
            ),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["next_cursor"] == "cursor-x"
        item = body["items"][0]
        assert item["kind"] == "github_issue"
        assert item["ref"] == "a/b#5"
        assert item["status"] == "running"
        assert item["provenance"] == "manual"
        assert item["run_count"] == 3
        assert item["latest_run_id"] == str(journey.latest_terminal_run_id)
        assert item["current_stage"] == {
            "map_id": str(_MAP),
            "version": 2,
            "stage_id": "review",
            "stage_name": "Review",
            "position": 0,
        }

    def test_list_null_stage_wire_shape(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        journey = _make_journey_mock(
            map_id=None, stage_id=None, stage_name=None, position=None, latest_status=None, latest_provenance=None
        )
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(return_value=_make_map_mock()),
            ),
            patch("modulo.api.routes.lifecycle_maps.list_map_journeys", new=AsyncMock(return_value=([journey], None))),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys")
        app.dependency_overrides.clear()

        body = resp.json()
        item = body["items"][0]
        assert item["current_stage"] is None
        assert item["status"] is None

    def test_detail_unknown_journey_404(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(return_value=_make_map_mock()),
            ),
            patch("modulo.api.routes.lifecycle_maps.get_map_journey", new=AsyncMock(return_value=None)),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys/github_issue/simple-ref")
        app.dependency_overrides.clear()

        assert resp.status_code == 404

    def test_detail_wire_shape_with_run_history(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        journey = _make_journey_mock()
        run = MagicMock()
        run.id = uuid.uuid4()
        run.status = "complete"
        run.completed_at = datetime(2026, 1, 5, tzinfo=UTC)
        run.trigger_type = "manual"
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(return_value=_make_map_mock()),
            ),
            patch("modulo.api.routes.lifecycle_maps.get_map_journey", new=AsyncMock(return_value=journey)),
            patch("modulo.api.routes.lifecycle_maps.list_journey_runs", new=AsyncMock(return_value=[run])),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys/github_issue/simple-ref")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "github_issue"
        assert body["runs"] == [
            {
                "run_id": str(run.id),
                "status": "complete",
                "completed_at": "2026-01-05T00:00:00Z",
                "provenance": "manual",
            }
        ]

    def test_detail_empty_run_history(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(return_value=_make_map_mock()),
            ),
            patch(
                "modulo.api.routes.lifecycle_maps.get_map_journey",
                new=AsyncMock(return_value=_make_journey_mock()),
            ),
            patch("modulo.api.routes.lifecycle_maps.list_journey_runs", new=AsyncMock(return_value=[])),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys/github_issue/simple-ref")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["runs"] == []

    def test_detail_db_failure_maps_to_503(self, mock_session: AsyncMock) -> None:
        app = _make_app()

        async def override_session() -> AsyncGenerator[AsyncSession, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        with (
            patch(
                "modulo.api.routes.lifecycle_maps.get_lifecycle_map",
                new=AsyncMock(side_effect=SQLAlchemyError("boom")),
            ),
            TestClient(app) as c,
        ):
            resp = c.get(f"/api/v1/lifecycle-maps/{_MAP}/journeys/github_issue/simple-ref")
        app.dependency_overrides.clear()

        assert resp.status_code == 503

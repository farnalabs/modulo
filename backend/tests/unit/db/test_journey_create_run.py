"""create_run journey stamping + reserved-key strip matrix (FAR-142).

These tests exercise the REAL ``create_run`` path against an in-memory SQLite
database (no mocks of the function under test):

  * reserved-key strip-before-hash â€” same logical payload with different
    injected reserved keys produces IDENTICAL input_hash, and the keys never
    reach the stored input_payload.
  * forge prevention per input path â€” a payload carrying ``_work_item_id``
    through the raw webhook passthrough (empty payload_mapping) or a manual
    POST /runs body never results in a forged work_item_id (the run gets its
    deterministic floor id instead).
  * the create_run path matrix â€” manual / webhook / cron / agent_signal /
    parent-adoption across mint modes (floor, adopted-from-parent, explicit,
    no-refs), asserting work_item_id is set once and refs are stamped when
    provided.
  * journey hydration â€” a ref creates a journey row keyed by the deterministic
    canonical id; a duplicate (org, kind, ref) is an ON CONFLICT no-op; no
    refs means no journey row; the journey survives a run purge (no FK); a
    forced stamp failure is fail-open (create_run still succeeds).
"""

import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.trigger_engine import TriggerEngine, _apply_payload_mapping
from modulo.db.crud.run import _floor_work_item_id, create_run
from modulo.db.crud.variant_group import run_variant_weighted
from modulo.db.lifecycle_refs import canonical_work_item_id
from modulo.db.models.base import Base
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.team import Team
from modulo.db.models.variant_group import VariantGroup

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_TEAM = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

_REF_ENTRIES = [{"kind": "GitHub Issue", "ref": "https://github.com/a/b/pull/5", "source": "derived"}]

_TABLES: list[Table] = cast(
    list[Table],
    [
        Organisation.__table__,
        Pipeline.__table__,
        Team.__table__,
        Run.__table__,
        PipelineSnapshot.__table__,
        Journey.__table__,
        VariantGroup.__table__,
        EvalDefinition.__table__,
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


async def _seed_team(session: AsyncSession, team_id: uuid.UUID = _TEAM) -> None:
    session.add(Team(id=team_id, organisation_id=_ORG, name="team-a", account_id=_ORG))
    await session.flush()


async def _seed_pipeline(
    session: AsyncSession, *, owner_team_id: uuid.UUID | None, pipeline_id: uuid.UUID = _PIPELINE
) -> None:
    session.add(
        Pipeline(
            id=pipeline_id,
            organisation_id=_ORG,
            name="pipeline",
            account_id=_ORG,
            visibility="team" if owner_team_id is not None else "org",
            owner_team_id=owner_team_id,
        )
    )
    await session.flush()


async def _create(
    session: AsyncSession,
    *,
    org_id: uuid.UUID = _ORG,
    trigger_type: str = "manual",
    input_payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Run:
    return await create_run(
        session,
        org_id=org_id,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type=trigger_type,
        input_payload=input_payload or {},
        **kwargs,
    )


async def _journey_for(session: AsyncSession, kind: str, ref: str, org_id: uuid.UUID = _ORG) -> Journey | None:
    return (
        await session.execute(
            select(Journey).where(
                Journey.organisation_id == org_id,
                Journey.kind == kind,
                Journey.ref == ref,
            )
        )
    ).scalar_one_or_none()


class TestReservedKeyStripBeforeHash:
    async def test_injected_reserved_keys_produce_identical_hash(self, session: AsyncSession) -> None:
        await _seed_org(session)
        base = {"data": 1, "nested": {"k": "v"}}
        forged = [
            {**base, "_work_item_id": "forged"},
            {**base, "_modulo.work_item": {"kind": "linear", "ref": "FAR-1"}},
            {**base, "_feedback_correction": {"is_correction_run": True}},
            base,
        ]
        hashes = set()
        for payload in forged:
            run = await _create(session, input_payload=payload)
            hashes.add(run.input_hash)
        assert len(hashes) == 1

    async def test_reserved_keys_never_reach_stored_payload(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(
            session,
            input_payload={
                "data": 1,
                "_work_item_id": "forged",
                "_modulo.work_item": {"kind": "linear", "ref": "FAR-1"},
                "_feedback_correction": {"is_correction_run": True},
            },
        )
        assert run.input_payload is not None
        assert run.input_payload == {"data": 1}
        for key in ("_work_item_id", "_modulo.work_item", "_feedback_correction"):
            assert key not in run.input_payload

    async def test_forged_work_item_id_never_survives(self, session: AsyncSession) -> None:
        """A payload carrying _work_item_id must not forge the anchor â€” the run
        gets its deterministic floor id, not the forged value."""
        await _seed_org(session)
        run = await _create(session, input_payload={"_work_item_id": "00000000-0000-0000-0000-00000000dead"})
        assert run.work_item_id == _floor_work_item_id(_ORG, run.id)
        assert run.work_item_id != uuid.UUID("00000000-0000-0000-0000-00000000dead")

    async def test_feedback_correction_kwarg_injected_post_strip(self, session: AsyncSession) -> None:
        """A correction run spawned via create_run's ``feedback_correction``
        kwarg stores the block in the run's input_payload (so
        executor._seed_state can promote it to run_context), while a raw
        payload carrying the same key is stripped first — the kwarg is
        engine-only and wins over any user-supplied value."""
        await _seed_org(session)

        correction_block = {"rejection_reason": "bad output", "is_correction_run": True}
        kwarg_run = await _create(
            session,
            input_payload={"user_input": "x", "_feedback_correction": {"is_correction_run": True}},
            feedback_correction=correction_block,
        )
        assert kwarg_run.input_payload is not None
        assert kwarg_run.input_payload["user_input"] == "x"
        assert kwarg_run.input_payload["_feedback_correction"] == correction_block

        raw_run = await _create(
            session,
            input_payload={"user_input": "x", "_feedback_correction": {"is_correction_run": True}},
        )
        assert raw_run.input_payload is not None
        assert "_feedback_correction" not in raw_run.input_payload


class TestForgePreventionPerInputPath:
    def test_raw_webhook_passthrough_empty_mapping_keeps_forged_key_in_route_payload(
        self,
    ) -> None:
        """The raw webhook passthrough (empty payload_mapping) copies the
        payload verbatim at the route layer â€” the neutralisation happens at
        the create_run chokepoint (covered by the strip tests above)."""
        raw = {"event": "opened", "_work_item_id": "forged"}
        assert _apply_payload_mapping(raw, {}) == raw

    def test_payload_mapping_rename_to_reserved_target_rejected(self) -> None:
        raw = {"github": {"number": 1}}
        for target in ("_work_item_id", "_modulo.work_item", "_feedback_correction"):
            with pytest.raises(ValueError, match=target):
                _apply_payload_mapping(raw, {target: "github.number"})

    async def test_manual_post_body_forged_key_is_stripped(self, session: AsyncSession) -> None:
        """The manual POST /runs path funnels through create_run, so a body
        carrying _work_item_id lands as the floor id with no forged key."""
        await _seed_org(session)
        run = await _create(session, input_payload={"user": "x", "_work_item_id": "forged"})
        assert run.work_item_id == _floor_work_item_id(_ORG, run.id)
        assert run.input_payload is not None
        assert "_work_item_id" not in run.input_payload


class TestCreateRunPathMatrix:
    async def test_manual_floor_mint(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, trigger_type="manual")
        assert run.work_item_id == _floor_work_item_id(_ORG, run.id)

    async def test_webhook_floor_mint(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, trigger_type="webhook")
        assert run.work_item_id == _floor_work_item_id(_ORG, run.id)

    async def test_cron_floor_mint(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, trigger_type="cron")
        assert run.work_item_id == _floor_work_item_id(_ORG, run.id)

    async def test_explicit_work_item_id_wins(self, session: AsyncSession) -> None:
        await _seed_org(session)
        explicit = uuid.uuid4()
        run = await _create(session, work_item_id=explicit)
        assert run.work_item_id == explicit

    async def test_child_adopts_parent_work_item_id(self, session: AsyncSession) -> None:
        await _seed_org(session)
        parent = await _create(session, trigger_type="webhook", work_item_id=uuid.uuid4())
        child = await _create(session, trigger_type="agent_signal", parent_run_id=parent.id)
        assert child.work_item_id == parent.work_item_id
        assert child.parent_run_id == parent.id

    async def test_child_of_parent_without_work_item_id_gets_floor(self, session: AsyncSession) -> None:
        await _seed_org(session)
        parent = await _create(session)
        parent.work_item_id = None
        await session.flush()
        child = await _create(session, trigger_type="correction", parent_run_id=parent.id)
        assert child.work_item_id == _floor_work_item_id(_ORG, child.id)

    async def test_refs_stamped_when_provided(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, work_item_refs=_REF_ENTRIES)
        assert run.work_item_refs == [{"kind": "github_issue", "ref": "a/b#5", "source": "derived"}]

    async def test_no_refs_means_no_refs_stamped(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session)
        assert run.work_item_refs is None

    async def test_is_replay_and_variant_group_id_stored(self, session: AsyncSession) -> None:
        await _seed_org(session)
        group_id = uuid.uuid4()
        run = await _create(session, is_replay=True, variant_group_id=group_id)
        assert run.is_replay is True
        assert run.variant_group_id == group_id

    async def test_work_item_id_set_exactly_once(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session)
        assert run.work_item_id is not None
        assert run.work_item_id == _floor_work_item_id(_ORG, run.id)
        await session.flush()
        stored = (await session.execute(select(Run.work_item_id).where(Run.id == run.id))).scalar_one()
        assert stored == run.work_item_id


class TestJourneyHydration:
    async def test_create_with_ref_mints_journey_row(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, work_item_refs=_REF_ENTRIES)
        journey = await _journey_for(session, "github_issue", "a/b#5")
        assert journey is not None
        assert journey.canonical_work_item_id == canonical_work_item_id(_ORG, "github_issue", "a/b#5")
        assert run.work_item_refs == [{"kind": "github_issue", "ref": "a/b#5", "source": "derived"}]

    async def test_same_ref_is_on_conflict_noop(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _create(session, work_item_refs=_REF_ENTRIES)
        await _create(session, work_item_refs=_REF_ENTRIES)
        rows = (
            (
                await session.execute(
                    select(Journey).where(Journey.organisation_id == _ORG, Journey.kind == "github_issue")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].run_count == 0
        assert rows[0].latest_status is None

    async def test_no_refs_means_no_journey_row(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _create(session)
        count = (await session.execute(select(Journey))).scalars().all()
        assert count == []

    async def test_journey_row_survives_run_purge(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, work_item_refs=_REF_ENTRIES)
        await session.execute(select(Journey))
        await session.delete(run)
        await session.flush()
        journey = await _journey_for(session, "github_issue", "a/b#5")
        assert journey is not None
        assert journey.canonical_work_item_id == canonical_work_item_id(_ORG, "github_issue", "a/b#5")


class TestDeterministicId:
    async def test_same_org_kind_ref_same_canonical_id_across_runs(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run_a = await _create(session, work_item_refs=_REF_ENTRIES)
        run_b = await _create(session, work_item_refs=_REF_ENTRIES)
        journey_a = await _journey_for(session, "github_issue", "a/b#5")
        journey_b = await _journey_for(session, "github_issue", "a/b#5")
        assert journey_a is not None
        assert journey_b is not None
        assert journey_a.id == journey_b.id
        assert journey_a.canonical_work_item_id == journey_b.canonical_work_item_id
        assert run_a.work_item_refs == run_b.work_item_refs


class TestFailOpen:
    async def test_canonicalizer_failure_does_not_abort_create_run(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_org(session)

        def _boom(*_args: Any, **_kwargs: Any) -> uuid.UUID:
            raise RuntimeError("canonicalizer exploded")

        monkeypatch.setattr("modulo.db.crud.run.canonical_work_item_id", _boom)
        run = await _create(session, work_item_refs=_REF_ENTRIES)
        assert run.id is not None
        assert run.work_item_id == _floor_work_item_id(_ORG, run.id)


class TestVariantAndReplayWiring:
    """FAR-143 part 3 — run_variant_weighted stamps variant_group_id; a replay
    event creates its run with is_replay=True."""

    async def test_run_variant_weighted_stamps_variant_group_id(self, session: AsyncSession) -> None:
        await _seed_org(session)
        group = VariantGroup(
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            name="ab-test",
            variants=[{"snapshot_id": str(_SNAPSHOT), "weight": 1.0}],
            selection_strategy="weighted",
        )
        session.add(group)
        await session.flush()

        result = await run_variant_weighted(session, org_id=_ORG, group=group, input_payload={})
        assert result is not None
        run = await session.get(Run, result["run_id"])
        assert run is not None
        assert run.variant_group_id == group.id
        # The variant group's run_count is incremented alongside.
        group_row = await session.get(VariantGroup, group.id)
        assert group_row is not None
        assert group_row.run_count == 1

    async def test_replay_event_creates_run_with_is_replay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        event_id = uuid.uuid4()
        trigger_id = uuid.uuid4()
        event = SimpleNamespace(
            id=event_id,
            trigger_id=trigger_id,
            trigger_type="webhook",
            validation_result="accepted",
            organisation_id=_ORG,
        )
        trigger = SimpleNamespace(
            id=trigger_id,
            pipeline_id=_PIPELINE,
            active=True,
            config_json={},
            organisation_id=_ORG,
            max_concurrent_runs=5,
            trigger_type="webhook",
        )
        payload = SimpleNamespace(raw_payload={"a": 1}, raw_body=b"body", organisation_id=_ORG)
        pipeline = SimpleNamespace(rate_limit_config=None)

        class _RowResult:
            def __init__(self, value: object) -> None:
                self._value = value

            def scalar_one_or_none(self) -> object:
                return self._value

            def scalar_one(self) -> object:
                return self._value

            def scalars(self) -> object:
                # FAR-214: the pre-trigger guardrail row query runs before
                # create_run on replays; the fake session has no guardrail rows
                # bound, so return an empty scalar result.
                return SimpleNamespace(all=lambda: [])

        class _ReplaySession:
            def __init__(self) -> None:
                self.added: list[Any] = []

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _RowResult:
                sql = str(stmt)
                # Order matters — "count" matches ``max_concurrent_runs`` columns
                # on triggers/pipelines, so the table-specific checks run first.
                if "trigger_events" in sql:
                    return _RowResult(event)
                if "pg_try_advisory_lock" in sql:
                    return _RowResult(True)
                if "triggers" in sql:
                    return _RowResult(trigger)
                if "webhook_payloads" in sql:
                    return _RowResult(payload)
                if "pipelines" in sql:
                    return _RowResult(pipeline)
                if "count" in sql:
                    return _RowResult(0)
                return _RowResult(None)

            async def flush(self) -> None:
                return None

            def in_transaction(self) -> bool:
                # Replays run inside the advisory-lock transaction; feature-flag
                # and library-service paths guard on an active transaction.
                return True

            def get_bind(self) -> object:
                # Postgres dialect so RLS set_config paths take the SQL branch.
                bind = SimpleNamespace()
                bind.dialect = SimpleNamespace(name="postgresql")
                return bind

            def add(self, obj: Any) -> None:
                self.added.append(obj)

            def add_all(self, objs: list[Any]) -> None:
                self.added.extend(objs)

        calls: dict[str, Any] = {}

        async def _fake_create_run(session: object, **kwargs: Any) -> Any:
            calls.update(kwargs)
            return SimpleNamespace(id=uuid.uuid4())

        monkeypatch.setattr("modulo.core.trigger_engine.create_run", _fake_create_run)
        monkeypatch.setattr("modulo.core.trigger_engine.ensure_triggers_resumable", AsyncMock())

        engine = TriggerEngine()
        _, _, _ = await engine.replay_event(
            _ReplaySession(),  # type: ignore[arg-type]
            event_id=event_id,
            org_id=_ORG,
            snapshot_id=_SNAPSHOT,
        )
        assert calls.get("is_replay") is True
        assert calls.get("trigger_type") == "webhook"
        assert calls.get("trigger_id") == trigger_id


class TestOwnerTeamStamp:
    """``Run.owner_team_id`` is stamped at creation from the pipeline it
    belongs to when no explicit team is passed — the source of truth for the
    MCP team-boundary guards and the analytics facts."""

    async def test_create_run_inherits_pipeline_owner_team(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_team(session)
        await _seed_pipeline(session, owner_team_id=_TEAM)

        run = await _create(session)

        assert run.owner_team_id == _TEAM

    async def test_create_run_org_level_pipeline_stays_org_level(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session, owner_team_id=None)

        run = await _create(session)

        assert run.owner_team_id is None

    async def test_explicit_owner_team_wins_over_pipeline_inheritance(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_team(session)
        await _seed_pipeline(session, owner_team_id=_TEAM)
        explicit_team = uuid.UUID("00000000-0000-0000-0000-0000000000d2")

        run = await _create(session, owner_team_id=explicit_team)

        assert run.owner_team_id == explicit_team

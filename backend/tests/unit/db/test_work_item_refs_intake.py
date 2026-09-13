"""Work-item refs supply channels + engine-assigned provenance (FAR-794 slice 2a).

These tests exercise the REAL ``create_run`` / ``coalesce_pending_run`` paths
against an in-memory SQLite database (no mocks of the function under test):

  * engine-assigned provenance — a manual/rerun trigger stamps ``caller``,
    every extraction-driven channel stamps ``derived``; the wire ``source``
    is NEVER trusted (a forged ``agent`` claim on a manual trigger is
    re-stamped, and an unknown wire source raises no vocabulary error);
  * supply channels — the explicit ``work_item_refs`` kwarg, the reserved
    ``_work_item_refs`` payload carrier, and the unprefixed
    ``work_item_refs`` wire alias, in that precedence order; a non-list
    carrier is ignored AND stripped;
  * re-stamp — canonical refs are folded into the stored payload (and thus
    ``input_hash``) with the wire carrier removed first;
  * the required-refs gate — a pipeline declaring
    ``run_context_defaults.work_item_refs_required`` refuses a delivery with
    no refs (``WorkItemRefsRequiredError``), fail-open on a malformed flag;
  * rank-guarded journey hydration — provenance only ever upgrades
    (``agent < derived < caller``), ``first_seen_source`` is immutable, and
    the finalise-owned ``latest_*`` / ``run_count`` columns are untouched;
  * coalesce merging — the surviving run's refs union the new delivery's
    (rank-guarded), the merged set is re-stamped into the payload, and the
    required flag is evaluated against the MERGED set.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import (
    COALESCE_KEY_FIELD,
    WorkItemRefsRequiredError,
    _hydrate_journeys,
    _input_hash,
    coalesce_pending_run,
    create_run,
)
from modulo.db.lifecycle_refs import (
    REFS_EVENT_ASSIGNED_SOURCE,
    REFS_EVENT_MALFORMED,
    REFS_EVENT_SHADOW_STRIP_HIT,
    REFS_EVENT_UNKNOWN_SOURCE,
    WIRE_REFS_ALIAS_KEY,
    WORK_ITEM_REFS_KEY,
    assigned_source_for_trigger,
    notify_refs_event,
    set_refs_counter_hook,
    sort_canonical_refs,
    validate_ref_entry,
)
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


@pytest.fixture
def refs_events() -> list[tuple[str, dict[str, Any]]]:
    """Capture lifecycle_refs counter events for the duration of a test."""
    captured: list[tuple[str, dict[str, Any]]] = []
    set_refs_counter_hook(lambda event, attrs: captured.append((event, dict(attrs))))
    yield captured
    set_refs_counter_hook(None)


async def _seed_org(session: AsyncSession, org_id: uuid.UUID = _ORG) -> None:
    session.add(Organisation(id=org_id, name="test org", slug=f"test-{org_id}"))
    await session.flush()


async def _seed_pipeline(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID = _PIPELINE,
    run_context_defaults: dict[str, Any] | None = None,
) -> Pipeline:
    pipeline = Pipeline(
        id=pipeline_id,
        organisation_id=_ORG,
        name="pipeline",
        account_id=_ORG,
        visibility="org",
        owner_team_id=None,
        run_context_defaults=run_context_defaults,
    )
    session.add(pipeline)
    await session.flush()
    return pipeline


async def _create(
    session: AsyncSession,
    *,
    org_id: uuid.UUID = _ORG,
    pipeline_id: uuid.UUID = _PIPELINE,
    trigger_type: str = "manual",
    input_payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Run:
    return await create_run(
        session,
        org_id=org_id,
        pipeline_id=pipeline_id,
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


async def _create_pending(session: AsyncSession, **kwargs: Any) -> Run:
    """Create a pending run eligible for coalescing.

    SQLite stores the Run model's ``server_default='false'`` for
    ``cancellation_requested`` as the TEXT value ``'false'`` (NUMERIC column
    affinity keeps the non-numeric string as text), which SQLAlchemy's Boolean
    result processor then reads back as ``True`` — so the coalesce scan's
    ``.is_(False)`` filter would never match a server-defaulted row. Postgres
    (a real BOOLEAN column) is unaffected; stamping False explicitly mirrors
    what production data actually looks like.
    """
    run = await _create(session, **kwargs)
    run.cancellation_requested = False
    await session.flush()
    return run


class TestAssignedSourceForTrigger:
    @pytest.mark.parametrize(
        ("trigger_type", "expected"),
        [
            ("manual", "caller"),
            ("rerun", "caller"),
            ("webhook", "derived"),
            ("cron", "derived"),
            ("polling", "derived"),
            ("agent_signal", "derived"),
            ("replay", "derived"),
            (None, "derived"),
        ],
        ids=["manual", "rerun", "webhook", "cron", "polling", "agent_signal", "replay", "none"],
    )
    def test_channel_assignment(self, trigger_type: str | None, expected: str) -> None:
        assert assigned_source_for_trigger(trigger_type) == expected


class TestValidateRefEntryIntakePolicy:
    def test_force_source_overrides_wire_source(self) -> None:
        entry = validate_ref_entry(
            {"kind": "github", "ref": "a/b#5", "source": "agent"},
            force_source="caller",
        )
        assert entry == {"kind": "github", "ref": "a/b#5", "source": "caller"}

    def test_force_source_absent_wire_source_gets_forced(self) -> None:
        entry = validate_ref_entry({"kind": "linear", "ref": "FAR-1"}, force_source="derived")
        assert entry == {"kind": "linear", "ref": "FAR-1", "source": "derived"}

    def test_force_source_ignores_unknown_wire_source_without_error(self) -> None:
        """The wire source is never trusted: an unknown wire value raises NO
        vocabulary error under force_source — callers count unknown
        submissions separately instead."""
        entry = validate_ref_entry(
            {"kind": "feature", "ref": "1", "source": "hooligan-value"},
            force_source="caller",
        )
        assert entry["source"] == "caller"

    def test_invalid_force_source_raises(self) -> None:
        with pytest.raises(ValueError, match="force_source must be one of"):
            validate_ref_entry({"kind": "feature", "ref": "1"}, force_source="reported")

    def test_default_accepts_read_vocabulary(self) -> None:
        for source in ("caller", "derived", "agent", "reported"):
            entry = validate_ref_entry({"kind": "feature", "ref": "1", "source": source})
            assert entry["source"] == source

    def test_normalise_reported_maps_to_agent(self) -> None:
        entry = validate_ref_entry(
            {"kind": "feature", "ref": "1", "source": "reported"},
            normalise_reported=True,
        )
        assert entry["source"] == "agent"

    def test_normalise_reported_leaves_other_sources(self) -> None:
        entry = validate_ref_entry(
            {"kind": "feature", "ref": "1", "source": "caller"},
            normalise_reported=True,
        )
        assert entry["source"] == "caller"

    def test_extra_keys_are_stripped(self) -> None:
        """Only {kind, ref, source, status?} may be asserted — the engine owns
        source_node_id and any other caller-injected key is dropped."""
        entry = validate_ref_entry(
            {"kind": "feature", "ref": "1", "source": "caller", "source_node_id": "n1", "evil": True}
        )
        assert entry == {"kind": "feature", "ref": "1", "source": "caller"}


class TestSortCanonicalRefs:
    def test_deterministic_kind_ref_source_ordering(self) -> None:
        refs = [
            {"kind": "linear", "ref": "FAR-2", "source": "derived"},
            {"kind": "github", "ref": "a/b#5", "source": "caller"},
            {"kind": "github", "ref": "a/b#5", "source": "agent"},
            {"kind": "github", "ref": "a/b#1", "source": "caller"},
        ]
        assert sort_canonical_refs(refs) == [
            {"kind": "github", "ref": "a/b#1", "source": "caller"},
            {"kind": "github", "ref": "a/b#5", "source": "agent"},
            {"kind": "github", "ref": "a/b#5", "source": "caller"},
            {"kind": "linear", "ref": "FAR-2", "source": "derived"},
        ]


class TestRefsCounterHook:
    def test_hook_receives_events_with_attrs(self) -> None:
        received: list[tuple[str, dict[str, Any]]] = []
        set_refs_counter_hook(lambda event, attrs: received.append((event, dict(attrs))))
        try:
            notify_refs_event(REFS_EVENT_ASSIGNED_SOURCE, source="caller", count=2)
            notify_refs_event(REFS_EVENT_SHADOW_STRIP_HIT, surface="run_context")
        finally:
            set_refs_counter_hook(None)
        assert received == [
            (REFS_EVENT_ASSIGNED_SOURCE, {"source": "caller", "count": 2}),
            (REFS_EVENT_SHADOW_STRIP_HIT, {"surface": "run_context"}),
        ]

    def test_raising_hook_is_swallowed(self) -> None:
        """Observability must never alter run-creation behaviour: a failing
        sink is logged and swallowed."""

        def _boom(_event: str, _attrs: dict[str, Any]) -> None:
            raise RuntimeError("sink exploded")

        set_refs_counter_hook(_boom)
        try:
            notify_refs_event(REFS_EVENT_UNKNOWN_SOURCE, count=1)
        finally:
            set_refs_counter_hook(None)


class TestEngineAssignedProvenance:
    async def test_manual_run_stamps_caller(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, trigger_type="manual", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        assert run.work_item_refs == [{"kind": "github", "ref": "a/b#5", "source": "caller"}]

    async def test_rerun_run_stamps_caller(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, trigger_type="rerun", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        assert run.work_item_refs == [{"kind": "github", "ref": "a/b#5", "source": "caller"}]

    async def test_webhook_run_stamps_derived(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, trigger_type="webhook", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        assert run.work_item_refs == [{"kind": "github", "ref": "a/b#5", "source": "derived"}]

    async def test_forged_wire_source_is_never_trusted(self, session: AsyncSession) -> None:
        """A caller-supplied entry claiming ``agent`` provenance on a manual
        trigger is re-stamped ``caller`` — the engine owns provenance."""
        await _seed_org(session)
        run = await _create(
            session,
            trigger_type="manual",
            work_item_refs=[{"kind": "github", "ref": "a/b#5", "source": "agent"}],
        )
        assert run.work_item_refs == [{"kind": "github", "ref": "a/b#5", "source": "caller"}]

    async def test_unknown_wire_source_does_not_reject(self, session: AsyncSession) -> None:
        """An unknown wire source value must NOT raise a vocabulary error at
        intake (the entry is re-stamped anyway) — it is counted separately."""
        await _seed_org(session)
        run = await _create(
            session,
            trigger_type="webhook",
            work_item_refs=[{"kind": "github", "ref": "a/b#5", "source": "hooligan"}],
        )
        assert run.work_item_refs == [{"kind": "github", "ref": "a/b#5", "source": "derived"}]

    async def test_intake_emits_assigned_and_unknown_counters(self, session: AsyncSession, refs_events: list) -> None:
        await _seed_org(session)
        await _create(
            session,
            trigger_type="webhook",
            work_item_refs=[
                {"kind": "github", "ref": "a/b#5", "source": "hooligan"},
                {"kind": "linear", "ref": "FAR-1"},
                "garbage-not-a-dict",
            ],
        )
        events = dict(refs_events)
        assert events[REFS_EVENT_ASSIGNED_SOURCE] == {"source": "derived", "count": 2}
        assert events[REFS_EVENT_UNKNOWN_SOURCE] == {"count": 1}
        assert events[REFS_EVENT_MALFORMED] == {"count": 1}


class TestRefsSupplyChannels:
    async def test_reserved_carrier_in_payload_is_harvested_and_restamped(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(
            session,
            trigger_type="webhook",
            input_payload={
                "data": 1,
                WORK_ITEM_REFS_KEY: [
                    {"kind": "GitHub Issue", "ref": "https://github.com/a/b/pull/5", "source": "agent"}
                ],
            },
        )
        # Canonicalised + engine-assigned (wire ``agent`` claim ignored).
        assert run.work_item_refs == [{"kind": "github_issue", "ref": "a/b#5", "source": "derived"}]
        assert run.input_payload is not None
        assert run.input_payload[WORK_ITEM_REFS_KEY] == [{"kind": "github_issue", "ref": "a/b#5", "source": "derived"}]
        assert run.input_payload["data"] == 1

    async def test_unprefixed_alias_in_payload_is_harvested(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(
            session,
            trigger_type="manual",
            input_payload={WIRE_REFS_ALIAS_KEY: [{"kind": "linear", "ref": "far-1"}]},
        )
        assert run.work_item_refs == [{"kind": "linear", "ref": "FAR-1", "source": "caller"}]

    async def test_explicit_kwarg_wins_over_payload_carrier(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(
            session,
            trigger_type="manual",
            input_payload={WORK_ITEM_REFS_KEY: [{"kind": "github", "ref": "a/b#1"}]},
            work_item_refs=[{"kind": "github", "ref": "a/b#2"}],
        )
        assert run.work_item_refs == [{"kind": "github", "ref": "a/b#2", "source": "caller"}]

    async def test_reserved_carrier_takes_precedence_over_alias(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(
            session,
            trigger_type="manual",
            input_payload={
                WORK_ITEM_REFS_KEY: [{"kind": "github", "ref": "a/b#1"}],
                WIRE_REFS_ALIAS_KEY: [{"kind": "github", "ref": "a/b#2"}],
            },
        )
        assert run.work_item_refs == [{"kind": "github", "ref": "a/b#1", "source": "caller"}]

    async def test_non_list_carrier_is_ignored_and_stripped(self, session: AsyncSession) -> None:
        """A forged non-list ``_work_item_refs`` (e.g. a string) must never be
        persisted verbatim — only engine-canonicalised entries (or nothing)
        may live under the reserved key."""
        await _seed_org(session)
        run = await _create(
            session,
            trigger_type="webhook",
            input_payload={WORK_ITEM_REFS_KEY: "not-a-list", "data": 1},
        )
        assert run.work_item_refs is None
        assert run.input_payload is not None
        assert WORK_ITEM_REFS_KEY not in run.input_payload
        assert run.input_payload["data"] == 1

    async def test_malformed_entry_dropped_run_still_created(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(
            session,
            trigger_type="manual",
            work_item_refs=[{"kind": "github"}, {"kind": "github", "ref": "a/b#5"}],
        )
        assert run.work_item_refs == [{"kind": "github", "ref": "a/b#5", "source": "caller"}]

    async def test_no_refs_means_none_stamped(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session)
        assert run.work_item_refs is None
        assert run.input_payload is not None
        assert WORK_ITEM_REFS_KEY not in run.input_payload

    async def test_restamped_refs_fold_into_input_hash(self, session: AsyncSession) -> None:
        """The canonical re-stamp happens BEFORE hashing: the same logical
        delivery with different wire spellings (URL vs shorthand, forged
        source claims) hashes identically."""
        await _seed_org(session)
        run_a = await _create(
            session,
            trigger_type="manual",
            input_payload={WORK_ITEM_REFS_KEY: [{"kind": "GitHub Issue", "ref": "https://github.com/a/b/pull/5"}]},
        )
        run_b = await _create(
            session,
            trigger_type="manual",
            input_payload={WORK_ITEM_REFS_KEY: [{"kind": "github_issue", "ref": "a/b#5", "source": "agent"}]},
        )
        assert run_a.input_hash == run_b.input_hash


class TestRequiredRefsGate:
    async def test_required_pipeline_refuses_refless_delivery(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session, run_context_defaults={"work_item_refs_required": True})
        with pytest.raises(WorkItemRefsRequiredError) as excinfo:
            await _create(session, trigger_type="webhook")
        assert excinfo.value.pipeline_id == _PIPELINE

    async def test_required_pipeline_accepts_delivery_with_refs(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session, run_context_defaults={"work_item_refs_required": True})
        run = await _create(session, trigger_type="webhook", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        assert run.work_item_refs is not None

    async def test_flag_absent_means_not_required(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session, run_context_defaults={})
        run = await _create(session, trigger_type="webhook")
        assert run.work_item_refs is None

    async def test_flag_false_means_not_required(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session, run_context_defaults={"work_item_refs_required": False})
        run = await _create(session, trigger_type="webhook")
        assert run.work_item_refs is None

    async def test_malformed_defaults_fail_open(self, session: AsyncSession) -> None:
        """The flag is an intake convenience, not a security control: a
        malformed defaults blob degrades to not-required."""
        await _seed_org(session)
        await _seed_pipeline(session, run_context_defaults="not-a-dict")
        run = await _create(session, trigger_type="webhook")
        assert run.work_item_refs is None

    async def test_no_pipeline_row_fail_open(self, session: AsyncSession) -> None:
        await _seed_org(session)
        run = await _create(session, trigger_type="webhook")
        assert run.work_item_refs is None


class TestRankGuardedJourneyHydration:
    async def test_agent_refs_stored_but_not_minted(self, session: AsyncSession) -> None:
        """``agent``-sourced entries (node emissions / reported claims) are
        stored on the run but never mint a journey row at create time —
        minting stays owned by engine-assigned provenance. Intake can never
        produce an agent stamp (force_source only assigns caller/derived), so
        this exercises the hydrate gate directly."""
        await _seed_org(session)
        await _hydrate_journeys(session, _ORG, [{"kind": "github", "ref": "a/b#5", "source": "agent"}])
        assert await _journey_for(session, "github", "a/b#5") is None

    async def test_provenance_upgrades_derived_to_caller(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _create(session, trigger_type="webhook", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        first = await _journey_for(session, "github", "a/b#5")
        assert first is not None
        assert first.provenance == "derived"
        assert first.first_seen_source == "derived"

        await _create(session, trigger_type="manual", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        # The hydrate upsert writes the DB row directly (on-conflict DO
        # UPDATE) — the identity map still holds the first-load object, so
        # expire before re-fetching or the stale provenance is asserted.
        session.expire_all()
        upgraded = await _journey_for(session, "github", "a/b#5")
        assert upgraded is not None
        assert upgraded.provenance == "caller"
        # first_seen_source is immutable — written only on INSERT.
        assert upgraded.first_seen_source == "derived"

    async def test_provenance_never_downgrades(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _create(session, trigger_type="manual", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        await _create(session, trigger_type="webhook", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        journey = await _journey_for(session, "github", "a/b#5")
        assert journey is not None
        assert journey.provenance == "caller"

    async def test_create_time_hydrate_is_mint_only(self, session: AsyncSession) -> None:
        """``latest_*`` / ``run_count`` are owned by the finalise path — a
        create-time hydrate of an EXISTING journey must not touch them."""
        await _seed_org(session)
        await _create(session, trigger_type="webhook", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        journey = await _journey_for(session, "github", "a/b#5")
        assert journey is not None
        journey.latest_status = "done"
        journey.latest_provenance = "derived"
        journey.run_count = 7
        await session.flush()

        await _create(session, trigger_type="manual", work_item_refs=[{"kind": "github", "ref": "a/b#5"}])
        after = await _journey_for(session, "github", "a/b#5")
        assert after is not None
        assert after.latest_status == "done"
        assert after.latest_provenance == "derived"
        assert after.run_count == 7

    async def test_duplicate_kind_ref_in_one_set_collapses_to_highest_rank(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _create(
            session,
            trigger_type="manual",
            work_item_refs=[
                {"kind": "github", "ref": "a/b#5"},
                {"kind": "github", "ref": "a/b#5"},
            ],
        )
        rows = (
            (await session.execute(select(Journey).where(Journey.organisation_id == _ORG, Journey.kind == "github")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].provenance == "caller"


class TestCoalesceRefsMerge:
    async def test_merged_refs_union_both_deliveries(self, session: AsyncSession) -> None:
        await _seed_org(session)
        pending = await _create_pending(
            session,
            trigger_type="webhook",
            coalesce_key="github:o/r:pr:1",
            work_item_refs=[{"kind": "github", "ref": "a/b#5"}],
        )
        merged = await coalesce_pending_run(
            session,
            org_id=_ORG,
            pipeline_id=_PIPELINE,
            coalesce_key="github:o/r:pr:1",
            input_payload={WORK_ITEM_REFS_KEY: [{"kind": "linear", "ref": "FAR-1"}]},
        )
        assert merged is pending
        assert merged.work_item_refs == [
            {"kind": "github", "ref": "a/b#5", "source": "derived"},
            {"kind": "linear", "ref": "FAR-1", "source": "derived"},
        ]
        assert merged.input_payload is not None
        assert merged.input_payload[WORK_ITEM_REFS_KEY] == merged.work_item_refs

    async def test_rank_guard_existing_caller_wins_over_new_derived(self, session: AsyncSession) -> None:
        await _seed_org(session)
        pending = await _create_pending(
            session,
            trigger_type="manual",
            coalesce_key="github:o/r:pr:1",
            work_item_refs=[{"kind": "github", "ref": "a/b#5"}],
        )
        merged = await coalesce_pending_run(
            session,
            org_id=_ORG,
            pipeline_id=_PIPELINE,
            coalesce_key="github:o/r:pr:1",
            input_payload={WORK_ITEM_REFS_KEY: [{"kind": "github", "ref": "a/b#5"}]},
        )
        assert merged is pending
        # The new delivery's derived claim must NOT downgrade the caller stamp.
        assert merged.work_item_refs == [{"kind": "github", "ref": "a/b#5", "source": "caller"}]

    async def test_rank_guard_new_caller_upgrades_existing_derived(self, session: AsyncSession) -> None:
        await _seed_org(session)
        pending = await _create_pending(
            session,
            trigger_type="webhook",
            coalesce_key="github:o/r:pr:1",
            work_item_refs=[{"kind": "github", "ref": "a/b#5"}],
        )
        merged = await coalesce_pending_run(
            session,
            org_id=_ORG,
            pipeline_id=_PIPELINE,
            coalesce_key="github:o/r:pr:1",
            input_payload={WORK_ITEM_REFS_KEY: [{"kind": "github", "ref": "a/b#5"}]},
            trigger_type="manual",
        )
        assert merged is pending
        assert merged.work_item_refs == [{"kind": "github", "ref": "a/b#5", "source": "caller"}]

    async def test_required_satisfied_by_either_delivery(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session, run_context_defaults={"work_item_refs_required": True})
        pending = await _create_pending(
            session,
            trigger_type="webhook",
            coalesce_key="github:o/r:pr:1",
            work_item_refs=[{"kind": "github", "ref": "a/b#5"}],
        )
        merged = await coalesce_pending_run(
            session,
            org_id=_ORG,
            pipeline_id=_PIPELINE,
            coalesce_key="github:o/r:pr:1",
            input_payload={"data": 1},
        )
        assert merged is pending
        assert merged.work_item_refs is not None

    async def test_required_refless_both_sides_raises(self, session: AsyncSession) -> None:
        """A required pipeline folds only when the MERGED set carries refs.

        The pending run is seeded directly via the ORM: create_run's own gate
        would refuse a refless delivery on a required pipeline before any
        pending row could exist, and this test exercises the coalesce-side
        evaluation of the flag (the legacy pending row models one created
        before the flag was enabled).
        """
        await _seed_org(session)
        await _seed_pipeline(session, run_context_defaults={"work_item_refs_required": True})
        session.add(
            Run(
                id=uuid.uuid4(),
                organisation_id=_ORG,
                pipeline_id=_PIPELINE,
                snapshot_id=_SNAPSHOT,
                trigger_type="webhook",
                run_number=1,
                langgraph_thread_id="lg-seed",
                input_hash=_input_hash({COALESCE_KEY_FIELD: "github:o/r:pr:1"}),
                input_payload={COALESCE_KEY_FIELD: "github:o/r:pr:1"},
                status="pending",
                cancellation_requested=False,
            )
        )
        await session.flush()
        with pytest.raises(WorkItemRefsRequiredError):
            await coalesce_pending_run(
                session,
                org_id=_ORG,
                pipeline_id=_PIPELINE,
                coalesce_key="github:o/r:pr:1",
                input_payload={"data": 1},
            )

    async def test_coalesce_hydrates_newly_seen_refs(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _create_pending(
            session,
            trigger_type="webhook",
            coalesce_key="github:o/r:pr:1",
            work_item_refs=[{"kind": "github", "ref": "a/b#5"}],
        )
        await coalesce_pending_run(
            session,
            org_id=_ORG,
            pipeline_id=_PIPELINE,
            coalesce_key="github:o/r:pr:1",
            input_payload={WORK_ITEM_REFS_KEY: [{"kind": "linear", "ref": "FAR-1"}]},
        )
        journey = await _journey_for(session, "linear", "FAR-1")
        assert journey is not None
        assert journey.provenance == "derived"

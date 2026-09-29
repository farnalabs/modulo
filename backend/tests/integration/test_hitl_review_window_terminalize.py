"""FAR-1257: the review-window sweep predicate against real Postgres.

Drives ``_terminalize_expired_hitl_reviews`` DIRECTLY against the migrated
schema (testcontainers) so the two-armed deadline predicate is exercised by
Postgres itself, not just pinned as SQL text by the unit tests:

* a STAMPED claim (``terminalize_at`` set) is collected once
  ``terminalize_at < now()``, regardless of its claim TTL;
* a STAMPED claim still inside its window is NOT collected even when the
  legacy ``expires_at + grace`` arithmetic would already have fired — the
  stamp is authoritative, which is the whole point of a separate column;
* a LEGACY claim (``terminalize_at`` NULL) keeps the shipped
  ``expires_at + grace`` fallback verbatim;
* live human work (a CLAIMED gate) still spares the run, and the
  WHY/WHO stamps ride the same UPDATE.

Seeding helpers are shared with ``test_org_sandbox_capacity`` (the FAR-648
sweep's behavioural matrix lives there); this module owns only the
``terminalize_at``-aware claim seeder.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.rls import set_rls_org
from tests.integration.test_org_sandbox_capacity import (
    _SANDBOX_GRAPH,
    _run_cancel_reason,
    _run_state,
    _seed_org_account,
    _seed_pipeline,
    _seed_run,
    _seed_snapshot,
    _terminalize_count,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group(name="hitl_review_window"),
]

_GRACE_SECONDS = 3600


async def _seed_claim(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    review_id: str,
    *,
    expires_at: datetime,
    terminalize_at: datetime | None = None,
    account_id: uuid.UUID | None = None,
    decision: str | None = None,
) -> None:
    """Insert a ``hitl_claims`` row with an optional FAR-1257 deadline stamp."""
    from sqlalchemy import insert

    from modulo.db.models.hitl_claim import HitlClaim

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await session.execute(
            insert(HitlClaim).values(
                id=uuid.uuid4(),
                organisation_id=org_id,
                run_id=run_id,
                pipeline_id=pipeline_id,
                review_id=review_id,
                account_id=account_id,
                claimed_at=datetime.now(UTC) if account_id is not None else None,
                expires_at=expires_at,
                terminalize_at=terminalize_at,
                decision=decision,
                decision_at=datetime.now(UTC) if decision is not None else None,
            )
        )


async def _seed_awaiting_run(
    db_engine: AsyncEngine,
    org_label: str,
    pipeline_label: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed org + account + pipeline + snapshot + an ``awaiting_human`` run."""
    org_id, user_id = await _seed_org_account(db_engine, org_label, cap=None)
    pipeline_id = await _seed_pipeline(db_engine, org_id, pipeline_label, user_id)
    snapshot_id = await _seed_snapshot(db_engine, org_id, pipeline_id, _SANDBOX_GRAPH)
    run_id = await _seed_run(db_engine, org_id, pipeline_id, snapshot_id, status="awaiting_human")
    return org_id, user_id, pipeline_id, run_id


@pytest_asyncio.fixture(autouse=True)
async def _clean_runs_between_tests(db_engine: AsyncEngine) -> None:
    """Truncate ``runs`` before every test so a sweep can never pick up a row
    seeded by an earlier test in this module (the sweep is org-scoped, but the
    org ids are fresh per test anyway — this keeps the assertions exact)."""
    async with db_engine.connect() as conn:
        await conn.execute(text("TRUNCATE runs RESTART IDENTITY CASCADE"))
        await conn.commit()


class TestStampedDeadline:
    async def test_past_terminalize_at_fires(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """A stamped claim past its deadline is collected — even though its
        claim TTL (``expires_at``) is still a DAY in the future, so the legacy
        arithmetic alone would never have fired it."""
        from modulo.core.cron_helpers import _terminalize_expired_hitl_reviews

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257StampedPastOrg", "PipeF1257StampedPast"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            terminalize_at=datetime.now(UTC) - timedelta(seconds=30),
        )

        count = await _terminalize_count(
            app_engine, org_id, _terminalize_expired_hitl_reviews, grace_seconds=_GRACE_SECONDS
        )
        assert count == 1

        status, code = await _run_state(db_engine, org_id, run_id)
        assert status == "cancelled"
        assert code == "hitl_review_expired"
        reason, actor = await _run_cancel_reason(db_engine, org_id, run_id)
        assert reason == "hitl_review_expired"
        assert actor == "system"

    async def test_inside_window_does_not_fire(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """The stamp is AUTHORITATIVE: a claim still inside its window is left
        alone even though ``expires_at`` is 10h stale (legacy arithmetic with a
        3600s grace would have fired it 9h ago). This is the regression that a
        single-arm predicate would ship."""
        from modulo.core.cron_helpers import _terminalize_expired_hitl_reviews

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257StampedInOrg", "PipeF1257StampedIn"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) - timedelta(hours=10),
            terminalize_at=datetime.now(UTC) + timedelta(hours=6),
        )

        count = await _terminalize_count(
            app_engine, org_id, _terminalize_expired_hitl_reviews, grace_seconds=_GRACE_SECONDS
        )
        assert count == 0

        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "awaiting_human"

    async def test_claimed_stamped_gate_spares_the_run(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """Multi-gate/claim safety is unchanged: a CLAIMED gate is live human
        work no matter how stale its deadline is."""
        from modulo.core.cron_helpers import _terminalize_expired_hitl_reviews

        org_id, user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257StampedClaimedOrg", "PipeF1257StampedClaimed"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            terminalize_at=datetime.now(UTC) - timedelta(hours=2),
            account_id=user_id,
        )

        count = await _terminalize_count(
            app_engine, org_id, _terminalize_expired_hitl_reviews, grace_seconds=_GRACE_SECONDS
        )
        assert count == 0

        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "awaiting_human"

    async def test_stamped_in_window_siblings_spare_a_past_deadline_gate(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """The NOT-EXISTS arm mirrors the same two deadlines: one past-deadline
        gate plus one still-in-window gate means live human work exists, so the
        run is left alone (the FAR-648 multi-gate rule, carried over)."""
        from modulo.core.cron_helpers import _terminalize_expired_hitl_reviews

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257MultiStampedOrg", "PipeF1257MultiStamped"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            terminalize_at=datetime.now(UTC) - timedelta(hours=2),
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-2",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            terminalize_at=datetime.now(UTC) + timedelta(hours=6),
        )

        count = await _terminalize_count(
            app_engine, org_id, _terminalize_expired_hitl_reviews, grace_seconds=_GRACE_SECONDS
        )
        assert count == 0

        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "awaiting_human"


class TestLegacyFallback:
    async def test_unstamped_past_grace_fires(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """The legacy arm is untouched: a NULL ``terminalize_at`` claim past
        ``expires_at + grace`` is still collected (shipped behaviour for every
        row that fired before the column existed)."""
        from modulo.core.cron_helpers import _terminalize_expired_hitl_reviews

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257LegacyPastOrg", "PipeF1257LegacyPast"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) - timedelta(hours=2),
            terminalize_at=None,
        )

        count = await _terminalize_count(
            app_engine, org_id, _terminalize_expired_hitl_reviews, grace_seconds=_GRACE_SECONDS
        )
        assert count == 1

        status, code = await _run_state(db_engine, org_id, run_id)
        assert status == "cancelled"
        assert code == "hitl_review_expired"

    async def test_unstamped_inside_grace_does_not_fire(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """Past ``expires_at`` but INSIDE the grace window is still live human
        work — the grace knob keeps its shipped meaning on legacy rows."""
        from modulo.core.cron_helpers import _terminalize_expired_hitl_reviews

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257LegacyGraceOrg", "PipeF1257LegacyGrace"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) - timedelta(minutes=30),
            terminalize_at=None,
        )

        count = await _terminalize_count(
            app_engine, org_id, _terminalize_expired_hitl_reviews, grace_seconds=_GRACE_SECONDS
        )
        assert count == 0

        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "awaiting_human"

    async def test_stamped_row_ignores_the_grace_knob(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """A stamped deadline 5 minutes in the past is collected even with a
        grace window so large the legacy arm could never have fired — the two
        arms are alternatives, not accumulations."""
        from modulo.core.cron_helpers import _terminalize_expired_hitl_reviews

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257StampedGraceOrg", "PipeF1257StampedGrace"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) + timedelta(days=7),
            terminalize_at=datetime.now(UTC) - timedelta(minutes=5),
        )

        count = await _terminalize_count(app_engine, org_id, _terminalize_expired_hitl_reviews, grace_seconds=604800)
        assert count == 1

        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "cancelled"


class TestParkMarginOrdering:
    async def test_park_cannot_fire_before_the_terminalize_deadline(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """Cancel precedes PARK by construction: a claim whose deadline passed
        60s ago is a terminalizer candidate but NOT a park candidate — the park
        needs ``terminalize_at + margin`` (margin >= 300s, floored at five
        reconcile ticks), so the sweep that must cancel the run always gets
        there first."""
        from modulo.core.run_admission import _PARK_MARGIN_FLOOR_SECONDS, park_expired_hitl_runs

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257ParkOrderOrg", "PipeF1257ParkOrder"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            terminalize_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        # grace_seconds=60 pins the margin at the FLOOR (300s), so this claim
        # (60s past) is unambiguously inside the park window.
        result = await park_expired_hitl_runs(app_engine, grace_seconds=60)  # type: ignore[arg-type]
        assert _PARK_MARGIN_FLOOR_SECONDS == 300
        assert result == {"parked": 0}

        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "awaiting_human"

    async def test_park_deadline_is_terminalize_at_plus_margin(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """The stamped park arm is live: once ``terminalize_at + margin`` has
        passed (10 min past the deadline vs the 300s floor), the run parks —
        NON-terminal (``hitl_parked``, still in ACTIVE_RUN_STATUSES) and the
        gate row is untouched (park != decide)."""
        from modulo.core.run_admission import park_expired_hitl_runs

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257ParkFireOrg", "PipeF1257ParkFire"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            terminalize_at=datetime.now(UTC) - timedelta(minutes=10),
        )

        result = await park_expired_hitl_runs(app_engine, grace_seconds=60)  # type: ignore[arg-type]
        assert result == {"parked": 1}

        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "hitl_parked"

    async def test_unstamped_claim_keeps_the_legacy_park_arithmetic(
        self,
        app_engine: AsyncEngine,
        db_engine: AsyncEngine,
        migrated_db_url: str,
    ) -> None:
        """The legacy arm is untouched: a NULL ``terminalize_at`` claim past
        ``expires_at + park_grace`` still parks (shipped behaviour)."""
        from modulo.core.run_admission import park_expired_hitl_runs

        org_id, _user_id, pipeline_id, run_id = await _seed_awaiting_run(
            db_engine, "F1257ParkLegacyOrg", "PipeF1257ParkLegacy"
        )
        await _seed_claim(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            "review-1",
            expires_at=datetime.now(UTC) - timedelta(hours=48),
            terminalize_at=None,
        )

        result = await park_expired_hitl_runs(app_engine, grace_seconds=3600)  # type: ignore[arg-type]
        assert result == {"parked": 1}

        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "hitl_parked"

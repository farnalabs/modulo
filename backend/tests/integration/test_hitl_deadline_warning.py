"""FAR-1270 approaching-deadline HITL warning — real Postgres.

Unit mocks cannot exercise the sweep's actual SELECT/JOIN against the
migrated schema (the org + unclaimed + undecided + awaiting_human predicate,
the dual-arm deadline arithmetic over real timestamps, the sibling-guard
subquery) or the recipient resolver's role/preference filters — so this file
runs ``dispatch_deadline_notifications`` end-to-end against the testcontainers
database with only the SMTP send stubbed. The engine is the testcontainers
superuser, which bypasses RLS (FORCE does not apply to superusers — see the
``non_superuser_role`` fixture docstring), so the test asserts the query
semantics via the explicit ``organisation_id`` filters, not RLS itself.

Scenarios: a 60s-window gate in band (the hard case), past-deadline,
not-yet-in-band, claimed, decided, run-not-awaiting, claimed-sibling, and a
no-recipient org whose once-only marker must stay unburned until someone
opts in.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.hitl_manager.deadline_warning import dispatch_deadline_notifications

pytestmark = pytest.mark.integration

_GRACE = 3600  # shipped default: hitl_review_cancel_grace_seconds


class _FakeRedis:
    """Minimal async Redis double honouring SET NX EX semantics."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


# ---------------------------------------------------------------------------
# Seed helpers (raw SQL, mirroring the sibling integration suites)
# ---------------------------------------------------------------------------


async def _seed_org(engine: AsyncEngine, org_id: uuid.UUID, name: str) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )


async def _seed_account(engine: AsyncEngine, email: str, *, hitl_email_default: bool | None = None) -> uuid.UUID:
    account_id = uuid.uuid4()
    preferences = "{}"
    if hitl_email_default is not None:
        preferences = '{"hitl_email": {"default": %s}}' % ("true" if hitl_email_default else "false")
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash, preferences) "
                "VALUES (:id, :email, :name, 'local', true, 'hash', CAST(:prefs AS json))"
            ),
            {"id": str(account_id), "email": email, "name": f"User {email}", "prefs": preferences},
        )
    return account_id


async def _seed_membership(engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID, role: str = "admin") -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, :role)"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id), "role": role},
        )


async def _seed_pipeline(engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID, name: str) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, description, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, 'test', :uid, 5, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": name, "uid": str(account_id)},
        )
    return pipeline_id


async def _seed_snapshot(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                "model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '[]'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


async def _seed_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, input_hash, input_payload, langgraph_thread_id, run_number, status) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :status)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": uuid.uuid4().hex,
                "thread": f"{org_id}:{run_id}",
                "rn": run_number,
                "status": status,
            },
        )
    return run_id


async def _seed_claim(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    review_id: str,
    *,
    expires_at: datetime,
    created_at: datetime,
    account_id: uuid.UUID | None = None,
    claimed_at: datetime | None = None,
    decision: str | None = None,
) -> uuid.UUID:
    claim_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO hitl_claims (id, organisation_id, run_id, pipeline_id, review_id, "
                "expires_at, created_at, account_id, claimed_at, decision) "
                "VALUES (:id, :oid, :rid, :pid, :review, :expires, :created, :acct, :claimed, :decision)"
            ),
            {
                "id": str(claim_id),
                "oid": str(org_id),
                "rid": str(run_id),
                "pid": str(pipeline_id),
                "review": review_id,
                "expires": expires_at,
                "created": created_at,
                "acct": str(account_id) if account_id else None,
                "claimed": claimed_at,
                "decision": decision,
            },
        )
    return claim_id


class _World:
    """Seeded scenario graph for one integration test."""

    def __init__(self, engine: AsyncEngine, now: datetime) -> None:
        self.engine = engine
        self.now = now
        self.factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        self.runs: dict[str, uuid.UUID] = {}
        self.claims: dict[str, uuid.UUID] = {}
        self.org1 = uuid.uuid4()
        self.org2 = uuid.uuid4()
        # Unique per world: accounts.email is unique across the shared session DB.
        suffix = uuid.uuid4().hex[:8]
        self.approver_email = f"deadline-approver-{suffix}@example.com"
        self.optin_email = f"deadline-optin-{suffix}@example.com"
        self.nobody_email = f"deadline-nobody-{suffix}@example.com"

    def approaching_expires(self) -> datetime:
        """Legacy deadline lands exactly 60s from *now* — the 60s window case."""
        return self.now + timedelta(seconds=60 - _GRACE)


async def _build_world(engine: AsyncEngine, now: datetime) -> _World:
    world = _World(engine, now)

    # --- org 1: one opted-in admin, one pipeline, one snapshot ------------
    await _seed_org(engine, world.org1, "DeadlineOrg1")
    account1 = await _seed_account(engine, world.approver_email, hitl_email_default=True)
    await _seed_membership(engine, world.org1, account1, role="admin")
    pipe1 = await _seed_pipeline(engine, world.org1, account1, "Deadline Pipeline 1")
    snap1 = await _seed_snapshot(engine, world.org1, pipe1)

    async def scenario(
        key: str,
        *,
        review_id: str,
        run_status: str = "awaiting_human",
        expires_at: datetime | None = None,
        created_at: datetime | None = None,
        account_id: uuid.UUID | None = None,
        claimed_at: datetime | None = None,
        decision: str | None = None,
        org_id: uuid.UUID | None = None,
        pipeline_id: uuid.UUID | None = None,
        snapshot_id: uuid.UUID | None = None,
        extra_claims: list[dict[str, Any]] | None = None,
    ) -> None:
        org = org_id if org_id is not None else world.org1
        pipe = pipeline_id if pipeline_id is not None else pipe1
        snap = snapshot_id if snapshot_id is not None else snap1
        run_id = await _seed_run(engine, org, pipe, snap, run_status)
        world.runs[key] = run_id
        world.claims[key] = await _seed_claim(
            engine,
            org,
            run_id,
            pipe,
            review_id,
            expires_at=expires_at if expires_at is not None else world.approaching_expires(),
            created_at=created_at if created_at is not None else now,
            account_id=account_id,
            claimed_at=claimed_at,
            decision=decision,
        )
        for index, extra in enumerate(extra_claims or []):
            world.claims[f"{key}-{index}"] = await _seed_claim(
                engine,
                org,
                run_id,
                pipe,
                extra["review_id"],
                expires_at=extra.get("expires_at", world.approaching_expires()),
                created_at=extra.get("created_at", now),
                account_id=extra.get("account_id"),
                claimed_at=extra.get("claimed_at"),
                decision=extra.get("decision"),
            )

    # In band: legacy deadline exactly 60s away, window 60s (the minimum).
    await scenario("approach", review_id="gate-approach")
    # Past deadline: the terminaliser owns it — never warn.
    await scenario(
        "past",
        review_id="gate-past",
        expires_at=now - timedelta(seconds=_GRACE + 60),
        created_at=now - timedelta(minutes=75),
    )
    # Deadline 10 000s away: beyond the 1h lead cap — far too early.
    await scenario(
        "far",
        review_id="gate-far",
        expires_at=now + timedelta(seconds=10000 - _GRACE),
    )
    # Claimed: the claim surface already owns it.
    await scenario(
        "claimed",
        review_id="gate-claimed",
        account_id=account1,
        claimed_at=now - timedelta(minutes=2),
    )
    # Decided: nothing to warn about.
    await scenario(
        "decided",
        review_id="gate-decided",
        decision="approved",
    )
    # Run no longer awaiting review: cancellation owns it.
    await scenario("cancelled_run", review_id="gate-cancelled-run", run_status="cancelled")
    # A claimed sibling gate spares the whole run from the terminaliser, so an
    # approaching warning for its other gate would be a false alarm.
    await scenario(
        "sibling",
        review_id="gate-sibling-open",
        extra_claims=[
            {
                "review_id": "gate-sibling-held",
                "account_id": account1,
                "claimed_at": now - timedelta(minutes=1),
            }
        ],
    )

    # --- org 2: an account exists but nobody holds an org membership ------
    await _seed_org(engine, world.org2, "DeadlineOrg2")
    account2 = await _seed_account(engine, world.nobody_email)
    pipe2 = await _seed_pipeline(engine, world.org2, account2, "Deadline Pipeline 2")
    snap2 = await _seed_snapshot(engine, world.org2, pipe2)
    await scenario(
        "no_members",
        review_id="gate-no-members",
        org_id=world.org2,
        pipeline_id=pipe2,
        snapshot_id=snap2,
    )

    return world


@pytest_asyncio.fixture
async def world(db_engine: AsyncEngine) -> _World:
    now = datetime.now(UTC)
    return await _build_world(db_engine, now)


def _seeded_run_ids(w: _World) -> set[uuid.UUID]:
    return set(w.runs.values())


async def _dispatch(w: _World, redis_client: _FakeRedis, send: AsyncMock) -> list[dict[str, Any]]:
    with patch("modulo.core.hitl_manager.deadline_warning.send_hitl_deadline_alerts", send):
        return await dispatch_deadline_notifications(
            w.factory,
            grace_seconds=_GRACE,
            redis_client=redis_client,
            now=w.now,
        )


async def test_sweep_notifies_only_the_in_band_gate_and_only_once(world: _World) -> None:
    send = AsyncMock()
    redis = _FakeRedis()
    seeded = _seeded_run_ids(world)

    first = await _dispatch(world, redis, send)
    hits = {entry["run_id"] for entry in first} & seeded
    assert hits == {world.runs["approach"]}, f"unexpected first-tick hits: {hits}"

    # Exactly one email, to the opted-in recipient, with the minimum-window
    # lead reflected in the payload (60s left -> 1 minute).
    approach_calls = [call for call in send.await_args_list if call.args[1] == world.runs["approach"]]
    assert len(approach_calls) == 1
    call = approach_calls[0]
    assert call.args[0] == [world.approver_email]
    assert call.args[2] == "gate-approach"
    assert call.args[3] == "Deadline Pipeline 1"
    assert call.args[4] == 1

    # Skipped scenarios never reached the send path at all.
    sent_run_ids = {call.args[1] for call in send.await_args_list}
    for key in ("past", "far", "claimed", "decided", "cancelled_run", "sibling", "no_members"):
        assert world.runs[key] not in sent_run_ids, f"{key} gate must not be notified"

    # Once-only: a second tick against the same store sends nothing new.
    second = await _dispatch(world, redis, send)
    hits_second = {entry["run_id"] for entry in second} & seeded
    assert not hits_second


async def test_no_recipients_leaves_marker_unset_until_someone_opts_in(world: _World) -> None:
    send = AsyncMock()
    redis = _FakeRedis()
    # Scope this test's assertions to the no-membership scenario only: the
    # same world also seeds an opted-in org whose own in-band gate this
    # dispatch legitimately notifies (fresh store, fresh claim).
    only = {world.runs["no_members"]}
    claim_marker_fragment = str(world.claims["no_members"])

    first = await _dispatch(world, redis, send)
    assert not ({entry["run_id"] for entry in first} & only)
    # The once-only marker was NOT burned by the empty-recipient skip.
    assert not [key for key in redis.keys if claim_marker_fragment in key]

    # Someone opts in mid-band: the next tick must still warn.
    opted_in = await _seed_account(world.engine, world.optin_email, hitl_email_default=True)
    await _seed_membership(world.engine, world.org2, opted_in, role="operator")

    third = await _dispatch(world, redis, send)
    hits = {entry["run_id"] for entry in third} & only
    assert hits == {world.runs["no_members"]}
    optin_calls = [call for call in send.await_args_list if call.args[1] == world.runs["no_members"]]
    assert len(optin_calls) == 1
    assert optin_calls[0].args[0] == [world.optin_email]

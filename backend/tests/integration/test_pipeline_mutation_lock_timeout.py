"""ITEM 5 + FAR-1313: the in-txn row lock wait is BOUNDED - and a timeout maps to 409.

``_reapply_team_gate_inside_mutation_txn`` issues ``SELECT ... FOR UPDATE`` as
the first lock of most pipeline mutation transactions. Before ITEM 5 that wait
was unbounded: a contended PATCH parked a pooled connection on the row lock
until the holder committed (or forever).

This exercises the REAL bound against real Postgres: a concurrent transaction
holds the pipeline row lock, a PATCH arrives, and the endpoint must come back
with HTTP 409 + the lock-timeout detail (SQLSTATE 55P03 mapped by
``handle_db_errors``) rather than hanging or answering the generic 503.

FAR-1313 extends the same invariant to the lock the GATE does not take in
time: the clone endpoint's ``check_pipeline_name_available`` fires its own
``SELECT ... FOR UPDATE`` (on the target-name row) BEFORE the clone's in-txn
team gate reaches ``clone_pipeline``'s step-(a) commit hook, so it ran with NO
bound at all until ``_set_mutation_row_lock_timeout`` was added at the top of
the clone transaction. The second test below contends that name row instead.

Each wait is ``Settings.mutation_row_lock_timeout_ms`` (5 s by default), so
each test takes ~5 s by design - it is timing the bounded wait, not a fixed
sleep.
"""

from __future__ import annotations

import time
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.auth.jwt import create_access_token
from modulo.settings import get_settings

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_MANUAL = "manual_approval"
#: Seconds added to ``mutation_row_lock_timeout_ms`` for the HTTP client's own
#: timeout - enough headroom to OBSERVE the server's answer at every permitted
#: setting (100 ms..30 s) without the client giving up first.
_CLIENT_MARGIN_SECONDS = 20.0
#: Seconds added to the expected wait for the "the wait was bounded" ceiling -
#: tight enough that an UNBOUNDED wait (the holder never releases) still fails,
#: loose enough for request/DB overhead at any permitted setting.
_BOUNDED_MARGIN_SECONDS = 10.0


def _auth_headers(org_id: uuid.UUID, account_id: uuid.UUID, role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        subject=f"user-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role=role,
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


async def _insert_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    name: str | None = None,
) -> uuid.UUID:
    """Minimal committed pipelines row (own row - never the shared fixture).

    ``pipelines.account_id`` is a NOT NULL FK to ``accounts``, so a valid
    account is required - the session-scoped ``test_user`` fixture supplies it.
    """
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, :default)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "default": _MANUAL,
                "name": name or f"lock-timeout-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM runs WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM pipeline_edges WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})


async def test_contended_patch_times_out_with_a_mapped_409(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A held row lock turns the PATCH into a bounded 409, not a hang or a 503."""
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    # The whole timing contract derives from the SETTING, not from constants:
    # MUTATION_ROW_LOCK_TIMEOUT_MS is operator-tunable across 100 ms..30 s, and
    # a hard-coded 30 s client timeout would give up BEFORE the server answered
    # at the field's max - the mapping would then be unobservable and the test
    # would collapse precisely when it is tuned.
    expected_seconds = get_settings().mutation_row_lock_timeout_ms / 1000
    # The client must outlive the server's bounded wait by enough to OBSERVE the
    # answer; the upper-bound assertion sits just above the wait so it still
    # proves the wait ENDED (bounded) rather than hanging.
    client_timeout = expected_seconds + _CLIENT_MARGIN_SECONDS
    bounded_ceiling = expected_seconds + _BOUNDED_MARGIN_SECONDS
    try:
        # Concurrent holder: takes the row lock and KEEPS IT OPEN across the
        # whole PATCH - nothing releases it, so only the bounded lock_timeout
        # can end the wait.
        async with db_engine.connect() as holder:
            await holder.execute(
                text("SELECT id FROM pipelines WHERE id = :id FOR UPDATE"),
                {"id": str(pipeline_id)},
            )

            started = time.monotonic()
            resp = await integration_client.patch(
                f"/api/v1/pipelines/{pipeline_id}",
                json={"description": "lock-timeout probe"},
                headers=_auth_headers(test_org, test_user, role="admin"),
                # The wait is the lock_timeout; the client must not
                # give up before the SERVER answers, or the mapping is never
                # observable.
                timeout=client_timeout,
            )
            elapsed = time.monotonic() - started
            await holder.rollback()

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "Timed out waiting for a lock" in detail, detail
        # The wait really happened (it did not resolve instantly)...
        assert elapsed >= expected_seconds * 0.5, (
            f"the wait ended after {elapsed:.2f}s - before the {expected_seconds}s lock_timeout could fire"
        )
        # ...and it was really BOUNDED (the holder never released the lock).
        assert elapsed < bounded_ceiling, f"the wait was NOT bounded - {elapsed:.2f}s for a held row lock"

        # The rejected PATCH left the row untouched.
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT description FROM pipelines WHERE id = :id"),
                    {"id": str(pipeline_id)},
                )
            ).scalar_one()
        assert stored != "lock-timeout probe", "the timed-out PATCH must not have been applied"
    finally:
        await _cleanup(db_engine, pipeline_id)


async def test_contended_clone_name_check_times_out_with_a_mapped_409(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-1313: the clone's target-name lock degrades to the same bounded 409.

    The clone transaction's FIRST lock is ``check_pipeline_name_available``'s
    ``SELECT ... FOR UPDATE`` on the row carrying the target name - it runs
    BEFORE the clone's in-txn team gate (FAR-1276), which only fires inside
    ``clone_pipeline``'s step-(a) commit hook, so the gate's own
    ``set_config`` could not bound it. A concurrent holder on that exact name
    row therefore used to park the request with no bound at all; with
    ``_set_mutation_row_lock_timeout`` at the top of the transaction it must
    time out (SQLSTATE 55P03) and map to the same 409 within
    ``Settings.mutation_row_lock_timeout_ms``.
    """
    target_name = f"lock-timeout-clone-target-{uuid.uuid4().hex[:8]}"
    source_id = await _insert_pipeline(db_engine, test_org, test_user)
    target_row_id = await _insert_pipeline(db_engine, test_org, test_user, name=target_name)
    # Same setting-derived timing contract as the PATCH test above: the bound
    # is operator-tunable (100 ms..30 s), so both the client's patience and
    # the "bounded" ceiling follow the SETTING rather than a constant.
    expected_seconds = get_settings().mutation_row_lock_timeout_ms / 1000
    client_timeout = expected_seconds + _CLIENT_MARGIN_SECONDS
    bounded_ceiling = expected_seconds + _BOUNDED_MARGIN_SECONDS
    try:
        # Concurrent holder: locks the ROW THAT CARRIES THE TARGET NAME (the
        # row check_pipeline_name_available would lock), and keeps it open
        # across the whole clone - nothing releases it, so only the bounded
        # lock_timeout can end the wait.
        async with db_engine.connect() as holder:
            await holder.execute(
                text("SELECT id FROM pipelines WHERE id = :id FOR UPDATE"),
                {"id": str(target_row_id)},
            )

            started = time.monotonic()
            resp = await integration_client.post(
                f"/api/v1/pipelines/{source_id}/clone",
                json={"name": target_name},
                headers=_auth_headers(test_org, test_user, role="admin"),
                timeout=client_timeout,
            )
            elapsed = time.monotonic() - started
            await holder.rollback()

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "Timed out waiting for a lock" in detail, detail
        # The wait really happened (it did not resolve instantly)...
        assert elapsed >= expected_seconds * 0.5, (
            f"the wait ended after {elapsed:.2f}s - before the {expected_seconds}s lock_timeout could fire"
        )
        # ...and it was really BOUNDED (the holder never released the lock).
        assert elapsed < bounded_ceiling, f"the wait was NOT bounded - {elapsed:.2f}s for a held name-row lock"

        # The timed-out clone created nothing: only the holder's own row still
        # carries the target name.
        async with db_engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pipelines "
                        "WHERE organisation_id = :oid AND name = :name AND deleted_at IS NULL"
                    ),
                    {"oid": str(test_org), "name": target_name},
                )
            ).scalar_one()
        assert count == 1, f"the timed-out clone created {count - 1} row(s) named {target_name!r}"
    finally:
        await _cleanup(db_engine, source_id)
        await _cleanup(db_engine, target_row_id)

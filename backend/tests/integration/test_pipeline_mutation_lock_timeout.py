"""ITEM 5: the in-txn row lock wait is BOUNDED - and a timeout maps to 409.

``_reapply_team_gate_inside_mutation_txn`` issues ``SELECT ... FOR UPDATE`` as
the first lock of every pipeline mutation transaction. Before ITEM 5 that wait
was unbounded: a contended PATCH parked a pooled connection on the row lock
until the holder committed (or forever).

This exercises the REAL bound against real Postgres: a concurrent transaction
holds the pipeline row lock, a PATCH arrives, and the endpoint must come back
with HTTP 409 + the lock-timeout detail (SQLSTATE 55P03 mapped by
``handle_db_errors``) rather than hanging or answering the generic 503.

The wait is ``Settings.mutation_row_lock_timeout_ms`` (5 s by default), so this
test takes ~5 s by design - it is timing the bounded wait, not a fixed sleep.
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


async def _insert_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
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
                "name": f"lock-timeout-{pipeline_id.hex[:8]}",
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
                # The wait is the lock_timeout (~5 s); the client must not
                # give up before the SERVER answers, or the mapping is never
                # observable.
                timeout=30.0,
            )
            elapsed = time.monotonic() - started
            await holder.rollback()

        expected_seconds = get_settings().mutation_row_lock_timeout_ms / 1000
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "Timed out waiting for a lock" in detail, detail
        # The wait really happened (it did not resolve instantly)...
        assert elapsed >= expected_seconds * 0.5, (
            f"the wait ended after {elapsed:.2f}s - before the {expected_seconds}s lock_timeout could fire"
        )
        # ...and it was really BOUNDED (the holder never released the lock).
        assert elapsed < 30.0, f"the wait was NOT bounded - {elapsed:.2f}s for a held row lock"

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

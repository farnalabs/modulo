"""FAR-1222: the ceiling/default merge must validate against the LOCKED row.

``update_pipeline_endpoint`` merges ``default_autonomy_level`` and
``max_autonomy_level`` (a PATCH may set only one of the pair) and rejects
``ceiling < default``. Before FAR-1222 the merge read the row with an
UNLOCKED SELECT, then the in-txn team gate re-read it ``FOR UPDATE`` — two
concurrent PATCHes could each validate against their own stale snapshot and
commit an inverted pair.

This runs against real Postgres (testcontainer) with two interleaved
transactions:

1. txn1 raises ``default_autonomy_level`` while holding the row lock.
2. A plain (unlocked) read taken at that moment still sees the OLD default —
   the TOCTOU window the fix closes.
3. txn2's FIRST read of the row is the ``FOR UPDATE`` one (the fixed endpoint
   order). It must BLOCK while txn1 holds the lock, then observe the COMMITTED
   default — so ``validate_autonomy_ceiling`` rejects the ceiling that the
   stale read would have accepted.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.core.run_context.autonomy import validate_autonomy_ceiling

pytestmark = pytest.mark.integration

_LOCKED_READ = "SELECT default_autonomy_level FROM pipelines WHERE id = :id FOR UPDATE"
_UNLOCKED_READ = "SELECT default_autonomy_level FROM pipelines WHERE id = :id"


async def _read_default(db_engine: AsyncEngine, pipeline_id: uuid.UUID, *, for_update: bool) -> str:
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text(_LOCKED_READ if for_update else _UNLOCKED_READ),
                {"id": str(pipeline_id)},
            )
        ).scalar_one()
    return str(row)


async def _insert_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level, max_autonomy_level) "
                "VALUES (:id, :oid, :name, 10, 30, 300, '{}'::json, '[]'::json, "
                "'manual_approval', NULL)",
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": f"ceiling-lock-{pipeline_id.hex[:8]}"},
        )
    return pipeline_id


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM pipelines WHERE id = :id"),
            {"id": str(pipeline_id)},
        )


async def test_merge_validates_against_the_row_the_transaction_locks(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    pipeline_id = await _insert_pipeline(db_engine, test_org)
    locked_task: asyncio.Task[str] | None = None
    try:
        async with db_engine.connect() as writer, writer.begin():
            # txn1: raise the default while holding the pipeline row lock.
            await writer.execute(
                text("SELECT id FROM pipelines WHERE id = :id FOR UPDATE"),
                {"id": str(pipeline_id)},
            )
            await writer.execute(
                text("UPDATE pipelines SET default_autonomy_level = 'fully_autonomous' WHERE id = :id"),
                {"id": str(pipeline_id)},
            )

            # The unlocked read the pre-fix endpoint did FIRST: still stale,
            # because txn1 has not committed. Validating against it would
            # accept a 'notify_on_complete' ceiling under a
            # 'fully_autonomous' default.
            stale_default = await _read_default(db_engine, pipeline_id, for_update=False)
            assert stale_default == "manual_approval"

            # The fixed endpoint's FIRST read is the locked one — it blocks.
            locked_task = asyncio.create_task(_read_default(db_engine, pipeline_id, for_update=True))
            done, _pending = await asyncio.wait({locked_task}, timeout=0.5)
            assert not done, "the locked read returned while another transaction held the row lock"
        # writer.begin() has COMMITTED: txn1's write is now visible.

        locked_default = await asyncio.wait_for(locked_task, timeout=10)
        assert locked_default == "fully_autonomous"

        # Against the LOCKED row the pair is rejected...
        with pytest.raises(ValueError, match="must be >="):
            validate_autonomy_ceiling(locked_default, "notify_on_complete", lenient=True)
        # ...while the same pair validated against the STALE read passes,
        # which is exactly the interleaving that used to commit
        # ceiling < default.
        validate_autonomy_ceiling(stale_default, "notify_on_complete", lenient=True)
    finally:
        if locked_task is not None and not locked_task.done():
            locked_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await locked_task
        await _cleanup(db_engine, pipeline_id)

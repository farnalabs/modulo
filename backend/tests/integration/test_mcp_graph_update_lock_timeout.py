"""FAR-1361: a contended MCP graph write degrades within the bounded lock timeout.

The REST mutation endpoints bound their row-lock wait
(``set_mutation_row_lock_timeout`` (db.crud.row_lock), FAR-1313) and this test proves the MCP
graph-update transaction now does too - against REAL Postgres, with a real
holder holding the real lock:

* a concurrent transaction holds ``FOR UPDATE`` on the pipeline row and never
  releases it, so only the bounded ``lock_timeout`` can end the wait,
* ``update_pipeline_graph`` must come back with the structured ``lock_timeout``
  error (the 55P03 map) rather than parking a pooled connection indefinitely,
* the wait must be long enough that the timeout actually FIRED and short enough
  to prove it was BOUNDED (the holder never released),
* the rejected write must leave the stored graph untouched.

The bound is read from ``Settings.mutation_row_lock_timeout_ms``, so both the
timing assertions derive from the SETTING (operator-tunable across
100 ms..30 s) rather than from constants - same contract as
``tests/integration/test_pipeline_mutation_lock_timeout.py``.

``mcp_server._session`` is replaced with a real session on ``db_engine`` (the
pattern ``test_mcp_trigger_update_config_guard.py`` establishes): the impl's
own lock-timeout line still runs against that session, and the session factory
itself is out of scope for this test.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.api import mcp_server as ms
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_MANUAL = "manual_approval"
#: Seconds added to ``mutation_row_lock_timeout_ms`` for the "bounded" ceiling -
#: tight enough that an UNBOUNDED wait (the holder never releases) still fails,
#: loose enough for request/DB overhead at any permitted setting.
_BOUNDED_MARGIN_SECONDS = 10.0


async def _insert_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> uuid.UUID:
    """Minimal committed pipelines row (own row - never the shared fixture)."""
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
                "name": f"mcp-lock-timeout-{pipeline_id.hex[:8]}",
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


async def _read_graph(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> object:
    async with db_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT graph_nodes_json FROM pipelines WHERE id = :id"),
                {"id": str(pipeline_id)},
            )
        ).scalar_one()


async def test_contended_mcp_graph_update_degrades_within_the_bounded_lock_timeout(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A held row lock turns the MCP graph write into a bounded lock_timeout error."""
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    expected_seconds = get_settings().mutation_row_lock_timeout_ms / 1000
    bounded_ceiling = expected_seconds + _BOUNDED_MARGIN_SECONDS
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

    @asynccontextmanager
    async def real_session(org_id: uuid.UUID):
        # Mirrors mcp_server._session: RLS org context, then a transaction that
        # COMMITS on a clean exit. The impl's bounded lock_timeout runs inside it.
        async with factory() as s, s.begin():
            await set_rls_org(s, org_id)
            yield s

    tokens = (
        ms._ctx_org_id.set(test_org),
        ms._ctx_user_id.set(test_user),
        ms._ctx_role.set("admin"),
    )
    try:
        with (
            patch.object(ms, "_session", real_session),
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_check_agent_tool_scope"),
        ):
            # Concurrent holder: takes the row lock and KEEPS IT OPEN across the
            # whole MCP call - nothing releases it, so only the bounded
            # lock_timeout can end the wait.
            async with db_engine.connect() as holder:
                await holder.execute(
                    text("SELECT id FROM pipelines WHERE id = :id FOR UPDATE"),
                    {"id": str(pipeline_id)},
                )

                started = time.monotonic()
                result = await ms.update_pipeline_graph(
                    pipeline_id=str(pipeline_id),
                    nodes=[],
                    edges=[],
                )
                elapsed = time.monotonic() - started
                await holder.rollback()
    finally:
        ms._ctx_org_id.reset(tokens[0])
        ms._ctx_user_id.reset(tokens[1])
        ms._ctx_role.reset(tokens[2])

    try:
        assert result.get("error") == "lock_timeout", result
        assert "another change is in progress" in result["detail"], result
        # The wait really happened (it did not resolve instantly)...
        assert elapsed >= expected_seconds * 0.5, (
            f"the wait ended after {elapsed:.2f}s - before the {expected_seconds}s lock_timeout could fire"
        )
        # ...and it was really BOUNDED (the holder never released the lock).
        assert elapsed < bounded_ceiling, f"the wait was NOT bounded - {elapsed:.2f}s for a held row lock"

        # The rejected write left the graph untouched.
        assert not await _read_graph(db_engine, pipeline_id)
    finally:
        await _cleanup(db_engine, pipeline_id)

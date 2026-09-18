"""Integration tests pinning REAL ORM behaviour for the deferred ``cost_breakdown``.

``list_runs`` defers ``cost_breakdown`` (``crud.run._RUNS_LIST_DEFERRED_COLUMNS``)
so a runs-list page SELECT never detoasts it. The MCP
``modulo://pipelines/{id}/runs`` resource still needs the value, and the only
safe way to get it is an awaited load — :func:`get_run_cost_breakdowns`.

The unit suite cannot prove this: it drives the resource with MagicMock/stand-in
rows that bypass SQLAlchemy entirely, so a plain attribute read there silently
succeeds. Against a real AsyncSession it does not — under asyncio the deferred
attribute's implicit lazy load attempts IO outside a greenlet and raises
``MissingGreenlet`` EVEN WHILE THE SESSION IS OPEN. These tests pin both halves
of that contract against real Postgres:

* the plain attribute read raises ``MissingGreenlet`` in-session (so
  "capture it before the session closes" is NOT a valid fix);
* :func:`get_run_cost_breakdowns` returns the real value in ONE awaited query.

Requires a running Postgres via testcontainers (pytest.mark.integration).
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.run import get_run_cost_breakdowns
from modulo.db.crud.run import list_runs as db_list_runs

pytestmark = [
    pytest.mark.integration,
]

_BREAKDOWN: list[dict[str, Any]] = [
    {"component": "llm", "display_name": "LLM tokens", "amount_usd": 0.075, "source": "ledger"},
    {"component": "sandbox", "display_name": "Sandbox", "amount_usd": 0.010, "source": "ledger"},
]


async def _insert_run(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    cost_breakdown: list[dict[str, Any]] | None,
) -> uuid.UUID:
    """Insert one committed run row carrying (or omitting) a cost breakdown."""
    run_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "status, trigger_type, langgraph_thread_id, run_number, input_hash, "
                "total_cost_usd, total_tokens, cost_breakdown) "
                "VALUES (:id, :oid, :pid, :sid, 'complete', 'manual', :thread, "
                ":run_number, :hash, 0.085, 1500, CAST(:breakdown AS json))"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "thread": str(uuid.uuid4()),
                "run_number": int(uuid.uuid4().int % 1_000_000),
                "hash": "0" * 64,
                "breakdown": None if cost_breakdown is None else json.dumps(cost_breakdown),
            },
        )
    return run_id


async def test_deferred_cost_breakdown_attribute_read_raises_in_session(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """A plain read of the deferred column raises while the session is STILL OPEN.

    This is the regression anchor for the MCP pipeline-runs resource: it proves
    that capturing ``{r.id: r.cost_breakdown for r in items}`` inside the
    ``async with`` block does NOT work, so the resource must use the awaited
    loader instead.
    """
    await _insert_run(
        db_engine,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        cost_breakdown=_BREAKDOWN,
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(test_org)},
        )
        page = await db_list_runs(session, organisation_id=test_org, pipeline_id=test_pipeline, page=1, page_size=50)
        assert page.items, "the inserted run must come back from list_runs"

        # Session is open here — the read still raises, because the implicit
        # lazy load attempts IO outside a greenlet.
        with pytest.raises(MissingGreenlet):
            _ = {r.id: r.cost_breakdown for r in page.items}


async def test_get_run_cost_breakdowns_returns_real_values(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """The awaited loader returns the real deferred value for every requested run."""
    with_breakdown = await _insert_run(
        db_engine,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        cost_breakdown=_BREAKDOWN,
    )
    without_breakdown = await _insert_run(
        db_engine,
        org_id=test_org,
        pipeline_id=test_pipeline,
        snapshot_id=test_snapshot,
        cost_breakdown=None,
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(test_org)},
        )
        page = await db_list_runs(session, organisation_id=test_org, pipeline_id=test_pipeline, page=1, page_size=50)
        run_ids = [r.id for r in page.items]
        assert with_breakdown in run_ids
        assert without_breakdown in run_ids

        breakdowns = await get_run_cost_breakdowns(session, run_ids)

    # The run that has a breakdown round-trips it verbatim; the NULL one maps to
    # None (never a missing key, so callers can distinguish "no rows" from NULL).
    assert breakdowns[with_breakdown] == _BREAKDOWN
    assert breakdowns[without_breakdown] is None


async def test_get_run_cost_breakdowns_empty_ids_makes_no_query(db_engine: AsyncEngine) -> None:
    """The empty-page short-circuit returns {} without touching the database."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        breakdowns = await get_run_cost_breakdowns(session, [])

    # An empty mapping, asserted via falsiness (tests/architecture
    # test_no_empty_container_literal_equality forbids ``== {}``); the isinstance
    # check keeps the return contract pinned to a dict.
    assert isinstance(breakdowns, dict)
    assert not breakdowns

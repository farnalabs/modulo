"""Tier-1 regression test for the FAR-2053 delivery veto's READER CHAIN.

The veto definition itself (``watchdog_retry._run_already_delivered``), its
call site in ``watchdog_retry_after_policy`` and the zombie watchdog's opt-in
(``veto_if_delivered=True``) are covered by
``test_pipeline_execution_watchdog_retry.py``. This module keeps the ONE thing
a mocked reader cannot prove: that the canonical reader chain
(``_read_run_markers`` -> ``read_run_markers`` -> ``read_run_blobs``) reads
markers out of the REAL schema.

That is the shape that catches the dropped-column class of bug (migration 0215
dropped ``runs.raw_output_markers``): every mocked test passed while production
read a column that no longer existed. Markers are seeded with the REAL writer
(``write_run_markers``) against a real SQLite engine, so the round trip cannot
drift from the shipped schema.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import ARRAY
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import modulo.db.models  # noqa: F401 — register every model on ``Base.metadata``
from modulo.core.pipeline_engine.watchdog_retry import (
    _read_run_markers,
    _run_already_delivered,
)
from modulo.db.crud.run_node_outputs import write_run_markers
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_DELIVERY_KEY = "run:{run_id}:node:n1:result"


@pytest.fixture
async def sqlite_engine(tmp_path) -> AsyncGenerator[AsyncEngine, None]:
    """File-backed SQLite with every ARRAY-free table created.

    The repo's SQLite pattern (see ``tests/unit/test_multi_backend_sqlite.py``):
    ``create_all`` skips tables carrying Postgres-only ``ARRAY`` columns.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delivery-veto.db'}")
    tables_to_create = [t for t in Base.metadata.sorted_tables if not any(isinstance(c.type, ARRAY) for c in t.columns)]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables_to_create))
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_run(engine: AsyncEngine, *, delivery_done: bool) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert the FK chain a run needs, then seed markers with the REAL writer."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    if delivery_done:
        markers: dict[str, object] = {_DELIVERY_KEY.format(run_id=run_id): {"delivery_done": True}}
    else:
        markers = {_DELIVERY_KEY.format(run_id=run_id): {"status": "success"}}

    async with AsyncSession(engine) as session:
        async with session.begin():
            session.add(Organisation(id=org_id, name="Veto Org", slug=f"veto-org-{org_id.hex[:8]}"))
            session.add(Account(id=account_id, display_name="Veto Tester"))
        async with session.begin():
            session.add(
                Pipeline(
                    id=pipeline_id,
                    organisation_id=org_id,
                    name="Veto Pipeline",
                    account_id=account_id,
                )
            )
            session.add(
                PipelineSnapshot(
                    id=snapshot_id,
                    organisation_id=org_id,
                    pipeline_id=pipeline_id,
                    snapshot_version=1,
                    graph_json={"nodes": [], "edges": []},
                    connector_bindings_json=[],
                    schema_pins_json=[],
                    prompt_pins_json=[],
                    model_backend_pins_json=[],
                )
            )
        async with session.begin():
            session.add(
                Run(
                    id=run_id,
                    organisation_id=org_id,
                    pipeline_id=pipeline_id,
                    snapshot_id=snapshot_id,
                    trigger_type="manual",
                    run_number=1,
                    input_hash="0" * 64,
                    langgraph_thread_id=f"thread-{run_id.hex}",
                )
            )
        async with session.begin():
            await set_rls_org(session, org_id)
            await write_run_markers(
                session,
                run_id=run_id,
                organisation_id=org_id,
                markers=markers,
            )
    return run_id, org_id


async def test_round_trip_through_real_reader_and_writer(sqlite_engine: AsyncEngine) -> None:
    """Prove the veto reads markers through the real repo reader, not a mock.

    Would have caught the migration-0215 dropped-column bug: the canonical
    reader chain is exercised end-to-end against a real database, with rows
    seeded by the real writer, and the canonical veto is resolved from it.
    """
    delivered_run, delivered_org = await _seed_run(sqlite_engine, delivery_done=True)

    markers = await _read_run_markers(sqlite_engine, run_id=delivered_run, org_id=delivered_org)
    assert markers is not None
    key = _DELIVERY_KEY.format(run_id=delivered_run)
    assert markers[key]["delivery_done"] is True
    assert await _run_already_delivered(sqlite_engine, run_id=delivered_run, org_id=delivered_org) is True

    plain_run, plain_org = await _seed_run(sqlite_engine, delivery_done=False)
    plain_markers = await _read_run_markers(sqlite_engine, run_id=plain_run, org_id=plain_org)
    assert plain_markers is not None
    plain_key = _DELIVERY_KEY.format(run_id=plain_run)
    assert "delivery_done" not in plain_markers[plain_key]
    assert await _run_already_delivered(sqlite_engine, run_id=plain_run, org_id=plain_org) is False

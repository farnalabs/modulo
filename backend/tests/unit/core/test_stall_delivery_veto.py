"""Regression tests for FAR-2053: delivery veto on the stall/zombie re-dispatch path.

The veto ensures a run whose markers carry ``delivery_done=True`` is never
re-dispatched when it stalls — the side effect already happened and a retry
risks delivering twice.

Two tiers:

* unit tests patch the ``_read_delivery_markers`` seam and cover the veto's
  decision logic plus the real ``zombie_watchdog`` wiring;
* one tier-1 round-trip test drives the REAL reader chain
  (``_read_delivery_markers`` → ``read_run_markers`` → ``read_run_blobs``)
  against a real SQLite engine seeded with the REAL writer. That is the test
  that would have caught the dropped-column bug (migration 0215 dropped
  ``runs.raw_output_markers``): every mocked test passed while production read
  a column that no longer exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import ARRAY
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import modulo.db.models  # noqa: F401 — register every model on ``Base.metadata``
from modulo.core.pipeline_execution import (
    EXECUTOR_STALLED_ERROR_CODE,
    _read_delivery_markers,
    _run_delivery_veto,
    zombie_watchdog,
)
from modulo.db.crud.run_node_outputs import write_run_markers
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_VETO_SEAM = "modulo.core.pipeline_execution._read_delivery_markers"

_DELIVERY_KEY = "run:{run_id}:node:n1:result"


def _delivery_marker(run_id: str) -> dict[str, object]:
    return {_DELIVERY_KEY.format(run_id=run_id): {"delivery_done": True}}


def _non_delivery_marker(run_id: str) -> dict[str, object]:
    return {_DELIVERY_KEY.format(run_id=run_id): {"status": "success"}}


class TestRunDeliveryVeto:
    """``_run_delivery_veto`` decision logic, with the reader seam patched."""

    async def test_returns_true_when_any_marker_is_delivery_done(self) -> None:
        run_id = str(uuid.uuid4())
        with patch(_VETO_SEAM, new_callable=AsyncMock) as read:
            read.return_value = _delivery_marker(run_id)

            result = await _run_delivery_veto(MagicMock(), run_id, str(uuid.uuid4()))

        assert result is True

    async def test_returns_false_when_no_marker_is_delivery_done(self) -> None:
        run_id = str(uuid.uuid4())
        with patch(_VETO_SEAM, new_callable=AsyncMock) as read:
            read.return_value = _non_delivery_marker(run_id)

            result = await _run_delivery_veto(MagicMock(), run_id, str(uuid.uuid4()))

        assert result is False

    async def test_returns_false_when_reader_returns_none(self) -> None:
        with patch(_VETO_SEAM, new_callable=AsyncMock) as read:
            read.return_value = None

            result = await _run_delivery_veto(MagicMock(), str(uuid.uuid4()), str(uuid.uuid4()))

        assert result is False

    async def test_returns_false_for_non_uuid_run_id_without_reading(self) -> None:
        """A non-UUID id names no persistable run, so nothing can be marked delivered."""
        with patch(_VETO_SEAM, new_callable=AsyncMock) as read:
            result = await _run_delivery_veto(MagicMock(), "run-1", str(uuid.uuid4()))

        assert result is False
        read.assert_not_awaited()

    async def test_fails_closed_on_read_error(self) -> None:
        """An unreadable marker set must block re-dispatch — never allow a
        possible double delivery."""
        with patch(_VETO_SEAM, new_callable=AsyncMock) as read:
            read.side_effect = Exception("DB connection failed")

            result = await _run_delivery_veto(MagicMock(), str(uuid.uuid4()), str(uuid.uuid4()))

        assert result is True

    async def test_propagates_cancelled_error(self) -> None:
        with patch(_VETO_SEAM, new_callable=AsyncMock) as read:
            read.side_effect = asyncio.CancelledError()

            with pytest.raises(asyncio.CancelledError):
                await _run_delivery_veto(MagicMock(), str(uuid.uuid4()), str(uuid.uuid4()))


class TestZombieWatchdogDeliveryVetoWiring:
    """Drive the REAL ``zombie_watchdog`` → ``_run_delivery_veto`` chain.

    Only the marker reader (the DB seam) and the two downstream decision
    functions are patched, so these tests fail if the veto wiring is removed.
    """

    async def test_delivery_done_blocks_redispatch(self) -> None:
        run_id = str(uuid.uuid4())
        exec_task = asyncio.create_task(asyncio.sleep(999))
        fail = AsyncMock()
        retry = AsyncMock(return_value=True)
        try:
            with (
                patch(_VETO_SEAM, new_callable=AsyncMock) as read,
                patch("modulo.core.pipeline_execution.fail_run_terminal", fail),
                patch("modulo.core.pipeline_execution._maybe_watchdog_retry", retry),
            ):
                read.return_value = _delivery_marker(run_id)
                await zombie_watchdog(
                    MagicMock(),
                    run_id,
                    str(uuid.uuid4()),
                    asyncio.Event(),
                    exec_task=exec_task,
                    grace_seconds=0.01,
                )
        finally:
            exec_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task

        retry.assert_not_awaited()
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == EXECUTOR_STALLED_ERROR_CODE

    async def test_no_delivery_marker_consults_retry_instead(self) -> None:
        run_id = str(uuid.uuid4())
        exec_task = asyncio.create_task(asyncio.sleep(999))
        fail = AsyncMock()
        retry = AsyncMock(return_value=True)
        try:
            with (
                patch(_VETO_SEAM, new_callable=AsyncMock) as read,
                patch("modulo.core.pipeline_execution.fail_run_terminal", fail),
                patch("modulo.core.pipeline_execution._maybe_watchdog_retry", retry),
            ):
                read.return_value = _non_delivery_marker(run_id)
                await zombie_watchdog(
                    MagicMock(),
                    run_id,
                    str(uuid.uuid4()),
                    asyncio.Event(),
                    exec_task=exec_task,
                    grace_seconds=0.01,
                )
        finally:
            exec_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task

        retry.assert_awaited_once()
        assert retry.await_args.kwargs["error_code"] == EXECUTOR_STALLED_ERROR_CODE
        fail.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tier-1: the REAL reader against a real database
# ---------------------------------------------------------------------------


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

    Would have caught the migration-0215 dropped-column bug: the reader chain
    is exercised end-to-end against a real database, with rows seeded by the
    real writer.
    """
    delivered_run, delivered_org = await _seed_run(sqlite_engine, delivery_done=True)

    markers = await _read_delivery_markers(
        sqlite_engine,
        run_uuid=delivered_run,
        org_uuid=delivered_org,
    )
    assert markers is not None
    key = _DELIVERY_KEY.format(run_id=delivered_run)
    assert markers[key]["delivery_done"] is True
    assert await _run_delivery_veto(sqlite_engine, str(delivered_run), str(delivered_org)) is True

    plain_run, plain_org = await _seed_run(sqlite_engine, delivery_done=False)
    plain_markers = await _read_delivery_markers(
        sqlite_engine,
        run_uuid=plain_run,
        org_uuid=plain_org,
    )
    assert plain_markers is not None
    plain_key = _DELIVERY_KEY.format(run_id=plain_run)
    assert "delivery_done" not in plain_markers[plain_key]
    assert await _run_delivery_veto(sqlite_engine, str(plain_run), str(plain_org)) is False

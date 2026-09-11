"""Unit tests for the run-status write's blob persistence (FAR-125 P1a -> FAR-583 B1).

Historical name ``test_node_telemetry_column.py``: the FAR-125 Agent Return
Contract split (per-node telemetry alongside outputs) was originally pinned
against the legacy ``runs.outputs_json`` / ``runs.node_telemetry_json`` ORM
columns. B1 (FAR-583) cut those columns' ORM mapping — the ``run_node_outputs``
per-node store is now the ONLY store, written by the REPLACE write
(:func:`modulo.db.crud.run_node_outputs.replace_run_node_outputs`) that
:func:`modulo.db.crud.run.update_run_status` performs INSIDE the status-write
transaction. The invariants this file pins survived the cut:

* a blobs-carrying status write persists BOTH sides (outputs + telemetry)
  atomically with the status flip — one transaction, never a torn half-state;
* a side the caller does not pass stays ABSENT (SQL NULL on the per-node
  row), never the JSON ``null`` VALUE, and a second write REPLACES the store
  (the delete-absent shrink);
* a telemetry-only write persists telemetry without outputs;
* a missing run returns None and performs no store write.

DB-backed cases run on in-memory SQLite with ``Base.metadata.create_all`` over
the involved tables only (the ``test_run_outputs_dualwrite`` harness); the
legacy blob columns are re-added with ALTERs so the repo module's raw Core
legacy-table legs run against the migrated shape.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import update_run_status
from modulo.db.crud.run_node_outputs import read_run_blobs
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, RunNodeOutput
from modulo.db.rls import set_rls_org

_ORG = uuid.uuid4()
_PIPELINE = uuid.uuid4()
_SNAPSHOT = uuid.uuid4()

_RUN_AND_ORG_TABLES = (Organisation.__table__, Run.__table__, RunNodeOutput.__table__)


async def _now_sqlite(engine: AsyncEngine) -> None:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_now(dbapi_connection: Any, connection_record: Any) -> None:
        dbapi_connection.create_function("now", 0, lambda: "now")


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    await _now_sqlite(eng)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_RUN_AND_ORG_TABLES))
        # FAR-583 B1: the legacy blob columns left the ORM mapping but remain
        # IN THE DATABASE until B2b — the status write's pre-write capture and
        # the repo readers' EMPTY fallback select them, so the test schema
        # reproduces the migrated shape.
        for legacy_col in ("outputs_json", "node_telemetry_json", "raw_output_markers"):
            await conn.exec_driver_sql(f"ALTER TABLE runs ADD COLUMN {legacy_col} JSON")
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
def sqlite_sessionmaker(sqlite_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)


async def _seed_run(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, 'blob-persist org', :slug, '{}', '{}')"
            ),
            {"id": str(_ORG), "slug": f"blob-persist-{_ORG.hex[:12]}"},
        )
        await session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id, cancellation_requested) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'running', 1, 'ih', :thread, 0)"
            ),
            {
                "id": run_id.hex,
                "oid": _ORG.hex,
                "pid": _PIPELINE.hex,
                "sid": _SNAPSHOT.hex,
                "thread": f"blob-persist-{run_id}",
            },
        )


async def _seed_org_only(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, 'blob-persist org', :slug, '{}', '{}')"
            ),
            {"id": str(_ORG), "slug": f"blob-persist-{_ORG.hex[:12]}"},
        )


async def _legacy_blobs(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> tuple[Any, Any]:
    async with maker() as session, session.begin():
        row = (
            await session.execute(
                text("SELECT outputs_json, node_telemetry_json FROM runs WHERE id = :rid"),
                {"rid": run_id.hex},
            )
        ).first()
        assert row is not None
        return row[0], row[1]


async def _store_rows(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> list[Any]:
    async with maker() as session, session.begin():
        return (
            (
                await session.execute(
                    select(RunNodeOutput).where(RunNodeOutput.run_id == run_id).order_by(RunNodeOutput.node_id)
                )
            )
            .scalars()
            .all()
        )


async def _status(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> str:
    async with maker() as session, session.begin():
        row = (await session.execute(select(Run.status).where(Run.id == run_id))).first()
    assert row is not None
    return str(row[0])


async def _write_status(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    status: str,
    **blobs: Any,
) -> Any:
    async with maker() as session, session.begin():
        await set_rls_org(session, _ORG)
        return await update_run_status(session, run_id, status, **blobs)


# ---------------------------------------------------------------------------
# The status write persists both sides on the store, atomically
# ---------------------------------------------------------------------------


class TestStatusWriteBlobPersistence:
    async def test_both_sides_persist_atomically_with_the_status_flip(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A blobs-carrying status write lands BOTH sides on run_node_outputs
        inside the SAME transaction as the status flip — never a torn
        half-state (the FAR-125 P1a invariant, re-expressed on the store)."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        outputs = {"n1": {"answer": 42}}
        telemetry = {"n1": {"status": "completed", "wall_clock_time_ms": 1200, "exit_code": 0}}

        result = await _write_status(
            sqlite_sessionmaker,
            run_id,
            "complete",
            outputs_json=outputs,
            node_telemetry_json=telemetry,
        )

        assert result is not None
        assert await _status(sqlite_sessionmaker, run_id) == "complete"
        rows = await _store_rows(sqlite_sessionmaker, run_id)
        final_rows = [r for r in rows if r.attempt_key == FINAL_ATTEMPT_KEY and r.node_id == "n1"]
        assert len(final_rows) == 1
        assert final_rows[0].outputs_json == {"answer": 42}
        assert final_rows[0].node_telemetry_json == {"status": "completed", "wall_clock_time_ms": 1200, "exit_code": 0}
        # The legacy columns are NEVER written post-B1 (the store is the only
        # store until B2b drops them).
        legacy_outputs, legacy_telemetry = await _legacy_blobs(sqlite_sessionmaker, run_id)
        assert legacy_outputs is None
        assert legacy_telemetry is None

    async def test_absent_telemetry_side_stays_absent_not_null_valued(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A side the caller does not pass stays ABSENT (SQL NULL on the
        per-node row), never the JSON ``null`` VALUE — the lossless-mapping
        distinction the readers reassemble from."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)

        result = await _write_status(sqlite_sessionmaker, run_id, "complete", outputs_json={"n1": {"answer": 42}})

        assert result is not None
        rows = await _store_rows(sqlite_sessionmaker, run_id)
        final_rows = [r for r in rows if r.attempt_key == FINAL_ATTEMPT_KEY and r.node_id == "n1"]
        assert len(final_rows) == 1
        assert final_rows[0].outputs_json == {"answer": 42}
        # ABSENT = SQL NULL; a JSON null VALUE would reassemble as the explicit
        # None-key side and corrupt the round-trip.
        assert final_rows[0].node_telemetry_json is None
        legacy_outputs, legacy_telemetry = await _legacy_blobs(sqlite_sessionmaker, run_id)
        assert legacy_outputs is None
        assert legacy_telemetry is None

    async def test_telemetry_only_write_persists_telemetry_without_outputs(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Telemetry alone (no outputs side) still lands on the store."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        telemetry = {"n1": {"status": "completed"}}

        result = await _write_status(sqlite_sessionmaker, run_id, "complete", node_telemetry_json=telemetry)

        assert result is not None
        rows = await _store_rows(sqlite_sessionmaker, run_id)
        final_rows = [r for r in rows if r.attempt_key == FINAL_ATTEMPT_KEY and r.node_id == "n1"]
        assert len(final_rows) == 1
        assert final_rows[0].outputs_json is None
        assert final_rows[0].node_telemetry_json == {"status": "completed"}

    async def test_second_write_replaces_the_store_shrink(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A second blobs-carrying status write REPLACES the store: the keys
        the newer payload omits are deleted (the delete-absent shrink), never
        merged — two writes re-written over the same run end in lockstep with
        the last payload. (A side the caller does not pass at all is left
        untouched — the REPLACE contract's None-side semantics.)"""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        await _write_status(
            sqlite_sessionmaker,
            run_id,
            "complete",
            outputs_json={"a": {"v": 1}, "b": {"v": 2}},
            node_telemetry_json={"a": {"t": 1}, "b": {"t": 2}},
        )
        await _write_status(
            sqlite_sessionmaker,
            run_id,
            "complete",
            outputs_json={"a": {"v": 3}},
            node_telemetry_json={"a": {"t": 1}},
        )

        rows = await _store_rows(sqlite_sessionmaker, run_id)
        final_nodes = sorted(r.node_id for r in rows if r.attempt_key == FINAL_ATTEMPT_KEY)
        assert final_nodes == ["a"]
        survivor = next(r for r in rows if r.node_id == "a" and r.attempt_key == FINAL_ATTEMPT_KEY)
        assert survivor.outputs_json == {"v": 3}
        assert survivor.node_telemetry_json == {"t": 1}

    async def test_omitted_side_is_left_untouched_not_deleted(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A side the caller does not pass (None) is LEFT UNTOUCHED by the
        REPLACE write — the legacy column-write semantics (a NULL SET clause
        leaves the column alone), pinned on the store."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        await _write_status(
            sqlite_sessionmaker,
            run_id,
            "complete",
            outputs_json={"a": {"v": 1}},
            node_telemetry_json={"a": {"t": 1}},
        )
        # Second write carries ONLY outputs — the stored telemetry side must
        # survive it.
        await _write_status(sqlite_sessionmaker, run_id, "complete", outputs_json={"a": {"v": 2}})

        rows = await _store_rows(sqlite_sessionmaker, run_id)
        survivor = next(r for r in rows if r.node_id == "a" and r.attempt_key == FINAL_ATTEMPT_KEY)
        assert survivor.outputs_json == {"v": 2}
        assert survivor.node_telemetry_json == {"t": 1}

    async def test_missing_run_writes_no_store_rows(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A status write for a MISSING run returns None and performs no
        store write — the original missing-run guard."""
        await _seed_org_only(sqlite_sessionmaker)
        missing = uuid.uuid4()

        result = await _write_status(
            sqlite_sessionmaker,
            missing,
            "complete",
            outputs_json={"n1": {"answer": 42}},
            node_telemetry_json={"n1": {"status": "completed"}},
        )

        assert result is None
        assert not await _store_rows(sqlite_sessionmaker, missing)

    async def test_reassembled_blobs_round_trip_through_the_reader(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The store write the status write performed is served intact by the
        repo reader (the reassembly contract the API surfaces consume)."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        outputs = {"n1": {"plan": "Step 1"}, "n2": {"code": "print('hi')"}}
        telemetry = {"n1": {"status": "completed", "wall_clock_time_ms": 900}}

        await _write_status(
            sqlite_sessionmaker,
            run_id,
            "complete",
            outputs_json=outputs,
            node_telemetry_json=telemetry,
        )

        async with sqlite_sessionmaker() as session, session.begin():
            await set_rls_org(session, _ORG)
            blobs = await read_run_blobs(session, run_id=run_id, organisation_id=_ORG)
        assert blobs.outputs == outputs
        assert blobs.telemetry == telemetry
        assert blobs.markers is None

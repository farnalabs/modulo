"""Round-trip unit tests for the run_node_outputs repo module (FAR-583).

Runs against a real in-memory SQLite database (no ORM tenant-filter listener,
no RLS — org context is set explicitly via ``set_rls_org`` exactly as the
generic-backend production path does), asserting the LOSSLESS mapping: the
reassembled legacy dict shapes, serialised with ``json.dumps(...,
default=str)``, are byte-identical to the legacy bytes — across the full
representation matrix: outputs-only / telemetry-only / both / per-node JSON
null / column-level NULL sides / ``{}`` (both + mixed) / NULL / colon node
ids / sentinel-named ids / ``__unknown__`` keys / multi-attempt markers /
order-hostile key ordering / shrinking REPLACE / the fenced markers reader /
malformed-metadata fail-open / inherited-sentinel filtering.

B2b: the legacy runs blob columns are DROPPED (migration 0212) - the
DIRECTION-AWARE fallback classes are gone with them; seeding is new-table
work through the repo writers.
"""

import itertools
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run_node_outputs import (
    FINAL_ATTEMPT_KEY,
    META_NODE_ID,
    QUARANTINE_TABLE,
    UNKNOWN_NODE_ID,
    NodeOutputWrite,
    OutputsSentinelViolation,
    RunBlobs,
    _jsonb_canonical_key,
    dialect_insert,
    parse_marker_node_id,
    read_node_output_blob_bytes,
    read_run_blobs,
    read_run_markers_fenced,
    read_run_node_outputs_raw,
    replace_run_node_outputs,
    upsert_rows,
    write_run_markers,
)
from modulo.db.models.base import Base
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import RunNodeOutput
from modulo.db.rls import OutputsRlsMismatch, set_rls_org

_TABLE_NAMES = {"organisations", "runs", "run_node_outputs"}

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")

_RUN_NUMBER = itertools.count(1)

_RUN_ID = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        # The quarantine side table has NO ORM model (migration-0192-owned,
        # Core-only in the repo module) - created here so the B2b-era parity
        # surface stays exercisable. B2b KEPT this table (post-0212 it is the
        # 0192-quarantined evidence's only surviving copy).
        await conn.run_sync(lambda sync_conn: QUARANTINE_TABLE.create(sync_conn, checkfirst=True))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_run(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID = _ORG_A,
    status: str = "complete",
    completed_at: datetime | None = None,
    claim_token: str | None = None,
    outputs: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    markers: dict[str, Any] | None = None,
) -> Run:
    run = Run(
        organisation_id=organisation_id,
        pipeline_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        trigger_type="manual",
        run_number=next(_RUN_NUMBER),
        input_hash="a" * 64,
        langgraph_thread_id="thread-" + uuid.uuid4().hex,
        status=status,
        completed_at=completed_at,
        claim_token=claim_token,
    )
    async with session.begin():
        session.add(run)
        await session.flush()
        # B2b: the blob STORE is run_node_outputs - seeding goes through the
        # repo writers (the legacy runs columns are gone with migration 0212).
        if outputs is not None or telemetry is not None:
            await set_rls_org(session, run.organisation_id)
            await replace_run_node_outputs(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                outputs=outputs,
                telemetry=telemetry,
            )
            await set_rls_org(session, None)
        if markers:
            await set_rls_org(session, run.organisation_id)
            await write_run_markers(session, run_id=run.id, organisation_id=run.organisation_id, markers=markers)
            await set_rls_org(session, None)
    return run


def _canonical(mapping: dict[str, Any]) -> dict[str, Any]:
    """The jsonb-canonical ordering the legacy column round-trips through."""
    return dict(sorted(mapping.items(), key=lambda item: _jsonb_canonical_key(item[0])))


def _assert_bytes_round_trip(actual: dict[str, Any] | None, expected: dict[str, Any] | None) -> None:
    assert json.dumps(actual, default=str) == json.dumps(expected, default=str)


async def _replace(
    session: AsyncSession,
    run: Run,
    *,
    outputs: dict[str, Any] | None,
    telemetry: dict[str, Any] | None,
) -> None:
    async with session.begin():
        await set_rls_org(session, run.organisation_id)
        await replace_run_node_outputs(
            session,
            run_id=run.id,
            organisation_id=run.organisation_id,
            outputs=outputs,
            telemetry=telemetry,
        )


async def _write_markers(session: AsyncSession, run: Run, markers: dict[str, Any]) -> None:
    async with session.begin():
        await set_rls_org(session, run.organisation_id)
        await write_run_markers(session, run_id=run.id, organisation_id=run.organisation_id, markers=markers)


async def _read_blobs(session: AsyncSession, run: Run, *, raw: bool = False) -> RunBlobs:
    """Reads run inside an explicit transaction - an autobegin transaction
    left open by a bare read would block the next explicit begin()."""
    async with session.begin():
        if raw:
            return await read_run_node_outputs_raw(session, run_id=run.id, organisation_id=run.organisation_id)
        return await read_run_blobs(session, run_id=run.id, organisation_id=run.organisation_id)


async def _read_bytes(session: AsyncSession, run: Run) -> dict[uuid.UUID, int]:
    async with session.begin():
        return await read_node_output_blob_bytes(session, [run.id])


def _meta_rows_query(run_id: uuid.UUID) -> Any:
    return select(RunNodeOutput).where(
        RunNodeOutput.run_id == run_id,
        RunNodeOutput.node_id == META_NODE_ID,
        RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
    )


def _attempt_key_rows_query(run_id: uuid.UUID, attempt_key: str) -> Any:
    return select(RunNodeOutput).where(RunNodeOutput.run_id == run_id, RunNodeOutput.attempt_key == attempt_key)


class TestTwinParser:
    def test_plain_and_colon_node_ids(self) -> None:
        assert parse_marker_node_id(f"run:{_RUN_ID}:node:node1:3") == "node1"
        assert parse_marker_node_id(f"run:{_RUN_ID}:node:my:weird:node:7") == "my:weird:node"
        assert parse_marker_node_id(f"run:{_RUN_ID}:node:a:b:claim-unknown") == "a:b"

    def test_unparseable_keys_map_to_unknown(self) -> None:
        assert parse_marker_node_id("junk") == UNKNOWN_NODE_ID
        assert parse_marker_node_id(f"run:{_RUN_ID}:node:nosuffix") == UNKNOWN_NODE_ID
        assert parse_marker_node_id(f"run:{_RUN_ID}:node::suffix") == UNKNOWN_NODE_ID
        assert parse_marker_node_id(f"run:{_RUN_ID}:node:x:") == UNKNOWN_NODE_ID

    def test_sentinel_named_ids_parse(self) -> None:
        assert parse_marker_node_id(f"run:{_RUN_ID}:node:__run_meta__:1") == "__run_meta__"


class TestDialectGuard:
    def test_mariadb_is_guarded_out(self) -> None:
        with pytest.raises(NotImplementedError):
            dialect_insert("mysql")


class TestRoundTripMatrix:
    async def test_outputs_only(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        outputs = {"n1": {"answer": 1}, "n2": {"deep": {"list": [1, 2]}}}
        await _replace(session, run, outputs=outputs, telemetry=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, _canonical(outputs))
        assert blobs.telemetry is None
        assert blobs.markers is None

    async def test_telemetry_only(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        telemetry = {"n1": {"status": "ok", "wall_clock_time_ms": 12}}
        await _replace(session, run, outputs=None, telemetry=telemetry)
        blobs = await _read_blobs(session, run)
        assert blobs.outputs is None
        _assert_bytes_round_trip(blobs.telemetry, _canonical(telemetry))

    async def test_both_sides_union_of_keys(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        outputs = {"shared": {"v": 1}, "only_out": {"v": 2}}
        telemetry = {"shared": {"ms": 5}, "only_tel": {"ms": 6}}
        await _replace(session, run, outputs=outputs, telemetry=telemetry)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, _canonical(outputs))
        _assert_bytes_round_trip(blobs.telemetry, _canonical(telemetry))

    async def test_per_node_json_null_value_is_preserved(self, session: AsyncSession) -> None:
        """An explicit JSON null VALUE (key present, value None) must survive:
        the reassembled dict keeps the key with a None value."""
        run = await _seed_run(session)
        outputs = {"null_node": None, "real": {"v": 1}}
        await _replace(session, run, outputs=outputs, telemetry=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, _canonical(outputs))

    async def test_column_level_null_side_is_absent(self, session: AsyncSession) -> None:
        """A side that LACKS the key is SQL NULL (absent) — the key must NOT
        appear in that side's reassembled dict even though the row exists."""
        run = await _seed_run(session)
        await _replace(session, run, outputs={"a": {"v": 1}}, telemetry={"a": {"ms": 1}, "b": {"ms": 2}})
        blobs = await _read_blobs(session, run)
        assert "b" in blobs.telemetry
        assert "b" not in blobs.outputs

    async def test_empty_dicts_both(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        await _replace(session, run, outputs={}, telemetry={})
        blobs = await _read_blobs(session, run)
        assert blobs.outputs is not None
        assert not blobs.outputs
        assert blobs.telemetry is not None
        assert not blobs.telemetry

    async def test_empty_outputs_mixed_with_populated_telemetry(self, session: AsyncSession) -> None:
        """The metadata flags payload is load bearing for the mixed case."""
        run = await _seed_run(session)
        telemetry = {"n1": {"ms": 3}}
        await _replace(session, run, outputs={}, telemetry=telemetry)
        blobs = await _read_blobs(session, run)
        assert blobs.outputs is not None
        assert not blobs.outputs
        _assert_bytes_round_trip(blobs.telemetry, _canonical(telemetry))

    async def test_null_side_is_not_empty(self, session: AsyncSession) -> None:
        """Legacy NULL (side absent) reads as None — never {} — even though a
        metadata row exists for the other side."""
        run = await _seed_run(session)
        await _replace(session, run, outputs=None, telemetry={})
        blobs = await _read_blobs(session, run)
        assert blobs.outputs is None
        assert blobs.telemetry is not None
        assert not blobs.telemetry

    async def test_colon_and_unicode_node_ids(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        outputs = {"my:node:x": {"v": 1}, "ünïcode": {"v": 2}, "hitl_gate_child": {"v": 3}}
        await _replace(session, run, outputs=outputs, telemetry=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, _canonical(outputs))

    async def test_order_hostile_keys_are_jsonb_canonical(self, session: AsyncSession) -> None:
        """Mixed-length keys must reassemble in jsonb canonical order
        (length-first, then bytewise) so the serialised bytes round-trip."""
        run = await _seed_run(session)
        outputs = {"zzzz": {"v": 1}, "a": {"v": 2}, "mm": {"v": 3}, "b0": {"v": 4}, "aaaaaaaa": {"v": 5}}
        await _replace(session, run, outputs=outputs, telemetry=None)
        blobs = await _read_blobs(session, run, raw=True)
        expected_order = [k for k, _ in sorted(outputs.items(), key=lambda item: _jsonb_canonical_key(item[0]))]
        assert list(blobs.outputs.keys()) == expected_order
        _assert_bytes_round_trip(blobs.outputs, _canonical(outputs))

    async def test_sentinel_named_node_id_in_outputs_is_rejected(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        with pytest.raises(OutputsSentinelViolation):
            await _replace(session, run, outputs={"__run_meta__": {"v": 1}}, telemetry=None)

    async def test_sentinel_attempt_key_in_markers_is_rejected(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        with pytest.raises(OutputsSentinelViolation):
            await _write_markers(session, run, {FINAL_ATTEMPT_KEY: {"raw": "x"}})

    async def test_rls_mismatched_org_write_raises(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        async with session.begin():
            await set_rls_org(session, _ORG_B)
            with pytest.raises(OutputsRlsMismatch):
                await replace_run_node_outputs(
                    session,
                    run_id=run.id,
                    organisation_id=_ORG_A,
                    outputs={"a": {"v": 1}},
                    telemetry=None,
                )

    async def test_write_without_rls_context_raises(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        async with session.begin():
            with pytest.raises(OutputsRlsMismatch):
                await replace_run_node_outputs(
                    session,
                    run_id=run.id,
                    organisation_id=_ORG_A,
                    outputs={"a": {"v": 1}},
                    telemetry=None,
                )


class TestReplaceSemantics:
    async def test_shrinking_outputs_dict_blanks_absent_keys(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        await _replace(session, run, outputs={"a": {"v": 1}, "b": {"v": 2}}, telemetry=None)
        await _replace(session, run, outputs={"a": {"v": 1}}, telemetry=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}})

    async def test_shrinking_side_keeps_the_other_side(self, session: AsyncSession) -> None:
        """A node carrying BOTH sides must keep its telemetry when the
        outputs dict shrinks past it (blank, never delete the row)."""
        run = await _seed_run(session)
        await _replace(session, run, outputs={"a": {"v": 1}, "b": {"v": 2}}, telemetry={"a": {"ms": 1}})
        await _replace(session, run, outputs={"a": {"v": 1}}, telemetry=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.telemetry, {"a": {"ms": 1}})
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}})

    async def test_metadata_row_is_dropped_when_no_side_is_empty(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        await _replace(session, run, outputs={}, telemetry=None)
        await _replace(session, run, outputs={"a": {"v": 1}}, telemetry=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}})
        async with session.begin():
            stale_meta = (await session.execute(_meta_rows_query(run.id))).first()
        assert stale_meta is None

    async def test_untouched_side_keeps_its_value(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        await _replace(session, run, outputs={"a": {"v": 1}}, telemetry={"a": {"ms": 9}})
        await _replace(session, run, outputs={"a": {"v": 2}}, telemetry=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 2}})
        _assert_bytes_round_trip(blobs.telemetry, {"a": {"ms": 9}})


class TestMarkers:
    async def test_multi_attempt_markers_round_trip(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        markers = {
            f"run:{run.id}:node:n1:0": {"raw": "first"},
            f"run:{run.id}:node:n1:1": {"raw": "second", "pr_url": "https://x/1"},
            f"run:{run.id}:node:n2:fallback": {"raw": "fb"},
        }
        await _write_markers(session, run, markers)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.markers, _canonical(markers))

    async def test_unparseable_key_preserved_as_unknown_row(self, session: AsyncSession) -> None:
        """Evidence preserved (FAR-188): the FULL original key is the
        attempt_key; the node id is the __unknown__ sentinel."""
        run = await _seed_run(session)
        markers = {"weird-legacy-key": {"raw": "keep me"}}
        await _write_markers(session, run, markers)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.markers, _canonical(markers))
        async with session.begin():
            row = (await session.execute(_attempt_key_rows_query(run.id, "weird-legacy-key"))).scalar_one_or_none()
        assert row is not None
        assert row.node_id == UNKNOWN_NODE_ID

    async def test_shrinking_markers_delete_absent_keys(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        await _write_markers(session, run, {f"run:{run.id}:node:a:0": {"raw": "1"}})
        await _write_markers(session, run, {f"run:{run.id}:node:b:0": {"raw": "2"}})
        blobs = await _read_blobs(session, run)
        assert set(blobs.markers.keys()) == {f"run:{run.id}:node:b:0"}


class TestFencedMarkersReader:
    """qa M4/M5: ONE fenced statement (runs row under the fence predicates,
    LEFT-JOINed to the marker rows) reassembling flat. B2b: the legacy leg
    is gone - a fence HIT with no marker rows reads ``None`` too.
    ``for_update`` renders FOR UPDATE OF runs on Postgres and is a no-op on
    SQLite."""

    async def _seed_running_run(
        self,
        session: AsyncSession,
        *,
        status: str = "running",
        claim_token: str | None = "tok-1",
        markers: dict[str, Any] | None = None,
    ) -> Run:
        return await _seed_run(
            session,
            status=status,
            claim_token=claim_token,
            markers=markers,
        )

    async def test_fenced_read_serves_reassembled_markers(self, session: AsyncSession) -> None:
        run = await self._seed_running_run(session)
        markers = {f"run:{run.id}:node:n1:0": {"raw": "first"}, "junk-key": {"raw": "keep"}}
        await _write_markers(session, run, markers)
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token="tok-1",
            )
        assert served == markers

    async def test_wrong_claim_token_reads_none(self, session: AsyncSession) -> None:
        """Fence miss (superseded executor's token) - the same visibility the
        predicate-fenced gate read had: NOTHING is served."""
        run = await self._seed_running_run(session, claim_token="tok-real")
        await _write_markers(session, run, {"k1": {"raw": "a"}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token="tok-stale",
            )
        assert served is None

    async def test_non_running_status_reads_none(self, session: AsyncSession) -> None:
        run = await self._seed_running_run(session, status="complete")
        await _write_markers(session, run, {"k1": {"raw": "a"}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token=None,
            )
        assert served is None

    async def test_no_marker_rows_serve_none(self, session: AsyncSession) -> None:
        """B2b: a fence hit with zero marker rows IS a markers-less run -
        ``None`` markers (no legacy column to fall back to)."""
        run = await self._seed_running_run(session)
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token="tok-1",
            )
        assert served is None

    async def test_for_update_true_is_accepted(self, session: AsyncSession) -> None:
        run = await self._seed_running_run(session)
        await _write_markers(session, run, {"k1": {"raw": "a"}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token="tok-1",
                for_update=True,
            )
        assert served == {"k1": {"raw": "a"}}

    async def test_single_statement_read(self, engine: AsyncEngine) -> None:
        """The fence + reassembly compose into ONE SQL statement (statement
        counter on the engine)."""
        maker = async_sessionmaker(engine, expire_on_commit=False)
        statements: list[str] = []

        def _count(conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any) -> None:
            statements.append(str(statement)[:120])

        event.listen(engine.sync_engine, "before_cursor_execute", _count)
        try:
            async with maker() as sess, sess.begin():
                run = Run(
                    organisation_id=_ORG_A,
                    pipeline_id=uuid.uuid4(),
                    snapshot_id=uuid.uuid4(),
                    trigger_type="manual",
                    run_number=next(_RUN_NUMBER),
                    input_hash="a" * 64,
                    langgraph_thread_id="thread-" + uuid.uuid4().hex,
                    status="running",
                    claim_token="tok-1",
                )
                sess.add(run)
                await sess.flush()
                await upsert_rows(
                    sess,
                    run_id=run.id,
                    organisation_id=_ORG_A,
                    rows=[NodeOutputWrite(node_id="__unknown__", attempt_key="k1", markers={"raw": "x"})],
                    ignore_conflicts=False,
                )
                run_id = run.id
            before = len(statements)
            async with maker() as sess, sess.begin():
                await set_rls_org(sess, _ORG_A)
                served = await read_run_markers_fenced(
                    sess,
                    run_id=run_id,
                    organisation_id=_ORG_A,
                    claim_token="tok-1",
                )
            assert served == {"k1": {"raw": "x"}}
            # Exactly ONE statement for the fenced read itself (set_rls_org on
            # SQLite is a session.info write, not SQL).
            assert len(statements) - before == 1, statements[before:]
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _count)


class TestMalformedMetadataFailOpen:
    """qa M21: a corrupt metadata row must not brick every reader for the
    run — treated as ABSENT on read paths (the write side keeps validating
    strictly)."""

    async def test_corrupt_meta_row_reads_node_rows_without_raising(
        self, session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        run = await _seed_run(session)
        # The metadata row only exists when a side is '{}' (explicit empty):
        # outputs={} + populated telemetry is the mixed case.
        await _replace(session, run, outputs={}, telemetry={"a": {"ms": 1}})
        async with session.begin():
            meta = (
                await session.execute(
                    select(RunNodeOutput).where(
                        RunNodeOutput.run_id == run.id,
                        RunNodeOutput.node_id == META_NODE_ID,
                    )
                )
            ).scalar_one()
            meta.outputs_json = {"empty_outputs": "corrupt"}
            await session.flush()
        with caplog.at_level("WARNING"):
            blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.telemetry, {"a": {"ms": 1}})
        assert any("malformed metadata" in record.getMessage() for record in caplog.records)

    async def test_corrupt_meta_row_with_no_node_rows_reads_absent(self, session: AsyncSession) -> None:
        """B2b: the legacy fallback is gone - a malformed metadata row with no
        node rows reads ABSENT (None), never legacy."""
        run = await _seed_run(session)
        async with session.begin():
            session.add(
                RunNodeOutput(
                    run_id=run.id,
                    organisation_id=run.organisation_id,
                    node_id=META_NODE_ID,
                    attempt_key=FINAL_ATTEMPT_KEY,
                    outputs_json={"bogus": True},
                )
            )
            await session.flush()
        blobs = await _read_blobs(session, run)
        assert blobs.outputs is None


class TestInheritedSentinelFiltering:
    """qa M19: inherited '__'-prefixed keys (pre-0176 legacy data, re-mapped
    into the new table by the B2b repair) are FILTERED from the REPLACE
    upserts and SURVIVE the blanking (the new table is the only store now)
    instead of raising and permanently wedging the run; NEWLY introduced
    sentinel keys still raise."""

    async def _seed_sentinel_state(self, session: AsyncSession) -> Run:
        """Directly insert the inherited-sentinel state the repair migration
        would leave behind (bypassing the sentinel-validated writers)."""
        run = await _seed_run(session)
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            await upsert_rows(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                rows=[NodeOutputWrite(node_id="__sneaky__", outputs={"v": 0})],
                ignore_conflicts=False,
            )
            await upsert_rows(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                rows=[NodeOutputWrite(node_id="a", outputs={"v": 1})],
                ignore_conflicts=False,
            )
        return run

    async def test_inherited_sentinel_key_is_filtered_and_kept(self, session: AsyncSession) -> None:
        run = await self._seed_sentinel_state(session)
        incoming = {"__sneaky__": {"v": 0}, "a": {"v": 2}}
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            stored_blobs = await read_run_node_outputs_raw(session, run_id=run.id, organisation_id=run.organisation_id)
            counts = await replace_run_node_outputs(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                outputs=incoming,
                telemetry=None,
                inherited_outputs=stored_blobs.outputs,
            )
        assert counts["outputs_dual_write_sentinel_filtered"] == 1
        # The new table carries the non-sentinel keys AND keeps the inherited
        # sentinel-key row (there is no legacy column any more).
        blobs = await _read_blobs(session, run, raw=True)
        assert set(blobs.outputs or {}) == {"a", "__sneaky__"}
        assert blobs.outputs["a"] == {"v": 2}
        assert blobs.outputs["__sneaky__"] == {"v": 0}

    async def test_newly_introduced_sentinel_key_still_raises(self, session: AsyncSession) -> None:
        run = await _seed_run(session, outputs={"a": {"v": 1}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            stored_blobs = await read_run_node_outputs_raw(session, run_id=run.id, organisation_id=run.organisation_id)
            with pytest.raises(OutputsSentinelViolation):
                await replace_run_node_outputs(
                    session,
                    run_id=run.id,
                    organisation_id=run.organisation_id,
                    outputs={"a": {"v": 1}, "__fresh__": {"v": 2}},
                    telemetry=None,
                    inherited_outputs=stored_blobs.outputs,
                )

    async def test_uncaptured_inherited_state_fails_closed(self, session: AsyncSession) -> None:
        """inherited_outputs=None (caller captured nothing) - every
        '__'-prefixed incoming key raises (fail-closed)."""
        run = await _seed_run(session)
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            with pytest.raises(OutputsSentinelViolation):
                await replace_run_node_outputs(
                    session,
                    run_id=run.id,
                    organisation_id=run.organisation_id,
                    outputs={"__sneaky__": {"v": 1}},
                    telemetry=None,
                )

    async def test_derived_sentinel_marker_node_id_raises(self, session: AsyncSession) -> None:
        """qa M19b: a parseable marker key deriving a '__'-prefixed node id
        squats the reserved namespace through the grammar - rejected."""
        run = await _seed_run(session)
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            with pytest.raises(OutputsSentinelViolation):
                await write_run_markers(
                    session,
                    run_id=run.id,
                    organisation_id=run.organisation_id,
                    markers={f"run:{_RUN_ID}:node:__sneaky__:1": {"raw": "x"}},
                )

    async def test_unparseable_marker_keys_still_accepted(self, session: AsyncSession) -> None:
        """The allowed sentinel row (__unknown__) is untouched: unparseable
        keys still write as evidence rows (FAR-188)."""
        run = await _seed_run(session)
        await _write_markers(session, run, {"weird-legacy-key": {"raw": "keep"}})
        blobs = await _read_blobs(session, run, raw=True)
        assert blobs.markers == {"weird-legacy-key": {"raw": "keep"}}


class TestBytesAccounting:
    async def test_metadata_row_is_excluded(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        await _replace(session, run, outputs={}, telemetry=None)
        totals = await _read_bytes(session, run)
        # The metadata row's flags payload must not skew the accounting.
        assert not totals

    async def test_per_node_sides_are_summed(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        outputs = {"a": {"v": 1}}
        await _replace(session, run, outputs=outputs, telemetry={"a": {"ms": 1}})
        totals = await _read_bytes(session, run)
        expected = len(json.dumps({"v": 1}, default=str)) + len(json.dumps({"ms": 1}, default=str))
        assert totals.get(run.id, 0) == expected

    async def test_empty_ids_request_is_cheap(self, session: AsyncSession) -> None:
        totals = await read_node_output_blob_bytes(session, [])
        assert not totals

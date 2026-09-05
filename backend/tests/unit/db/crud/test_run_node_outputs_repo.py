"""Round-trip unit tests for the run_node_outputs repo module (FAR-583).

Runs against a real in-memory SQLite database (no ORM tenant-filter listener,
no RLS — org context is set explicitly via ``set_rls_org`` exactly as the
generic-backend production path does), asserting the LOSSLESS mapping: the
reassembled legacy dict shapes, serialised with ``json.dumps(...,
default=str)``, are byte-identical to the legacy bytes — across the full
representation matrix: outputs-only / telemetry-only / both / per-node JSON
null / column-level NULL sides / ``{}`` (both + mixed) / NULL / colon node
ids / sentinel-named ids / ``__unknown__`` keys / multi-attempt markers /
order-hostile key ordering / shrinking REPLACE / fallback semantics /
high-water backfill selection.
"""

import itertools
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run_node_outputs import (
    FINAL_ATTEMPT_KEY,
    META_NODE_ID,
    UNKNOWN_NODE_ID,
    OutputsSentinelViolation,
    RunBlobs,
    _dialect_insert,
    _jsonb_canonical_key,
    backfill_run_node_outputs_batch,
    parse_marker_node_id,
    read_node_output_blob_bytes,
    read_run_blobs_with_fallback,
    read_run_node_outputs_raw,
    replace_run_node_outputs,
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
        outputs_json=outputs,
        node_telemetry_json=telemetry,
        raw_output_markers=markers,
    )
    async with session.begin():
        session.add(run)
        await session.flush()
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
        return await read_run_blobs_with_fallback(session, run_id=run.id, organisation_id=run.organisation_id)


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
            _dialect_insert("mysql")


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


class TestFallback:
    async def test_zero_rows_serves_legacy_columns(self, session: AsyncSession) -> None:
        """Pre-sweep stragglers: no new-table rows -> the legacy columns are
        served through the raw parameterised SELECT."""
        legacy_outputs = {"a": {"v": 1}}
        legacy_telemetry = {"a": {"ms": 2}}
        legacy_markers = {"run:x:node:a:0": {"raw": "m"}}
        run = await _seed_run(
            session,
            outputs=legacy_outputs,
            telemetry=legacy_telemetry,
            markers=legacy_markers,
        )
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, legacy_outputs)
        _assert_bytes_round_trip(blobs.telemetry, legacy_telemetry)
        _assert_bytes_round_trip(blobs.markers, legacy_markers)

    async def test_zero_rows_with_legacy_none_reads_none(self, session: AsyncSession) -> None:
        run = await _seed_run(session)
        blobs = await _read_blobs(session, run)
        assert blobs == RunBlobs(outputs=None, telemetry=None, markers=None)

    async def test_markers_key_set_mismatch_serves_legacy(self, session: AsyncSession) -> None:
        """Partial-row truncation hole: a marker row set missing a legacy key
        falls back to the (complete) legacy dict."""
        run = await _seed_run(session, markers={"k1": {"raw": "a"}, "k2": {"raw": "b"}})
        await _write_markers(session, run, {"k1": {"raw": "a"}})
        blobs = await _read_blobs(session, run)
        assert set(blobs.markers.keys()) == {"k1", "k2"}

    async def test_matching_markers_serve_new_table(self, session: AsyncSession) -> None:
        run = await _seed_run(session, markers={"k1": {"raw": "old"}})
        await _write_markers(session, run, {"k1": {"raw": "new"}})
        blobs = await _read_blobs(session, run)
        assert blobs.markers == {"k1": {"raw": "new"}}

    async def test_populated_new_table_ignores_stale_legacy(self, session: AsyncSession) -> None:
        run = await _seed_run(session, outputs={"legacy": {"v": 0}})
        await _replace(session, run, outputs={"fresh": {"v": 1}}, telemetry=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"fresh": {"v": 1}})


class TestBackfill:
    async def test_full_representation_and_idempotency(self, session: AsyncSession) -> None:
        run = await _seed_run(
            session,
            status="complete",
            completed_at=datetime(2026, 9, 1, 12, 0, 0),
            outputs={"a": {"v": 1}},
            telemetry={"a": {"ms": 1}, "b": {"ms": 2}},
            markers={"junk-key": {"raw": "x"}},
        )
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            first = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert first["runs_selected"] == 1
        assert first["runs_backfilled"] == 1
        assert first["unknown_marker_keys"] == 1
        assert first["new_high_water"] == datetime(2026, 9, 1, 12, 0, 0)

        blobs = await _read_blobs(session, run, raw=True)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}})
        _assert_bytes_round_trip(blobs.telemetry, _canonical({"a": {"ms": 1}, "b": {"ms": 2}}))
        assert blobs.markers == {"junk-key": {"raw": "x"}}

        async with session.begin():
            await set_rls_org(session, _ORG_A)
            second = await backfill_run_node_outputs_batch(
                session, organisation_id=_ORG_A, high_water_mark=first["new_high_water"], cap=100
            )
        assert second["runs_selected"] == 0

    async def test_empty_outputs_side_writes_metadata_row(self, session: AsyncSession) -> None:
        run = await _seed_run(
            session,
            status="failed",
            completed_at=datetime(2026, 9, 2, 8, 0, 0),
            outputs={},
            telemetry={"a": {"ms": 4}},
        )
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        blobs = await _read_blobs(session, run, raw=True)
        assert blobs.outputs is not None
        assert not blobs.outputs
        assert blobs.telemetry == {"a": {"ms": 4}}

    async def test_sentinel_named_ids_quarantine_the_run(self, session: AsyncSession) -> None:
        run = await _seed_run(
            session,
            status="complete",
            completed_at=datetime(2026, 9, 3, 6, 0, 0),
            outputs={"__sneaky__": {"v": 1}},
        )
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            result = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert result["runs_quarantined"] == 1
        assert result["runs_backfilled"] == 0
        blobs = await _read_blobs(session, run, raw=True)
        assert blobs == RunBlobs(outputs=None, telemetry=None, markers=None)

    async def test_high_water_advances_and_bounds_selection(self, session: AsyncSession) -> None:
        await _seed_run(session, status="complete", completed_at=datetime(2026, 8, 1, 0, 0, 0), outputs={"a": {}})
        await _seed_run(session, status="complete", completed_at=datetime(2026, 8, 2, 0, 0, 0), outputs={"b": {}})
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            first = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=1)
        assert first["runs_selected"] == 1
        assert first["new_high_water"] == datetime(2026, 8, 1, 0, 0, 0)

        async with session.begin():
            await set_rls_org(session, _ORG_A)
            second = await backfill_run_node_outputs_batch(
                session, organisation_id=_ORG_A, high_water_mark=first["new_high_water"], cap=1
            )
        assert second["runs_selected"] == 1
        assert second["new_high_water"] == datetime(2026, 8, 2, 0, 0, 0)

    async def test_non_terminal_runs_are_untouched(self, session: AsyncSession) -> None:
        await _seed_run(session, status="running", outputs={"a": {"v": 1}})
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            result = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert result["runs_selected"] == 0
        assert result["rows_written"] == 0

    async def test_other_org_is_out_of_scope(self, session: AsyncSession) -> None:
        await _seed_run(session, status="complete", completed_at=datetime(2026, 9, 1, 0, 0, 0), outputs={"a": {}})
        async with session.begin():
            await set_rls_org(session, _ORG_B)
            result = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_B, cap=100)
        assert result["runs_selected"] == 0

    async def test_already_healed_runs_are_excluded_from_selection(self, session: AsyncSession) -> None:
        """The NOT-EXISTS trigger legs keep healed runs out of the batch — a
        drained org selects nothing on the steady-state (mark-less) tick."""
        await _seed_run(session, completed_at=datetime(2026, 9, 1, 12, 0, 0), outputs={"a": {"v": 1}})
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            first = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert first["runs_selected"] == 1
        assert first["runs_backfilled"] == 1

        async with session.begin():
            await set_rls_org(session, _ORG_A)
            second = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert second["runs_selected"] == 0
        assert second["runs_backfilled"] == 0

    async def test_markers_only_run_selected_by_trigger_leg(self, session: AsyncSession) -> None:
        """A markers-only run (no outputs/telemetry) matches the markers leg."""
        await _seed_run(
            session,
            completed_at=datetime(2026, 9, 1, 12, 0, 0),
            markers={"junk-key": {"raw": "x"}},
        )
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            result = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert result["runs_selected"] == 1
        assert result["unknown_marker_keys"] == 1

    async def test_ghost_moved_rows_skip_the_insert(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-terminalization ghost protection: rows that moved between the
        census and the pre-insert re-check are skipped, never overwritten."""
        import modulo.db.crud.run_node_outputs as repo_module

        run = await _seed_run(session, completed_at=datetime(2026, 9, 1, 12, 0, 0), outputs={"a": {"v": 1}})

        async def _moved_recheck(s: AsyncSession, run_ids: Any) -> dict[uuid.UUID, datetime | None]:
            # The pre-insert re-check pretends a concurrent dual-write just
            # committed new rows for the run after the census read.
            return {uuid.UUID(str(rid)): datetime(2026, 9, 2, 0, 0, 0) for rid in run_ids}

        monkeypatch.setattr(repo_module, "_existing_row_updated_at", _moved_recheck)
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            result = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert result["runs_selected"] == 1
        assert result["runs_skipped_ghost"] == 1
        assert result["runs_backfilled"] == 0
        assert result["rows_written"] == 0
        blobs = await _read_blobs(session, run, raw=True)
        # Nothing was written by the ghosted pass.
        assert blobs == RunBlobs(outputs=None, telemetry=None, markers=None)


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

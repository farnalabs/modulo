"""Round-trip unit tests for the run_node_outputs repo module (FAR-583).

Runs against a real in-memory SQLite database (no ORM tenant-filter listener,
no RLS — org context is set explicitly via ``set_rls_org`` exactly as the
generic-backend production path does), asserting the LOSSLESS mapping: the
reassembled legacy dict shapes, serialised with ``json.dumps(...,
default=str)``, are byte-identical to the legacy bytes — across the full
representation matrix: outputs-only / telemetry-only / both / per-node JSON
null / column-level NULL sides / ``{}`` (both + mixed) / NULL / colon node
ids / sentinel-named ids / ``__unknown__`` keys / multi-attempt markers /
order-hostile key ordering / shrinking REPLACE / DIRECTION-AWARE fallback
semantics / quarantined backfill selection / the fenced markers reader /
malformed-metadata fail-open / inherited-sentinel filtering.
"""

import itertools
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run_node_outputs import (
    FINAL_ATTEMPT_KEY,
    META_NODE_ID,
    QUARANTINE_TABLE,
    RUNS_LEGACY_TABLE,
    UNKNOWN_NODE_ID,
    OutputsSentinelViolation,
    RunBlobs,
    _dialect_insert,
    _jsonb_canonical_key,
    parse_marker_node_id,
    read_node_output_blob_bytes,
    read_run_blobs_with_fallback,
    read_run_markers_fenced,
    read_run_node_outputs_raw,
    replace_run_node_outputs,
    write_run_markers,
)
from modulo.db.crud.run_node_outputs_backfill import backfill_run_node_outputs_batch
from modulo.db.models.base import Base
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import RunNodeOutput
from modulo.db.rls import OutputsRlsMismatch, set_rls_org

_TABLE_NAMES = {"organisations", "runs", "run_node_outputs"}

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")

_RUN_NUMBER = itertools.count(1)

_RUN_ID = "01234567-89ab-cdef-0123-456789abcdef"

# "leave this column untouched" sentinel for the legacy-blob rewrite helper.
_UNSET: Any = object()


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        # The quarantine side table has NO ORM model (migration-0192-owned,
        # Core-only in the repo module) — created here so the sweep's
        # quarantine exclusion + INSERT run against the real schema.
        await conn.run_sync(lambda sync_conn: QUARANTINE_TABLE.create(sync_conn, checkfirst=True))
        # FAR-583 B1: the legacy blob columns left the ORM mapping but are
        # still IN THE DATABASE until B2b — the patching ALTERs reproduce the
        # migrated schema (SQLite ADD COLUMN per legacy column) so the raw
        # Core legacy-table readers/sweep/fenced legs run against real DDL.
        for legacy_col in ("outputs_json", "node_telemetry_json", "raw_output_markers"):
            await conn.exec_driver_sql(f"ALTER TABLE runs ADD COLUMN {legacy_col} JSON")
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
        # FAR-583 B1: the legacy blob columns no longer map on the ORM — the
        # seeding writes them through the repo module's raw Core legacy table
        # (the same parameterised-SQL surface the production fallback readers
        # and sweep selection use; the SQLite round-trip is exercised here).
        if outputs is not None or telemetry is not None or markers is not None:
            await session.execute(
                update(RUNS_LEGACY_TABLE)
                .where(RUNS_LEGACY_TABLE.c.id == run.id)
                .values(
                    outputs_json=outputs,
                    node_telemetry_json=telemetry,
                    raw_output_markers=markers,
                )
            )
    return run


async def _set_legacy_blobs(
    session: AsyncSession,
    run: Run,
    *,
    outputs: Any = _UNSET,
    telemetry: Any = _UNSET,
    markers: Any = _UNSET,
) -> None:
    """Directly rewrite the run's legacy blob columns (simulating a
    kill-switch-OFF legacy-only write or the frozen-at-B1 legacy state).
    FAR-583 B1: the columns are rewritten via the repo module's raw Core
    legacy table (the ORM mapping is cut)."""
    values: dict[str, Any] = {}
    if outputs is not _UNSET:
        values["outputs_json"] = outputs
    if telemetry is not _UNSET:
        values["node_telemetry_json"] = telemetry
    if markers is not _UNSET:
        values["raw_output_markers"] = markers
    if not values:
        return
    async with session.begin():
        await session.execute(update(RUNS_LEGACY_TABLE).where(RUNS_LEGACY_TABLE.c.id == run.id).values(**values))


async def _quarantine_row_count(session: AsyncSession, run_id: uuid.UUID) -> int:
    async with session.begin():
        rows = (
            await session.execute(select(QUARANTINE_TABLE.c.run_id).where(QUARANTINE_TABLE.c.run_id == run_id))
        ).all()
    return len(rows)


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


class TestDirectionAwareFallback:
    """qa M2/M3: the fallback compares LEGACY vs REASSEMBLED key sets by
    subset direction — legacy ⊆ new serves NEW, new ⊂ legacy serves LEGACY
    (truncation guard), divergent serves NEW with a warning."""

    async def test_kill_switch_off_legacy_write_is_not_shadowed_outputs(self, session: AsyncSession) -> None:
        """A kill-switch-OFF legacy-only write to an already-represented run
        makes legacy the SUPERSET — the old absence-only fallback served the
        stale NEW dict forever; the direction rule serves LEGACY (fresher)."""
        run = await _seed_run(session)
        await _replace(session, run, outputs={"a": {"v": 1}, "b": {"v": 2}}, telemetry=None)
        await _set_legacy_blobs(session, run, outputs={"a": {"v": 1}, "b": {"v": 2}, "c": {"v": 3}})
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}, "b": {"v": 2}, "c": {"v": 3}})

    async def test_b1_legacy_frozen_new_grows_serves_new_outputs(self, session: AsyncSession) -> None:
        """B1 simulation: legacy frozen at {a, b}; the new table grows to
        {a, b, c} — legacy ⊆ new → serve NEW (not the stale subset)."""
        run = await _seed_run(session, outputs={"a": {"v": 0}, "b": {"v": 0}})
        await _replace(session, run, outputs={"a": {"v": 1}, "b": {"v": 1}, "c": {"v": 2}}, telemetry=None)
        await _set_legacy_blobs(session, run, outputs={"a": {"v": 0}, "b": {"v": 0}})
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}, "b": {"v": 1}, "c": {"v": 2}})

    async def test_divergent_key_sets_serve_new_with_warning(
        self, session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        run = await _seed_run(session, outputs={"a": {"v": 0}, "c": {"v": 0}})
        await _replace(session, run, outputs={"a": {"v": 1}, "b": {"v": 1}}, telemetry=None)
        await _set_legacy_blobs(session, run, outputs={"a": {"v": 0}, "c": {"v": 0}})
        with caplog.at_level("WARNING"):
            blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}, "b": {"v": 1}})
        assert any("diverge" in record.getMessage() for record in caplog.records)

    async def test_kill_switch_off_legacy_write_is_not_shadowed_markers(self, session: AsyncSession) -> None:
        """The markers leg: legacy gains a key the new table lacks (legacy
        superset) → LEGACY served — the old any-mismatch rule happened to do
        this; the subset rule pins it direction-correctly."""
        run = await _seed_run(session, markers={"k1": {"raw": "a"}})
        await _write_markers(session, run, {"k1": {"raw": "a"}})
        await _set_legacy_blobs(session, run, markers={"k1": {"raw": "a"}, "k2": {"raw": "b"}})
        blobs = await _read_blobs(session, run)
        assert set(blobs.markers.keys()) == {"k1", "k2"}

    async def test_b1_legacy_frozen_new_grows_serves_new_markers(self, session: AsyncSession) -> None:
        run = await _seed_run(session, markers={"k1": {"raw": "old"}})
        await _write_markers(session, run, {"k1": {"raw": "old"}, "k2": {"raw": "new"}})
        # Legacy frozen at {k1}; the new table carries {k1, k2} — legacy ⊆ new.
        await _set_legacy_blobs(session, run, markers={"k1": {"raw": "old"}})
        blobs = await _read_blobs(session, run)
        assert blobs.markers == {"k1": {"raw": "old"}, "k2": {"raw": "new"}}

    async def test_telemetry_side_is_direction_aware_too(self, session: AsyncSession) -> None:
        run = await _seed_run(session, telemetry={"a": {"ms": 1}})
        await _replace(session, run, outputs=None, telemetry={"a": {"ms": 1}, "b": {"ms": 2}})
        await _set_legacy_blobs(session, run, telemetry={"a": {"ms": 1}, "b": {"ms": 2}, "c": {"ms": 3}})
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.telemetry, {"a": {"ms": 1}, "b": {"ms": 2}, "c": {"ms": 3}})


class TestEqualKeysValueTiebreak:
    """qa iteration 2 (Major 1): EQUAL key sets with DIVERGENT values — the
    old ``legacy ⊆ new`` rule served NEW blind to the rewrite. With the
    kill-switch OFF a value rewrite on an existing key set can only be a
    legacy-only write (legacy is fresher) → tiebreak to LEGACY; with the
    switch ON the new table stays authoritative (documented posture)."""

    async def test_tiebreak_on_serves_legacy_on_equal_key_sets(self, session: AsyncSession) -> None:
        run = await _seed_run(session, outputs={"a": {"v": 0}, "b": {"v": 0}})
        await _replace(session, run, outputs={"a": {"v": 1}, "b": {"v": 1}}, telemetry=None)
        # Kill-switch-OFF legacy-only value REWRITE: same keys, new values.
        await _set_legacy_blobs(session, run, outputs={"a": {"v": 9}, "b": {"v": 9}})
        async with session.begin():
            blobs = await read_run_blobs_with_fallback(
                session, run_id=run.id, organisation_id=run.organisation_id, legacy_tiebreak_on_equal_mismatch=True
            )
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 9}, "b": {"v": 9}})

    async def test_tiebreak_off_keeps_new_authoritative(self, session: AsyncSession) -> None:
        """The documented general-reader posture: equal key sets with
        divergent values serve NEW (UI/analytics tolerate ≤1 sweep interval;
        B2b repair is the backstop)."""
        run = await _seed_run(session, outputs={"a": {"v": 0}})
        await _replace(session, run, outputs={"a": {"v": 1}}, telemetry=None)
        await _set_legacy_blobs(session, run, outputs={"a": {"v": 9}})
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}})

    async def test_fenced_markers_reader_tiebreak_serves_legacy(self, session: AsyncSession) -> None:
        run = await _seed_run(session, status="running", claim_token="tok-1", markers={"k1": {"raw": "old"}})
        await _write_markers(session, run, {"k1": {"raw": "old"}})
        # Kill-switch-OFF legacy-only rewrite: same attempt key, delivery_done stamped.
        await _set_legacy_blobs(session, run, markers={"k1": {"delivery_done": True}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token="tok-1",
                legacy_tiebreak_on_equal_mismatch=True,
            )
        assert served == {"k1": {"delivery_done": True}}

    async def test_identical_values_still_serve_new_under_tiebreak(self, session: AsyncSession) -> None:
        """Equal key sets with EQUAL values (the steady dual-written state) do
        NOT flip to legacy — the tiebreak fires on value DIVERGENCE only."""
        run = await _seed_run(session, outputs={"a": {"v": 1}})
        await _replace(session, run, outputs={"a": {"v": 1}}, telemetry=None)
        await _set_legacy_blobs(session, run, outputs={"a": {"v": 1}})
        async with session.begin():
            blobs = await read_run_blobs_with_fallback(
                session, run_id=run.id, organisation_id=run.organisation_id, legacy_tiebreak_on_equal_mismatch=True
            )
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}})


class TestShrinkBlindSpot:
    """qa iteration 2 (Major 1): metadata row ABSENT + legacy exactly ``{}`` +
    new non-empty → serve LEGACY. ``meta-absent + legacy-'{}'`` can only be a
    POST-representation legacy-only rewrite (a backfill over legacy ``{}``
    writes the metadata row), so legacy is fresher. Deterministic — NOT
    switch-gated. Markers are exempt (no metadata row encodes empty markers)."""

    async def test_meta_absent_legacy_empty_shrink_serves_legacy(self, session: AsyncSession) -> None:
        run = await _seed_run(session, outputs={"a": {"v": 0}})
        await _replace(session, run, outputs={"a": {"v": 1}, "b": {"v": 2}}, telemetry=None)
        # Post-representation legacy-only rewrite to {} — no metadata row was
        # written (the REPLACE that represented the run saw a populated dict).
        await _set_legacy_blobs(session, run, outputs={})
        blobs = await _read_blobs(session, run)
        assert blobs.outputs is not None
        assert not blobs.outputs

    async def test_legacy_null_still_serves_new(self, session: AsyncSession) -> None:
        """The blind spot is about the EXPLICIT ``{}``: a legacy NULL (side
        absent) keeps serving NEW — the truncation-hole guard."""
        run = await _seed_run(session, outputs={"a": {"v": 0}})
        await _replace(session, run, outputs={"a": {"v": 1}, "b": {"v": 2}}, telemetry=None)
        await _set_legacy_blobs(session, run, outputs=None)
        blobs = await _read_blobs(session, run)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 1}, "b": {"v": 2}})

    async def test_meta_present_legacy_empty_serves_new(self, session: AsyncSession) -> None:
        """With the metadata row PRESENT the legacy ``{}`` is already
        represented (the flags preserve it) — the shrink case must NOT fire;
        a represented ``{}`` side serves NEW."""
        run = await _seed_run(session, outputs={}, telemetry={"a": {"ms": 1}})
        await _replace(session, run, outputs={}, telemetry={"a": {"ms": 1}, "b": {"ms": 2}})
        blobs = await _read_blobs(session, run)
        assert blobs.outputs is not None
        assert not blobs.outputs
        _assert_bytes_round_trip(blobs.telemetry, {"a": {"ms": 1}, "b": {"ms": 2}})


class TestFencedMarkersReader:
    """qa M4/M5: ONE fenced statement (runs row under the fence predicates,
    LEFT-JOINed to the marker rows) reassembling flat, with the same
    direction-aware legacy fallback — the legacy column is selected from the
    SAME fenced joined row, so the fallback re-checks the fence by
    construction. ``for_update`` renders FOR UPDATE OF runs on Postgres and
    is a no-op on SQLite."""

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
        """Fence miss (superseded executor's token) — the same visibility the
        predicate-fenced gate read had: NOTHING is served, legacy included."""
        run = await self._seed_running_run(session, claim_token="tok-real")
        await _write_markers(session, run, {"k1": {"raw": "a"}})
        await _set_legacy_blobs(session, run, markers={"k1": {"raw": "a"}})
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
        await _set_legacy_blobs(session, run, markers={"k1": {"raw": "a"}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token=None,
            )
        assert served is None

    async def test_no_marker_rows_serves_fenced_legacy_column(self, session: AsyncSession) -> None:
        """Absence fallback within the fence: zero marker rows -> the legacy
        column (read from the SAME fenced joined row) is served."""
        run = await self._seed_running_run(session, markers={"legacy-k": {"raw": "x"}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token="tok-1",
            )
        assert served == {"legacy-k": {"raw": "x"}}

    async def test_truncated_rows_fall_back_to_legacy_within_the_fence(self, session: AsyncSession) -> None:
        run = await self._seed_running_run(session, markers={"k1": {"raw": "a"}, "k2": {"raw": "b"}})
        await _write_markers(session, run, {"k1": {"raw": "a"}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            served = await read_run_markers_fenced(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                claim_token="tok-1",
            )
        assert served == {"k1": {"raw": "a"}, "k2": {"raw": "b"}}

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
        """The fence + reassembly + legacy fallback compose into ONE SQL
        statement (statement counter on the engine)."""
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
                await sess.execute(
                    update(RUNS_LEGACY_TABLE)
                    .where(RUNS_LEGACY_TABLE.c.id == run.id)
                    .values(raw_output_markers={"legacy-k": {"raw": "x"}})
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
            assert served == {"legacy-k": {"raw": "x"}}
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

    async def test_corrupt_meta_row_with_no_node_rows_falls_back_to_legacy(self, session: AsyncSession) -> None:
        run = await _seed_run(session, outputs={"legacy": {"v": 9}})
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
        _assert_bytes_round_trip(blobs.outputs, {"legacy": {"v": 9}})


class TestInheritedSentinelFiltering:
    """qa M19: inherited '__'-prefixed keys (pre-0192 legacy data) are
    FILTERED from the new-table REPLACE write (kept on the legacy column)
    instead of raising and permanently wedging the run; NEWLY introduced
    sentinel keys still raise."""

    async def test_inherited_sentinel_key_is_filtered_and_counted(self, session: AsyncSession) -> None:
        legacy_outputs: dict[str, Any] = {"__sneaky__": {"v": 0}, "a": {"v": 1}}
        run = await _seed_run(session, outputs=legacy_outputs)
        incoming = {"__sneaky__": {"v": 0}, "a": {"v": 2}, "b": {"v": 3}}
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            counts = await replace_run_node_outputs(
                session,
                run_id=run.id,
                organisation_id=run.organisation_id,
                outputs=incoming,
                telemetry=None,
                inherited_outputs=legacy_outputs,
            )
        assert counts["outputs_dual_write_sentinel_filtered"] == 1
        # The new table carries only the non-sentinel keys.
        blobs = await _read_blobs(session, run, raw=True)
        _assert_bytes_round_trip(blobs.outputs, {"a": {"v": 2}, "b": {"v": 3}})
        # The caller's legacy write retains the inherited key (simulated):
        await _set_legacy_blobs(session, run, outputs=incoming)
        fallback_blobs = await _read_blobs(session, run)
        # The legacy superset (sentinel key included) is served via the
        # truncation guard — evidence preserved until B2b.
        assert "__sneaky__" in (fallback_blobs.outputs or {})

    async def test_newly_introduced_sentinel_key_still_raises(self, session: AsyncSession) -> None:
        run = await _seed_run(session, outputs={"a": {"v": 1}})
        async with session.begin():
            await set_rls_org(session, run.organisation_id)
            with pytest.raises(OutputsSentinelViolation):
                await replace_run_node_outputs(
                    session,
                    run_id=run.id,
                    organisation_id=run.organisation_id,
                    outputs={"a": {"v": 1}, "__fresh__": {"v": 2}},
                    telemetry=None,
                    inherited_outputs={"a": {"v": 1}},
                )

    async def test_uncaptured_inherited_state_fails_closed(self, session: AsyncSession) -> None:
        """inherited_outputs=None (caller captured nothing) — every
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
        squats the reserved namespace through the grammar — rejected."""
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
        # qa M1b: the quarantine row lands and the run is NEVER re-selected
        # (the old code skipped it in-body only — it stayed re-selectable
        # on every tick, consuming the cap).
        assert await _quarantine_row_count(session, run.id) == 1
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            second = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert second["runs_selected"] == 0
        assert second["runs_backfilled"] == 0

    async def test_non_dict_blob_anomaly_is_quarantined_never_reselected(self, session: AsyncSession) -> None:
        """qa M1c: a jsonb ARRAY in a blob side is junk — on SQLite the
        text-cast trigger still selects it (the jsonb-native OBJECT predicate
        that excludes it on Postgres is dialect-branched); the body must
        QUARANTINE the run, not silently continue (the old silent continue
        left the run re-selected on EVERY tick, starving the cap)."""
        run = await _seed_run(
            session,
            status="complete",
            completed_at=datetime(2026, 9, 3, 6, 0, 0),
        )
        await _set_legacy_blobs(session, run, markers=["not", "a", "dict"])
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            first = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert first["runs_quarantined"] == 1
        assert first["runs_backfilled"] == 0
        assert await _quarantine_row_count(session, run.id) == 1
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            second = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert second["runs_selected"] == 0

    async def test_sentinel_marker_attempt_key_quarantines_the_run(self, session: AsyncSession) -> None:
        """qa M1c: a '__'-prefixed marker attempt key (e.g. '__final__') used
        to be silently dropped key-by-key — the run stayed re-selectable
        forever with its marker evidence unrepresented. Now the run is
        quarantined (evidence preserved on the side table)."""
        run = await _seed_run(
            session,
            status="complete",
            completed_at=datetime(2026, 9, 3, 6, 0, 0),
            markers={FINAL_ATTEMPT_KEY: {"raw": "squat"}},
        )
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            result = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert result["runs_quarantined"] == 1
        assert result["runs_backfilled"] == 0
        assert await _quarantine_row_count(session, run.id) == 1

    async def test_backdated_unhealed_run_is_still_selected(self, session: AsyncSession) -> None:
        """qa M20 prove-the-fix: the OLD selection filtered
        ``completed_at > high_water_mark`` — a run terminalizing with an
        EARLIER completed_at than the current mark (its terminalizing
        transaction started before the mark advanced) was permanently
        missed. Without the filter, the trigger legs pick it up."""
        run_old = await _seed_run(
            session, status="complete", completed_at=datetime(2026, 8, 1, 0, 0, 0), outputs={"old": {}}
        )
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            first = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=10)
        assert first["runs_backfilled"] == 1
        # A BACKDATED un-healed run appears AFTER the mark advanced past
        # 2026-08-01 (e.g. an old machine's terminalizing transaction).
        run_backdated = await _seed_run(
            session, status="failed", completed_at=datetime(2026, 7, 15, 0, 0, 0), outputs={"late": {}}
        )
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            second = await backfill_run_node_outputs_batch(
                session,
                organisation_id=_ORG_A,
                high_water_mark=first["new_high_water"],  # accepted, IGNORED for selection
                cap=10,
            )
        assert second["runs_selected"] == 1
        assert second["runs_backfilled"] == 1
        blobs = await _read_blobs(session, run_backdated, raw=True)
        _assert_bytes_round_trip(blobs.outputs, {"late": {}})
        # The healed run stays healed (the trigger excludes it).
        blobs_old = await _read_blobs(session, run_old, raw=True)
        _assert_bytes_round_trip(blobs_old.outputs, {"old": {}})

    async def test_cap_bounds_selection_across_ticks_without_high_water(self, session: AsyncSession) -> None:
        """The per-tick cap + ORDER BY completed_at ASC + trigger legs bound
        the drain with NO high-water filter: each tick heals the oldest
        un-healed run; the mark-less steady state selects nothing."""
        await _seed_run(session, status="complete", completed_at=datetime(2026, 8, 1, 0, 0, 0), outputs={"a": {}})
        await _seed_run(session, status="complete", completed_at=datetime(2026, 8, 2, 0, 0, 0), outputs={"b": {}})
        await _seed_run(session, status="complete", completed_at=datetime(2026, 8, 3, 0, 0, 0), outputs={"c": {}})
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            first = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=1)
        assert first["runs_selected"] == 1
        assert first["runs_backfilled"] == 1
        assert first["new_high_water"] == datetime(2026, 8, 1, 0, 0, 0)

        async with session.begin():
            await set_rls_org(session, _ORG_A)
            second = await backfill_run_node_outputs_batch(
                session, organisation_id=_ORG_A, high_water_mark=first["new_high_water"], cap=1
            )
        assert second["runs_selected"] == 1
        assert second["new_high_water"] == datetime(2026, 8, 2, 0, 0, 0)

        async with session.begin():
            await set_rls_org(session, _ORG_A)
            third = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=1)
        assert third["runs_selected"] == 1

        async with session.begin():
            await set_rls_org(session, _ORG_A)
            drained = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=1)
        assert drained["runs_selected"] == 0

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

    async def test_empty_markers_dict_run_is_never_selected(self, session: AsyncSession) -> None:
        """A run whose ONLY blob is markers = '{}' (explicit empty dict) is
        never selected: '{}' markers mean "no markers" and are not
        representable (migration 0192's _ANY_BLOB_OBJECT_SQL excludes them).
        Selecting such a run would write zero rows every pass — an
        un-healable zombie re-selected on every sweep tick."""
        await _seed_run(
            session,
            completed_at=datetime(2026, 9, 1, 12, 0, 0),
            markers={},
        )
        async with session.begin():
            await set_rls_org(session, _ORG_A)
            result = await backfill_run_node_outputs_batch(session, organisation_id=_ORG_A, cap=100)
        assert result["runs_selected"] == 0
        assert result["runs_backfilled"] == 0
        assert result["rows_written"] == 0

    async def test_ghost_moved_rows_skip_the_insert(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-terminalization ghost protection: rows that moved between the
        census and the pre-insert re-check are skipped, never overwritten."""
        import modulo.db.crud.run_node_outputs_backfill as backfill_module

        run = await _seed_run(session, completed_at=datetime(2026, 9, 1, 12, 0, 0), outputs={"a": {"v": 1}})

        async def _moved_recheck(s: AsyncSession, run_ids: Any) -> dict[uuid.UUID, datetime | None]:
            # The pre-insert re-check pretends a concurrent dual-write just
            # committed new rows for the run after the census read.
            return {uuid.UUID(str(rid)): datetime(2026, 9, 2, 0, 0, 0) for rid in run_ids}

        monkeypatch.setattr(backfill_module, "_existing_row_updated_at", _moved_recheck)
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

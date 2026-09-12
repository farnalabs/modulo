"""Integration test for migration 0215 (FAR-583 — the DROP migration).

Runs the REAL alembic ``upgrade`` of 0215 against a live Postgres
(testcontainers) on an ISOLATED database (the shared session database is
migrated to head — where the columns are already gone), stepped up to the
revision just before 0215 so the legacy ``runs`` blob columns still exist.
Covers every stage of the drop spec:

* the pre-flight drain assertion RAISES on a pre-B1 in-flight run (and the
  transaction rolls back — alembic_version stays at the pre-revision);
* the structural-anomaly gate RAISES on a legacy blob that is not a jsonb
  object (instruction of the 0192 dict semantics — unparseable);
* the per-run REPAIR bound RAISES on a NON-quarantined run whose legacy
  blob's keys are all `__`-prefixed (the pre-bound `while candidates` loop
  re-selected that run forever — a deploy HANG; the bound aborts loudly
  with the sample run id instead);
* the INSERT-only repair inserts the absent new-table rows for a TERMINAL
  pre-cutoff run whose legacy blobs have no new-table representation (and
  the MARKERS leg ONLY for the unknown-status run — 0192's twin geometry);
* content parity for outputs/telemetry is LEGACY-AUTHORITATIVE: a run whose
  pre-existing new-table rows carry divergent content is overwritten with
  the legacy values (terminal + pre-cutoff runs are immutable);
* the markers-subset parity RAISES on a value divergence on a PRESENT key
  (never repairable by an INSERT-only leg);
* the ``'__unknown__'`` marker row is created for an unparseable key with
  the FULL original attempt key preserved;
* the DROP: the three runs blob columns and the 0193 sweep index are gone,
  while the 0192 quarantine table SURVIVES;
* the downgrade RAISES ("never rewind past this migration").

Aborted attempts roll their full transaction back, so the same isolated
database drives all five attempts in sequence (drain -> junk -> repair
bound -> markers divergence -> success), each blocker removed/repaired in
between.
"""

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

MIGRATION_REV = "heads"
PREV_REV = "0214_connector_instance_indexes_unique"

# The B1 deploy cutoff embedded in the migration (B1 merged 2026-09-10T11:25:42Z, PR #298).
_B1_CUTOFF = "2026-09-10T11:25:42+00:00"

# The three runs blob columns (fixed identifiers of the migrated schema —
# never caller input — so DDL/index identifiers are inlined literals).
RUN_LEGACY_COLUMNS = ("outputs_json", "node_telemetry_json", "raw_output_markers")
SWEEP_INDEX = "ix_runs_org_completed_at_terminal_sweep"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    """Return ``db_url`` with its database name replaced by ``new_db``."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


@pytest_asyncio.fixture
async def drop_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A private Postgres database stepped up to ``PREV_REV`` (pre-0215).

    Identical in shape to test_migration_0126's isolated-db fixture: clone
    template0, pin DATABASE_URL / DATABASE_ADMIN_URL, upgrade to the
    revision BEFORE the drop so the legacy runs blob columns still exist,
    and drop the database again afterwards.
    """
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"drop_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)
    monkeypatch.setenv("DATABASE_URL", iso_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", iso_url)
    eng = create_async_engine(iso_url, poolclass=NullPool)
    async with eng.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
        await conn.commit()
    await eng.dispose()

    with patch.dict(os.environ, {"DATABASE_URL": iso_url, "DATABASE_ADMIN_URL": iso_url}):
        command.upgrade(_alembic_config(iso_url), PREV_REV)

    try:
        yield iso_url
    finally:
        admin_engine = create_async_engine(
            db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"}
        )
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": db_name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await admin_engine.dispose()


async def _seed_drop_world(db_url: str) -> dict[str, Any]:
    """Seed the org/account/pipeline/snapshot graph + the runs under test.

    Returns ``{"org": uuid, markers: {...}, runs: {...}}``. All runs are
    created BEFORE the B1 cutoff.
    """
    oid = uuid.uuid4()
    pid = uuid.uuid4()
    sid = uuid.uuid4()
    aid = uuid.uuid4()
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                    "VALUES (:id, 'drop@example.com', 'drop', 'hash', 'local', true)"
                ),
                {"id": str(aid)},
            )
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'drop', 'drop', '{}'::json)"
                ),
                {"id": str(oid)},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, account_id, name, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :aid, 'drop pipeline', 10, 30, 300, '{}'::json, '[]'::json)"
                ),
                {"id": str(pid), "oid": str(oid), "aid": str(aid)},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                    "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                    "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                    "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                    "'[]'::json, '{}'::json, '{}'::json)"
                ),
                {"id": str(sid), "pid": str(pid), "oid": str(oid)},
            )
    finally:
        await engine.dispose()

    run_a = uuid.uuid4()  # repair: legacy-only blobs, no new-table rows at all
    run_b = uuid.uuid4()  # parity overwrite: divergent pre-existing final rows
    run_c = uuid.uuid4()  # markers PRESENT-key value divergence (TERMINAL variant)
    run_e = uuid.uuid4()  # unknown-status run with an unparseable marker key (markers leg only)
    run_f = uuid.uuid4()  # junk scalar legacy blob (out/tel structural anomaly gate)
    run_g = uuid.uuid4()  # NOT quarantined; legacy blob whose every key is __-prefixed (repair bound)
    run_d = uuid.uuid4()  # pre-B1 in-flight run (drain gate)

    marker_a = f"run:{run_a}:node:n1:1"
    marker_c = f"run:{run_c}:node:n1:1"
    marker_e = "legacy_chunk:weird"
    # Blob values are CONSTANT test shapes (never caller input) and are
    # inlined as ::jsonb literals so asyncpg does not need a codec for the
    # jsonb-typed params; run ids/keys ride in as bound parameters.
    created_at = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [
        # (run_id, status, outputs_json, node_telemetry_json, raw_output_markers)
        (run_a, "complete", '{"n1": {"v": 1}}', '{"n1": {"t": 9}}', f'{{"{marker_a}": {{"raw": "m"}}}}'),
        (run_b, "complete", '{"n1": {"v": "LEGACY"}}', None, None),
        (run_c, "complete", None, None, f'{{"{marker_c}": {{"raw": "LEGACY-MARK"}}}}'),
        (run_e, "unknown", None, None, f'{{"{marker_e}": "payload"}}'),
        (run_f, "complete", "3", None, None),
        # run_g: a jsonb OBJECT blob — passes the junk gate — but EVERY key
        # is __-prefixed, so the out/tel leg's sentinel filter drops them
        # all; the meta leg needs a '{}' side run_g does not have. 0192's
        # quarantine window is long closed so run_g is NOT quarantined:
        # before the per-run repair bound, the candidate walk re-selected
        # run_g forever (a deploy HANG under release.sh's 3x retry).
        (run_g, "complete", '{"__squat_a": {"v": 1}, "__squat_b": {"v": 2}}', None, None),
        (run_d, "running", None, None, None),
    ]

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for idx, (run_id, status, outputs, telemetry, markers) in enumerate(rows):
                # The legacy runs blob columns still exist on THIS database
                # (it is parked at PREV_REV, before 0215 drops them).
                blobs_sql = ""
                if outputs is not None or telemetry is not None or markers is not None:
                    legacy_outputs = f"CAST('{outputs}' AS jsonb)" if outputs else "NULL"
                    legacy_telemetry = f"CAST('{telemetry}' AS jsonb)" if telemetry else "NULL"
                    legacy_markers = f"CAST('{markers}' AS jsonb)" if markers else "NULL"
                    blobs_sql = f", {legacy_outputs}, {legacy_telemetry}, {legacy_markers}"
                await conn.execute(
                    text(
                        # Constant test literals only (blob values are fixed
                        # shapes inlined as ::jsonb casts; run ids/keys are
                        # bound parameters).
                        "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "  # noqa: S608 - constant test literals, bound params for values
                        "status, run_number, input_hash, langgraph_thread_id, created_at"
                        + (", outputs_json, node_telemetry_json, raw_output_markers" if blobs_sql else "")
                        + ") VALUES (:id, :oid, :pid, :sid, 'manual', :status, :run_number, 'ih', :thread, :created"
                        + blobs_sql
                        + ")"
                    ),
                    {
                        "id": str(run_id),
                        "oid": str(oid),
                        "pid": str(pid),
                        "sid": str(sid),
                        "status": status,
                        # Distinct per-row within the shared org (uq
                        # (organisation_id, run_number)) and a REAL int
                        # (asyncpg needs the typed value).
                        "run_number": idx + 1,
                        "thread": f"drop-{run_id.hex[:12]}",
                        "created": created_at,
                    },
                )
            # runC's pre-existing new-table markers row carries the SAME
            # attempt key as the legacy dict with DIFFERENT content -> the
            # markers-subset parity step must abort on attempt 3.
            await conn.execute(
                text(
                    "INSERT INTO run_node_outputs (run_id, node_id, attempt_key, organisation_id, "
                    "raw_output_markers, created_at, updated_at) "
                    "VALUES (:rid, 'n1', :key, :oid, CAST(:val AS jsonb), now(), now())"
                ),
                {"rid": str(run_c), "key": marker_c, "oid": str(oid), "val": '{"raw": "NEW-TABLE-MARK"}'},
            )
            # runB's pre-existing __final__ row diverges from the legacy blob
            # -> the parity step overwrites it LEGACY-AUTHORITATIVELY.
            await conn.execute(
                text(
                    "INSERT INTO run_node_outputs (run_id, node_id, attempt_key, organisation_id, "
                    "outputs_json, created_at, updated_at) "
                    "VALUES (:rid, 'n1', '__final__', :oid, CAST(:out AS jsonb), now(), now())"
                ),
                {"rid": str(run_b), "oid": str(oid), "out": '{"n1": {"v": "NEW"}}'},
            )
    finally:
        await engine.dispose()

    return {
        "org": oid,
        "runs": {
            "run_a": run_a,
            "run_b": run_b,
            "run_c": run_c,
            "run_e": run_e,
            "run_f": run_f,
            "run_g": run_g,
            "run_d": run_d,
        },
        "markers": {"a": marker_a, "c": marker_c, "e": marker_e},
    }


async def _upgrade(db_url: str) -> None:
    """A single real alembic upgrade attempt against the isolated database."""
    with patch.dict(os.environ, {"DATABASE_URL": db_url, "DATABASE_ADMIN_URL": db_url}):
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)


async def _alembic_version(db_url: str) -> str | None:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    finally:
        await engine.dispose()


async def _final_rows(db_url: str, run_id: uuid.UUID) -> list[tuple[str, str, Any, Any, Any]]:
    async with create_async_engine(db_url, poolclass=NullPool).connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT node_id, attempt_key, outputs_json, node_telemetry_json, raw_output_markers "
                    "FROM run_node_outputs WHERE run_id = :rid ORDER BY node_id, attempt_key"
                ),
                {"rid": str(run_id)},
            )
        ).all()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


async def _delete_run(db_url: str, run_id: uuid.UUID) -> None:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM run_node_outputs WHERE run_id = :rid"), {"rid": str(run_id)})
            await conn.execute(text("DELETE FROM runs WHERE id = :rid"), {"rid": str(run_id)})
    finally:
        await engine.dispose()


async def test_0215_drop_legs_and_abort_gates(drop_db_url, monkeypatch: pytest.MonkeyPatch) -> None:
    db_url = str(drop_db_url)
    ids = await _seed_drop_world(db_url)
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", db_url)

    # -- Attempt 1: the pre-flight DRAIN assertion aborts. --------------------
    with pytest.raises(RuntimeError, match="pre-flight drain assertion FAILED"):
        await _upgrade(db_url)
    assert await _alembic_version(db_url) == PREV_REV, "an aborted drop attempt must roll back fully"
    await _delete_run(db_url, ids["runs"]["run_d"])

    # -- Attempt 2: the structural-anomaly gate aborts on a scalar blob. ------
    with pytest.raises(RuntimeError, match="NOT a jsonb object or a jsonb 'null' value"):
        await _upgrade(db_url)
    assert await _alembic_version(db_url) == PREV_REV
    await _delete_run(db_url, ids["runs"]["run_f"])

    # -- Attempt 3: the per-run REPAIR bound aborts on an all-sentinel blob. --
    # run_g (attempt 2 proved it is not junk — its blob IS a jsonb object)
    # has NO quarantine row and NO new-table rows, and its every legacy key
    # is __-prefixed: the pre-bound `while candidates` loop re-selected
    # run_g forever (0 rows per pass, candidate predicate never clears) —
    # a deploy HANG under release.sh's 3x retry. The bound must abort
    # LOUDLY with the sample run id instead.
    with pytest.raises(RuntimeError, match="repair bound exceeded") as excinfo:
        await _upgrade(db_url)
    assert str(ids["runs"]["run_g"]) in str(excinfo.value), "the abort must name the stuck run"
    assert await _alembic_version(db_url) == PREV_REV, "the repair-bound abort must roll back fully"
    await _delete_run(db_url, ids["runs"]["run_g"])

    # -- Attempt 4: markers PRESENT-key value divergence aborts. ---------------
    with pytest.raises(RuntimeError, match="diverges from the reassembled new-table markers"):
        await _upgrade(db_url)
    assert await _alembic_version(db_url) == PREV_REV, "the markers raise must roll back the repair too"
    assert not await _final_rows(db_url, ids["runs"]["run_a"]), "rollback must erase every repair insert"
    # Fix the divergence: align the existing marker row's value with legacy.
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE run_node_outputs SET raw_output_markers = CAST(:v AS jsonb) "
                    "WHERE run_id = :rid AND attempt_key = :k"
                ),
                {
                    "v": '{"raw": "LEGACY-MARK"}',
                    "rid": str(ids["runs"]["run_c"]),
                    "k": ids["markers"]["c"],
                },
            )
    finally:
        await engine.dispose()

    # -- Attempt 5: the migration succeeds all the way through. ---------------
    await _upgrade(db_url)
    assert await _alembic_version(db_url) == "0215_drop_runs_blob_columns"

    async with create_async_engine(db_url, poolclass=NullPool).connect() as conn:
        cols = {
            r[0]
            for r in (
                await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'"))
            ).all()
        }
        indexes = {
            r[0] for r in (await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'runs'"))).all()
        }
        tables = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_name = 'run_node_outputs_quarantine'"
                    )
                )
            ).all()
        }
    assert not ({*RUN_LEGACY_COLUMNS} & cols), "the three runs blob columns must be GONE post-0215"
    assert SWEEP_INDEX not in indexes, "the 0193 sweep index must be dropped with the columns"
    assert tables == {"run_node_outputs_quarantine"}, "the quarantine table survives"

    # Repair correctness: runA's legacy blobs reappear EXACTLY in the
    # new-table rows (outputs + telemetry through __final__, markers rows).
    rows = await _final_rows(db_url, ids["runs"]["run_a"])
    finals = {r[0]: r for r in rows if r[1] == "__final__"}
    assert finals["n1"][2] == {"v": 1}
    assert finals["n1"][3] == {"t": 9}
    markers = {r[1]: r[4] for r in rows if r[1] != "__final__"}
    assert markers[ids["markers"]["a"]] == {"raw": "m"}

    # Parity overwrite: runB's divergent final row is LEGACY-AUTHORITATIVE now.
    rows = await _final_rows(db_url, ids["runs"]["run_b"])
    assert [r[2] for r in rows if r[1] == "__final__"] == [{"v": "LEGACY"}]

    # Markers repair for the unknown-status run: the unparseable key maps to
    # a '__unknown__' node row with the FULL original key preserved (FAR-188),
    # and the unknown population repairs through the MARKERS leg ONLY
    # (0192's twin geometry — no __final__ content rows may exist).
    rows = await _final_rows(db_url, ids["runs"]["run_e"])
    unknown = [r for r in rows if r[0] == "__unknown__"]
    assert len(unknown) == 1, "the sentinel-squatting key must land in a '__unknown__' node row"
    assert unknown[0][1] == "legacy_chunk:weird"
    assert unknown[0][4] == "payload"
    assert not [r for r in rows if r[1] == "__final__"], (
        "the unknown-status population must repair through the MARKERS leg ONLY — 0192's twin geometry"
    )

    # Downgrade NEVER rewinds. (Programmatic command.downgrade() leaves
    # cmd_opts unset and env.py's upgrade-at-head fast-path would skip the
    # invocation: inject the documented downgrade shape, per the 0194 test.)
    from types import SimpleNamespace

    config = _alembic_config(db_url)
    config.cmd_opts = SimpleNamespace(command="downgrade")  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="never rewind past this migration"):
        command.downgrade(config, "-1")
    del config.cmd_opts  # type: ignore[attr-defined]
    assert await _alembic_version(db_url) == "0215_drop_runs_blob_columns"

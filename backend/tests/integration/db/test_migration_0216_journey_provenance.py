"""Integration test for migration 0216_journey_provenance (FAR-794 slice 1).

Runs the real Alembic ``upgrade``/``downgrade`` of revision 0216 against a live
Postgres (testcontainers) on an ISOLATED database (the migration drops data-leg
columns on downgrade — never touch the shared session DB) and verifies:

  * the schema legs: ``journeys.provenance`` (NOT NULL DEFAULT 'derived'),
    ``journeys.first_seen_source`` (nullable) and the
    ``ix_journeys_org_provenance`` btree index;
  * the ``journal``-leg backfill mapping from legacy ``latest_provenance``:
    reported → ('agent', 'agent'),  derived → ('derived', 'derived'),
    NULL / unrecognised → ('derived', NULL) i.e. untouched defaults;
  * the ``runs.work_item_refs`` leg: every ``reported`` element rewritten to
    ``agent``, element order + extra keys + non-matching elements preserved,
    NULL/derived-only runs untouched;
  * idempotency: re-running the data legs against the already-backfilled DB
    converges (no re-write, no change);
  * the downgrade: ``agent`` runs created/updated BEFORE the deploy cutoff are
    rewritten back to ``reported``, the index + columns are dropped, and a
    re-upgrade re-derives the same values (convergent round-trip).

The test resets ``alembic_version`` on ITS OWN database seeded by upgrading
from template0 up to the revision immediately before 0216 (only that one chain
segment re-runs, and 0216 itself is idempotent/existence-guarded).
"""

import os
import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
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

MIGRATION_REV = "0216_journey_provenance"
PREV_REV = "0215_drop_runs_blob_columns"

# Runs seeded with rows whose work_item_refs carry 'reported' so the backfill
# has real work; runs whose refs are derived-only or NULL must be untouched.
_REPORTED = '[{"source": "reported", "kind": "github_issue", "ref": "GH-1"}, {"source": "derived"}]'
_REONLY = '[{"source": "reported", "kind": "linear_ticket", "ref": "LIN-7"}]'
_DERIVED = '[{"source": "derived"}]'

# Seed timestamps BEFORE the migration's deploy cutoff so the downgrade
# reverse-rewrite applies to them.
_old = datetime(2025, 1, 1, tzinfo=UTC)

CUTOFF = datetime(2026, 9, 12, tzinfo=UTC)


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A fresh private DB at PREV_REV; env.py is pointed at it via DATABASE URLs."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"journey_prov_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)
    monkeypatch.setenv("DATABASE_URL", iso_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", iso_url)

    with patch.dict(os.environ, {"DATABASE_URL": iso_url, "DATABASE_ADMIN_URL": iso_url}):
        command.upgrade(_alembic_config(iso_url), PREV_REV)
    try:
        yield iso_url
    finally:
        cleanup = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
        async with cleanup.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ).bindparams(n=db_name)
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await cleanup.dispose()


async def _seed(db_url: str) -> dict[str, uuid.UUID]:
    oid, pid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                    "VALUES (:id, 'prov@example.com', 'prov', 'hash', 'local', true)"
                ),
                {"id": str(uuid.uuid4())},
            )
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'prov', 'prov', '{}')"),
                {"id": str(oid)},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, account_id, name, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, (SELECT id FROM accounts LIMIT 1), 'prov pipeline', "
                    "10, 30, 300, '{}', '[]')"
                ),
                {"id": str(pid), "oid": str(oid)},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                    "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                    "model_backend_pins_json, run_context_defaults, config_json) "
                    "VALUES (:id, :pid, :oid, 1, '{}', '[]', '[]', '[]', '[]', '{}', '{}')"
                ),
                {"id": str(sid), "pid": str(pid), "oid": str(oid)},
            )

            # --- runs: mixed / reported-only / derived-only / NULL work_item_refs ---
            ref_rows = {
                "run_mixed": _REPORTED,
                "run_reonly": _REONLY,
                "run_derived": _DERIVED,
                "run_null": None,
            }
            for idx, (key, refs) in enumerate(ref_rows.items()):
                rid = uuid.uuid4()
                ref_rows[key] = rid
                await conn.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                        "trigger_type, status, run_number, input_hash, langgraph_thread_id, "
                        "created_at, updated_at, work_item_refs) "
                        "VALUES (:id, :oid, :pid, :sid, 'manual', 'complete', :rn, 'ih', :thread, "
                        ":ts, :ts, " + (f"'{refs}'::jsonb" if refs is not None else "NULL") + ")"
                    ),
                    {
                        "id": str(rid),
                        "oid": str(oid),
                        "pid": str(pid),
                        "sid": str(sid),
                        "rn": idx + 1,
                        "thread": f"prov-{rid.hex[:12]}",
                        "ts": _old,
                    },
                )

            # --- journeys: one row per legacy latest_provenance shape ---
            prov_rows = {"j_reported": "reported", "j_derived": "derived", "j_null": None, "j_bogus": "weird"}
            for key, prov in prov_rows.items():
                jid = uuid.uuid4()
                prov_rows[key] = jid
                await conn.execute(
                    text(
                        "INSERT INTO public.journeys (id, organisation_id, kind, ref, "
                        '"canonical_work_item_id", latest_provenance) '
                        "VALUES (:id, :oid, 'github_issue', :refv, :cwi, NULLIF(:prov, '__NULL__'))"
                    ),
                    {
                        "id": str(jid),
                        "oid": str(oid),
                        "refv": f"{key}-{jid.hex[:8]}",
                        "cwi": str(uuid.uuid4()),
                        "prov": prov if prov is not None else "__NULL__",
                    },
                )
    finally:
        await engine.dispose()

    return {"oid": oid, **ref_rows, **prov_rows}


async def _journeys(db_url: str, jid: uuid.UUID) -> tuple[str | None, str | None]:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT provenance, first_seen_source FROM public.journeys WHERE id = :id"),
                    {"id": str(jid)},
                )
            ).one_or_none()
    finally:
        await engine.dispose()
    assert row is not None
    return row[0], row[1]


async def _refs(db_url: str, rid: uuid.UUID) -> list | None:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            val = (
                await conn.execute(text("SELECT work_item_refs FROM public.runs WHERE id = :id"), {"id": str(rid)})
            ).scalar()
    finally:
        await engine.dispose()
    return val


async def _column_exists(db_url: str, name: str) -> bool:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return bool(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name='journeys' AND column_name = :c"
                        ),
                        {"c": name},
                    )
                ).scalar()
            )
    finally:
        await engine.dispose()


async def _upgrade(db_url: str) -> None:
    with patch.dict(os.environ, {"DATABASE_URL": db_url, "DATABASE_ADMIN_URL": db_url}):
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)


async def _downgrade(db_url: str) -> None:
    """Downgrade; injected cmd_opts prevents env.py's upgrade-at-head fast-path
    from silently no-op'ing a programmatic downgrade (see env.py
    ``_invocation_is_upgrade`` — it honours SimpleNamespace(command=...))."""
    with patch.dict(os.environ, {"DATABASE_URL": db_url, "DATABASE_ADMIN_URL": db_url}):
        cfg = _alembic_config(db_url)
        cfg.cmd_opts = types.SimpleNamespace(command="downgrade")
        command.downgrade(cfg, PREV_REV)


async def test_0216_upgrade_backfills_and_is_idempotent(isolated_db_url: str) -> None:
    seeded = await _seed(isolated_db_url)

    await _upgrade(isolated_db_url)

    # Schema legs
    assert await _column_exists(isolated_db_url, "provenance")
    assert await _column_exists(isolated_db_url, "first_seen_source")

    # Journey mapping
    assert await _journeys(isolated_db_url, seeded["j_reported"]) == ("agent", "agent")
    assert await _journeys(isolated_db_url, seeded["j_derived"]) == ("derived", "derived")
    assert await _journeys(isolated_db_url, seeded["j_null"]) == ("derived", None)
    assert await _journeys(isolated_db_url, seeded["j_bogus"]) == ("derived", None)

    # runs.work_item_refs rewrite (order + extra keys + untouched elements)
    assert await _refs(isolated_db_url, seeded["run_mixed"]) == [
        {"source": "agent", "kind": "github_issue", "ref": "GH-1"},
        {"source": "derived"},
    ]
    assert await _refs(isolated_db_url, seeded["run_reonly"]) == [
        {"source": "agent", "kind": "linear_ticket", "ref": "LIN-7"}
    ]
    assert await _refs(isolated_db_url, seeded["run_derived"]) == [{"source": "derived"}]
    assert await _refs(isolated_db_url, seeded["run_null"]) is None

    # Idempotency: rewind alembic_version one revision and re-run the REAL
    # 0216 migration against the already-backfilled data — the column guards
    # / CREATE INDEX IF NOT EXISTS are no-ops, the batching predicates match
    # zero rows, and nothing changes or degrades.
    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = :prev"), {"prev": PREV_REV})
    finally:
        await engine.dispose()
    with patch.dict(os.environ, {"DATABASE_URL": isolated_db_url, "DATABASE_ADMIN_URL": isolated_db_url}):
        command.upgrade(_alembic_config(isolated_db_url), MIGRATION_REV)

    assert await _journeys(isolated_db_url, seeded["j_reported"]) == ("agent", "agent")
    assert await _journeys(isolated_db_url, seeded["j_null"]) == ("derived", None)
    assert await _refs(isolated_db_url, seeded["run_mixed"]) == [
        {"source": "agent", "kind": "github_issue", "ref": "GH-1"},
        {"source": "derived"},
    ]


async def test_0216_downgrade_reverses_and_reupgrades(isolated_db_url: str) -> None:
    seeded = await _seed(isolated_db_url)
    await _upgrade(isolated_db_url)
    assert await _column_exists(isolated_db_url, "provenance")

    await _downgrade(isolated_db_url)

    # Columns + index dropped
    assert not await _column_exists(isolated_db_url, "provenance")
    assert not await _column_exists(isolated_db_url, "first_seen_source")
    # Collapse pre-migration runs (updated_at << cutoff) reverted agent →
    # reported; whenever the await-slice pattern (await _refs(...))[
    # idx]-subscript is misused, the coroutine is subscripted before await.
    mixed_after = await _refs(isolated_db_url, seeded["run_mixed"])
    reonly_after = await _refs(isolated_db_url, seeded["run_reonly"])
    derived_after = await _refs(isolated_db_url, seeded["run_derived"])
    assert mixed_after[0]["source"] == "reported"
    assert reonly_after[0]["source"] == "reported"
    assert derived_after[0]["source"] == "derived"
    assert derived_after == [{"source": "derived"}]  # untouched element preserved

    # Convergent round-trip: re-upgrade re-derives identical shapes.
    await _upgrade(isolated_db_url)
    assert await _column_exists(isolated_db_url, "provenance")
    mixed_again = await _refs(isolated_db_url, seeded["run_mixed"])
    assert mixed_again == [
        {"source": "agent", "kind": "github_issue", "ref": "GH-1"},
        {"source": "derived"},
    ]

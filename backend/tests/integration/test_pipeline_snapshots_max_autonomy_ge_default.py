"""0268: ``ck_pipeline_snapshots_max_autonomy_ge_default`` against real Postgres.

Migration 0259 guarded the VOCABULARY of both snapshot autonomy columns but
never their RELATIVE ORDER - that invariant lived on ``pipelines`` only (0264).
0268 repairs inverted snapshot rows (behaviour-preserving) and adds the
composite CHECK to ``pipeline_snapshots``.

Runs against the migrated testcontainer (real Postgres):

* an INVERTED snapshot pair (default above ceiling) must be rejected by the
  DATABASE - the constraint name in the error is the evidence the right CHECK
  fired,
* every NULL arm and ordered pair must be accepted (both snapshot columns are
  nullable by design, so NULL must round-trip),
* the migration's own repair statement, run against a row it can actually see
  (the CHECK dropped first, since post-migration no inverted row can be
  inserted), must lower the default onto the ceiling AND leave the effective
  autonomy resolution unchanged - and the migration's own ADD + VALIDATE DDL
  must then put the CHECK back and validate it.

The schema-mutating test uses an ISOLATED database (template0 clone migrated
to head) rather than the shared session one. See ``isolated_head_db_url`` for
why that is load-bearing under ``-n 2``.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.core.run_context.autonomy import PIPELINE_MAX_AUTONOMY_KEY, resolve_autonomy

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]  # backend/

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0268_pipeline_snapshots_max_autonomy_ge_default.py"
)
_CONSTRAINT = "ck_pipeline_snapshots_max_autonomy_ge_default"

_MANUAL = "manual_approval"
_FULL = "fully_autonomous"
_NOTIFY = "notify_on_complete"

_SNAPSHOT_INSERT = (
    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
    "max_autonomy_level, default_autonomy_level, graph_json, connector_bindings_json, schema_pins_json, "
    "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
    "VALUES (:id, :pid, :oid, :version, :ceiling, :default, '{}'::json, '[]'::json, '[]'::json, "
    "'[]'::json, '[]'::json, '{}'::json, '{}'::json)"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_0268_pipeline_snapshots_max_autonomy_ge_default", _MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_statements() -> dict[str, str]:
    """The migration's three statements, keyed by role.

    ``UPGRADE_STATEMENTS`` is ordered ADD -> repair -> VALIDATE (0268's
    statement-order contract); keying by role keeps every call site readable
    and makes an order change fail loudly here rather than silently swapping
    the repair and the DDL.
    """
    statements = _load_migration().UPGRADE_STATEMENTS
    assert len(statements) == 3, f"expected add + repair + validate, got {len(statements)} statements"
    add_ddl, repair_ddl, validate_ddl = statements
    return {"add": add_ddl, "repair": repair_ddl, "validate": validate_ddl}


# ---------------------------------------------------------------------------
# Isolated database (schema-mutating test only)
# ---------------------------------------------------------------------------


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    # Skip fileConfig: its default disable_existing_loggers=True would disable
    # every module logger not listed in alembic.ini for the rest of the pytest
    # session (breaks caplog assertions in later tests). Same as migrated_db_url.
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    """Return ``db_url`` with its database name replaced by ``new_db``."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


@pytest_asyncio.fixture
async def isolated_head_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A fresh Postgres database migrated to head - private to this test.

    The repair test has to DROP ``ck_pipeline_snapshots_max_autonomy_ge_default``
    before it can INSERT an inverted row at all (post-migration the CHECK
    rejects one). Running that DROP on the shared session database opens a
    window in which a SIBLING test can assert on a CHECK that is currently
    absent: CI runs integration with ``-n 2`` against one Postgres, so a
    sibling landing inside the dropped window gets a false red - or, worse, a
    vacuously passing "the CHECK is present" assertion of our own.

    So: clone ``template0`` (guaranteed empty), migrate it to head, and hand
    the test its own engine. The shared session schema is never touched, which
    also removes the restore-masking hazard entirely - see the test body.

    ``migrations/env.py`` resolves the alembic target from ``DATABASE_URL`` /
    ``DATABASE_ADMIN_URL`` rather than from the Config URL, so both are pinned
    to the isolated database for the upgrade (``monkeypatch`` for anything the
    test body reads, ``patch.dict`` for the upgrade call itself).
    """
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"snapshot_ceiling_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        # Clone template0 (always empty) rather than the default template1,
        # which in CI can already carry the modulo schema - a non-empty clone
        # would make the upgrade collide with already-applied migrations.
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

    with patch.dict(
        os.environ,
        {"DATABASE_URL": iso_url, "DATABASE_ADMIN_URL": iso_url},
    ):
        command.upgrade(_alembic_config(iso_url), "head")

    try:
        yield iso_url
    finally:
        cleanup = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
        async with cleanup.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": db_name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await cleanup.dispose()


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


async def _seed_org_account_and_pipeline(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A minimal organisation + account + pipeline INSIDE the engine's own DB.

    ``pipeline_snapshots.pipeline_id`` is a NOT NULL FK to ``pipelines``, which
    in turn needs an org and an account - and the isolated database has no rows
    yet (the session-scoped ``test_org`` / ``test_user`` fixtures live on the
    SHARED database and are invisible here).
    """
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    slug = f"snapshot-iso-{org_id.hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": slug, "slug": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(account_id), "email": f"{slug}@example.com", "name": "0268 repair"},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, 'manual_approval')"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "aid": str(account_id), "name": f"{slug}-pipeline"},
        )
    return org_id, account_id, pipeline_id


async def _insert_snapshot(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    default: str | None,
    ceiling: str | None,
    version: int = 1,
) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(_SNAPSHOT_INSERT),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "version": version,
                "default": default,
                "ceiling": ceiling,
            },
        )
    return snapshot_id


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


async def _constraint_validated(db_engine: AsyncEngine) -> bool:
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
                {"name": _CONSTRAINT},
            )
        ).first()
    if row is None:
        return False
    return bool(row[0])


async def _restore_constraint(db_engine: AsyncEngine) -> None:
    """Re-add + VALIDATE 0268's CHECK (both statements are existence-gated)."""
    statements = _upgrade_statements()
    async with db_engine.begin() as conn:
        await conn.execute(text(statements["add"]))
        await conn.execute(text(statements["validate"]))


# ---------------------------------------------------------------------------
# Shared-database tests (read/INSERT only - no DDL)
# ---------------------------------------------------------------------------


async def test_inverted_snapshot_pair_is_rejected_by_the_db(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """``default > ceiling`` fails at the DATABASE - not just at the app layer.

    The rejected INSERT persists nothing, so there is no row to clean up beyond
    the pipeline fixture this test owns. This test never mutates schema, so it
    belongs on the shared database - and it is also the check that the shared
    schema actually carries the CHECK.
    """
    pipeline_id = await _insert_pipeline_fixture(db_engine, test_org, test_user)
    try:
        with pytest.raises(DBAPIError) as excinfo:
            await _insert_snapshot(
                db_engine,
                test_org,
                pipeline_id,
                default=_FULL,
                ceiling=_MANUAL,
            )
        message = str(excinfo.value)
        assert _CONSTRAINT in message, message
        assert "violates check constraint" in message.lower(), message
    finally:
        await _cleanup(db_engine, pipeline_id)


async def _insert_pipeline_fixture(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    """Minimal committed pipelines row on the SHARED database (own row)."""
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, 'manual_approval')"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "name": f"snapshot-ge-default-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


@pytest.mark.parametrize(
    ("default", "ceiling"),
    [
        # NULL ceiling = "ceiling is the default": always valid (first arm).
        (_FULL, None),
        # NULL default: the CASE comparison is NULL, which a CHECK satisfies.
        (None, _MANUAL),
        # Equal and ordered pairs.
        (_MANUAL, _MANUAL),
        (_NOTIFY, _FULL),
    ],
)
async def test_valid_snapshot_pairs_round_trip(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    default: str | None,
    ceiling: str | None,
) -> None:
    pipeline_id = await _insert_pipeline_fixture(db_engine, test_org, test_user)
    try:
        snapshot_id = await _insert_snapshot(
            db_engine,
            test_org,
            pipeline_id,
            default=default,
            ceiling=ceiling,
        )
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT default_autonomy_level, max_autonomy_level FROM pipeline_snapshots WHERE id = :id"),
                    {"id": str(snapshot_id)},
                )
            ).one()
        assert stored[0] == default
        assert stored[1] == ceiling
    finally:
        await _cleanup(db_engine, pipeline_id)


# ---------------------------------------------------------------------------
# Schema-mutating test (isolated database)
# ---------------------------------------------------------------------------


async def test_repair_lowers_the_default_onto_the_ceiling_and_preserves_behaviour(
    isolated_head_db_url: str,
) -> None:
    """The migration's DATA-MUTATING statement, on a row it can actually see.

    Post-migration no inverted row can be INSERTed, so the CHECK is dropped
    first - on the ISOLATED database, never the shared one (see the fixture).

    The repaired snapshot must (a) satisfy the invariant and (b) resolve
    autonomy EXACTLY as before: the executor pins BOTH snapshot values into the
    run context, and ``resolve_autonomy`` computes
    ``base = min(default, ceiling)``, which for an inverted row is already the
    ceiling, so lowering the default onto it changes nothing observable.

    There is deliberately NO ``finally``-restore here. Two reasons:

    * a ``finally`` that raises replaces the body's original exception, hiding
      the real failure behind a DDL error - and (on a shared database) can
      leave the CHECK absent run-wide for every later test;
    * there is nothing left to protect: the database is dropped by the fixture
      whatever happens.

    So the restore runs IN the body - a failure there is reported as itself,
    with its own traceback - and is then asserted, which is what proves the
    migration's own existence-gated ADD + VALIDATE actually work.
    """
    statements = _upgrade_statements()
    engine = create_async_engine(isolated_head_db_url, poolclass=NullPool)
    try:
        org_id, _account_id, pipeline_id = await _seed_org_account_and_pipeline(engine)

        # Literal DDL (the constraint name is spelled out rather than formatted
        # in - no f-string SQL). Dropping the CHECK is what makes the inverted
        # row insertable at all.
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE pipeline_snapshots DROP CONSTRAINT ck_pipeline_snapshots_max_autonomy_ge_default")
            )

        snapshot_id = await _insert_snapshot(
            engine,
            org_id,
            pipeline_id,
            default=_FULL,
            ceiling=_MANUAL,
        )

        # The executor pins BOTH values into the run context (ceiling key set
        # whenever the snapshot ceiling is non-NULL), so this is the resolution
        # shape an inverted snapshot actually produces.
        run_context = {PIPELINE_MAX_AUTONOMY_KEY: _MANUAL}
        before = resolve_autonomy(_FULL, run_context).effective
        # A context-setter recommendation is clamped by the same min() both
        # before and after the repair - check that arm too.
        recommended = dict(run_context)
        recommended["autonomy_recommendation"] = _NOTIFY
        before_with_rec = resolve_autonomy(_FULL, recommended).effective

        async with engine.begin() as conn:
            await conn.execute(text(statements["repair"]))

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT default_autonomy_level, max_autonomy_level FROM pipeline_snapshots WHERE id = :id"),
                    {"id": str(snapshot_id)},
                )
            ).one()
        assert row[0] == _MANUAL, f"repair must lower the default onto the ceiling, got {row[0]}"
        assert row[1] == _MANUAL
        # The invariant itself now holds for the repaired row.
        assert row[0] == row[1]

        after = resolve_autonomy(str(row[0]), run_context).effective
        assert after == before, f"effective autonomy changed: {before} -> {after}"
        after_with_rec = resolve_autonomy(str(row[0]), recommended).effective
        assert after_with_rec == before_with_rec, f"recommendation clamp changed: {before_with_rec} -> {after_with_rec}"

        # Put the CHECK back with the migration's OWN statements (both
        # existence-gated, so this is a no-op if the drop above never ran) and
        # prove it validates - the ADD/VALIDATE half of 0268.
        await _restore_constraint(engine)
        assert await _constraint_validated(engine), "the CHECK must be restored and VALIDATEd after the repair"
    finally:
        await engine.dispose()

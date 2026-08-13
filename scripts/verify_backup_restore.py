#!/usr/bin/env python3
"""FAR-19 CI gate: prove the full backup -> restore -> downgrade -> upgrade path.

This script is the automated backup/restore gate. It runs against a REAL
Postgres (the calling workflow starts a ``postgres:16-alpine`` container and
installs the Postgres client tools on the runner) and exercises the production
backup/restore CLI end-to-end:

  1. create a fresh scratch database and run ``alembic upgrade heads`` from scratch
  2. seed representative data (organisation + account + pipeline + snapshot + run)
  3. create the LangGraph checkpoint tables the way the app does at startup
  4. ``modulo backup`` -> pg_dump + checkpoint JSON exports + backup manifest
  5. create a NEW empty database and ``modulo restore`` into it
  6. assert data integrity (pipeline + run present and correct) after restore
  7. ``alembic downgrade -1`` then ``alembic upgrade heads`` (downgrade works)
  8. assert data integrity again after the downgrade/upgrade round-trip

Exit code 0 = gate green; any non-zero exit = gate red (CI fails).

Usage (from ``backend/``):
    uv run --no-sync python ../scripts/verify_backup_restore.py

Requires the following environment variables (the workflow sets them):
    DATABASE_URL   base Postgres URL of the CI server (e.g.
                   postgresql+asyncpg://modulo_test:modulo_test@localhost:5433/modulo_test)
    SECRET_KEY, FERNET_KEY   app settings consumed by the backup/restore CLI
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, urlunparse

import click
import psycopg
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory

# Repo-root scripts live at <repo>/scripts/; the backend is one level up.
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

_ROLE_DDL = [
    'DROP ROLE IF EXISTS "modulo_migrate"',
    'DROP ROLE IF EXISTS "modulo_breakglass"',
    'DROP ROLE IF EXISTS "modulo_app"',
    "CREATE ROLE modulo_migrate NOSUPERUSER NOLOGIN BYPASSRLS",
    "CREATE ROLE modulo_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'",
    "CREATE ROLE modulo_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'",
]


def _db_name(database_url: str) -> str:
    parsed = urlparse(database_url)
    name = parsed.path.lstrip("/").split("/")[0].split("?")[0]
    return name or "modulo_test"


def _plain_url(url: str) -> str:
    """Strip an async driver prefix so psycopg / pg_dump / psql can use it.

    The workflow (like production and conftest.py) sets DATABASE_URL to the
    ``postgresql+asyncpg`` scheme — importing ``modulo.cli.*`` builds an engine
    from ``modulo.db.session`` at import time and needs the asyncpg dialect.
    psycopg and the PG client tools only accept plain ``postgresql://``.
    """
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def _maintenance_url(database_url: str) -> str:
    """Point the URL at the ``postgres`` maintenance database (used to create/drop)."""
    parsed = urlparse(database_url)
    return urlunparse(parsed._replace(path="/postgres"))


def _with_db(database_url: str, name: str) -> str:
    parsed = urlparse(database_url)
    return urlunparse(parsed._replace(path=f"/{name}"))


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


# ── Alembic helpers (mirror backend/tests/integration/conftest.py) ────────────


def _alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _alembic_head() -> str:
    return str(ScriptDirectory.from_config(_alembic_config()).get_current_head())


def _run_alembic(database_url: str, direction: str, revision: str) -> None:
    """Run alembic against a specific database URL.

    ``env.py`` re-reads ``DATABASE_ADMIN_URL`` / ``DATABASE_URL`` on every run
    (it deletes the cached env module), so both are pinned to the target URL
    for the duration of the invocation, exactly like conftest.py does.
    """
    previous = {key: os.environ.get(key) for key in ("DATABASE_ADMIN_URL", "DATABASE_URL")}
    os.environ["DATABASE_ADMIN_URL"] = database_url
    os.environ["DATABASE_URL"] = database_url
    try:
        config = _alembic_config()
        if direction == "downgrade":
            # env.py's boot fast-path skips the run when the DB is already at
            # head AND the invocation moves forward. A downgrade must ALWAYS
            # run, so mark the command explicitly or it becomes a silent no-op.
            config.cmd_opts = SimpleNamespace(command="downgrade")
        run = alembic_command.upgrade if direction == "upgrade" else alembic_command.downgrade
        run(config, revision)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _verify_alembic_at_head(database_url: str) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
    if row is None:
        _fail("alembic_version table is empty after upgrade heads")
    current = str(row[0])
    head = _alembic_head()
    if current != head:
        _fail(f"DB is at migration {current}, expected head {head}")


# ── Postgres admin helpers ────────────────────────────────────────────────────


def _provision_break_glass_roles(maintenance: str) -> None:
    """Create the runtime roles before the migrations run.

    Migrations 0036/0037/0066/0067 ``SET ROLE modulo_migrate`` and re-own
    tables to it, so ``modulo_migrate``, ``modulo_breakglass`` and
    ``modulo_app`` must all exist before ``alembic upgrade heads`` (same
    provisioning as backend/tests/integration/conftest.py).
    """
    with psycopg.connect(maintenance, autocommit=True) as conn:
        for statement in _ROLE_DDL:
            conn.execute(statement)


def _drop_database(maintenance: str, name: str) -> None:
    with psycopg.connect(maintenance, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _create_database(maintenance: str, name: str) -> None:
    with psycopg.connect(maintenance, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')


def _ensure_alembic_table(database_url: str) -> None:
    """Pre-create alembic_version with VARCHAR(255) for branch migration IDs."""
    with psycopg.connect(database_url) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        conn.commit()


def _create_checkpoint_tables(database_url: str) -> None:
    """Create the LangGraph checkpoint tables as the app does at startup.

    These tables are NOT part of the Alembic migration chain — the production
    app creates them in ``main.py``'s lifespan via ``ModuloPostgresSaver.setup()``.
    A backup taken before first app start would not see them, so the gate
    mirrors production startup ordering (conftest.py uses the same DDL).
    """
    from modulo.core.pipeline_engine.modulo_saver import _MIGRATION_SQL

    with psycopg.connect(database_url, autocommit=True) as conn:
        for ddl in _MIGRATION_SQL:
            conn.execute(ddl)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN (%s, %s, %s)",
            _CHECKPOINT_TABLES,
        )
        found = {row[0] for row in cur.fetchall()}
    missing = set(_CHECKPOINT_TABLES) - found
    if missing:
        _fail(f"checkpoint tables were not created: {sorted(missing)}")


# ── Representative data seeding ───────────────────────────────────────────────


def _seed_data(database_url: str) -> dict[str, uuid.UUID]:
    """Insert an org + account + membership + pipeline + snapshot + run."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    input_hash = hashlib.sha256(b"far-19-gate-seed-input").hexdigest()

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (%s, %s, %s, '{}'::json, '{}'::json)",
                (str(org_id), "Backup Gate Org", f"gate-{org_id.hex[:8]}"),
            )
            cur.execute(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (%s, %s, %s, 'hash', 'local', true)",
                (str(account_id), "gate-admin@example.com", "Gate Admin"),
            )
            cur.execute(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (%s, %s, %s, 'admin')",
                (str(membership_id), str(account_id), str(org_id)),
            )
            cur.execute(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                "VALUES (%s, %s, %s, %s, 10, 30, 300, '{}'::json, '[]'::json)",
                (str(pipeline_id), str(org_id), "Backup Gate Pipeline", str(account_id)),
            )
            cur.execute(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                "model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (%s, %s, %s, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'{}'::json, '{}'::json)",
                (str(snapshot_id), str(pipeline_id), str(org_id)),
            )
            cur.execute(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id) "
                "VALUES (%s, %s, %s, %s, 'manual', 'complete', 1, %s, %s)",
                (
                    str(run_id),
                    str(org_id),
                    str(pipeline_id),
                    str(snapshot_id),
                    input_hash,
                    f"thread-{uuid.uuid4()}",
                ),
            )
        conn.commit()
    return {"org_id": org_id, "pipeline_id": pipeline_id, "run_id": run_id}


def _assert_integrity(database_url: str, expected: dict[str, uuid.UUID]) -> None:
    """Assert the seeded pipeline + run survived backup/restore intact."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM pipelines WHERE id = %s", (str(expected["pipeline_id"]),))
        pipeline_count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT name, organisation_id FROM pipelines WHERE id = %s",
            (str(expected["pipeline_id"]),),
        )
        pipeline_row = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM runs WHERE id = %s", (str(expected["run_id"]),))
        run_count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT status, run_number, organisation_id FROM runs WHERE id = %s",
            (str(expected["run_id"]),),
        )
        run_row = cur.fetchone()

    if pipeline_count != 1:
        _fail(f"pipeline {expected['pipeline_id']} missing after restore (count={pipeline_count})")
    if pipeline_row is None or pipeline_row[0] != "Backup Gate Pipeline":
        _fail(f"pipeline {expected['pipeline_id']} has wrong name after restore: {pipeline_row}")
    if str(pipeline_row[1]) != str(expected["org_id"]):
        _fail(f"pipeline {expected['pipeline_id']} org mismatch after restore: {pipeline_row[1]}")
    if run_count != 1:
        _fail(f"run {expected['run_id']} missing after restore (count={run_count})")
    if run_row is None or run_row[0] != "complete" or run_row[1] != 1:
        _fail(f"run {expected['run_id']} has wrong status/number after restore: {run_row}")
    if str(run_row[2]) != str(expected["org_id"]):
        _fail(f"run {expected['run_id']} org mismatch after restore: {run_row[2]}")
    print("      data integrity verified (pipeline + run present with correct values)")


# ── Backup/restore CLI invocation (real code paths) ───────────────────────────


def _invoke_cli(argv: list[str]) -> None:
    from click.testing import CliRunner
    from modulo.cli.backup import cli

    runner = CliRunner()
    try:
        result = runner.invoke(cli, argv, catch_exceptions=False)
    except click.ClickException as exc:
        _fail(f"CLI {argv[0]} failed: {exc}")
    if result.exit_code != 0:
        _fail(f"CLI {argv[0]} failed with exit code {result.exit_code}\n{result.output}")


# ── Gate ──────────────────────────────────────────────────────────────────────


def main() -> None:
    raw_url = os.environ.get("DATABASE_URL", "")
    if not raw_url:
        _fail("DATABASE_URL is required (the workflow sets it to the CI Postgres server)")
    # Keep the raw (asyncpg) value in the environment so importing modulo.cli.*
    # builds its engine correctly; all connections in this script use the plain
    # scheme that psycopg / pg_dump / psql accept.
    database_url = _plain_url(raw_url)

    for tool in ("pg_dump", "psql"):
        if shutil.which(tool) is None:
            _fail(f"'{tool}' not found on PATH — install the Postgres client tools (postgresql-client-16)")

    maintenance = _maintenance_url(database_url)
    base_name = _db_name(database_url)
    source_name = f"{base_name}_backup_src"
    restored_name = f"{base_name}_restored"
    source_url = _with_db(database_url, source_name)
    restored_url = _with_db(database_url, restored_name)

    print("=== FAR-19 backup/restore gate ===")
    print(f"server: {database_url.split('@')[-1]}")

    print("\n[1/8] Dropping scratch databases + provisioning runtime roles")
    _drop_database(maintenance, source_name)
    _drop_database(maintenance, restored_name)
    _create_database(maintenance, source_name)
    _create_database(maintenance, restored_name)
    _provision_break_glass_roles(maintenance)

    print(f"[2/8] Running alembic upgrade heads from scratch on '{source_name}'")
    _ensure_alembic_table(source_url)
    _run_alembic(source_url, "upgrade", "heads")
    _verify_alembic_at_head(source_url)

    print("[3/8] Seeding representative data (org + account + pipeline + snapshot + run)")
    expected = _seed_data(source_url)

    print("[4/8] Creating LangGraph checkpoint tables (as the app does at startup)")
    _create_checkpoint_tables(source_url)

    print(f"[5/8] Running 'modulo backup' on '{source_name}'")
    backup_dir = Path(tempfile.mkdtemp(prefix="modulo-gate-backup-"))
    _invoke_cli(["backup", "--db-url", source_url, "--output-dir", str(backup_dir)])
    sql_dump = backup_dir / "database.sql"
    if not sql_dump.exists():
        _fail(f"backup did not produce database.sql in {backup_dir}")
    print(f"      pg_dump written: {sql_dump.stat().st_size} bytes")

    print(f"[6/8] Running 'modulo restore' into fresh empty database '{restored_name}'")
    _invoke_cli(["restore", str(backup_dir), "--db-url", restored_url, "--yes"])

    print("[7/8] Asserting data integrity after restore")
    _assert_integrity(restored_url, expected)

    print("[8/8] Running alembic downgrade -1 then upgrade heads on the restored DB")
    _run_alembic(restored_url, "downgrade", "-1")
    _run_alembic(restored_url, "upgrade", "heads")
    _assert_integrity(restored_url, expected)

    print("\nGATE PASSED: backup -> restore -> downgrade -> upgrade round-trip verified.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""FAR-19 gate: prove the pg_dump backup + Alembic upgrade/downgrade path.

This script is the automated evidence behind ``docs/operations/backup.md``.
It runs the real ``modulo backup`` / ``modulo restore`` CLI against a real
Postgres and proves the whole self-hosted disaster-recovery loop:

  1. reset two scratch databases on the target server
  2. provision the runtime roles the migration chain requires
  3. ``alembic upgrade heads`` from scratch, then assert the DB is at head
  4. seed representative data (org, account, membership, pipeline,
     snapshot, run) through the ORM
  5. create the LangGraph checkpoint tables exactly as app startup does
  6. ``modulo backup`` -> pg_dump + checkpoint JSON exports + manifest
  7. ``modulo restore`` that backup into the second, empty database
  8. assert the seeded rows survived the restore
  9. ``alembic downgrade -1`` on the restored DB, assert it moved OFF head and
     the seeded rows are still there
 10. ``alembic upgrade heads`` again, assert it is back at head with the seeded
     rows still intact

Every step failure exits non-zero: no step is reported as having run unless
it ran, and the downgrade/upgrade steps assert the recorded revision actually
moved rather than trusting that the command was not skipped.

Usage
-----
Start a throwaway Postgres and run against it::

    docker run -d --rm --name modulo-dr-gate \\
        -e POSTGRES_USER=modulo_test -e POSTGRES_PASSWORD=modulo_test \\
        -e POSTGRES_DB=modulo_test -p 55555:5432 postgres:16-alpine

    export DATABASE_URL=postgresql+asyncpg://modulo_test:modulo_test@localhost:55555/modulo_test
    uv run --project backend python scripts/verify_backup_restore.py

``--database-url`` overrides ``$DATABASE_URL``. ``pg_dump`` and ``psql``
(major version >= the server's) must be on ``PATH``: ``modulo backup`` and
``modulo restore`` shell out to them.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, urlunparse

import psycopg
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

_SEED_PIPELINE_NAME = "Backup Gate Pipeline"
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

# Migrations 0036/0037/0066/0067 SET ROLE modulo_migrate and re-own tables to
# it, and the reconciliation chain grants EXECUTE to modulo_app /
# modulo_breakglass, so all three must exist before `alembic upgrade heads`.
# Mirrors backend/tests/integration/conftest.py.
_ROLE_DDL = (
    'DROP ROLE IF EXISTS "modulo_migrate"',
    'DROP ROLE IF EXISTS "modulo_breakglass"',
    'DROP ROLE IF EXISTS "modulo_app"',
    "CREATE ROLE modulo_migrate NOSUPERUSER NOLOGIN BYPASSRLS",
    "CREATE ROLE modulo_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'",
    "CREATE ROLE modulo_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'",
)


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def _plain_url(url: str) -> str:
    """Strip an async/psycopg driver prefix for psycopg / pg_dump / psql."""
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def _sqlalchemy_url(url: str) -> str:
    """Same server, declared with the sync psycopg driver for the ORM."""
    plain = _plain_url(url)
    return "postgresql+psycopg://" + plain[len("postgresql://") :]


def _maintenance_url(url: str) -> str:
    """Point the URL at the ``postgres`` maintenance database."""
    return urlunparse(urlparse(url)._replace(path="/postgres"))


def _with_db(url: str, name: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{name}"))


def _base_db_name(url: str) -> str:
    name = urlparse(url).path.lstrip("/").split("/")[0].split("?")[0]
    return name or "modulo_test"


# -- Postgres admin helpers -------------------------------------------------


def _drop_database(maintenance: str, name: str) -> None:
    with psycopg.connect(maintenance, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _create_database(maintenance: str, name: str) -> None:
    with psycopg.connect(maintenance, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')


def _provision_roles(maintenance: str) -> None:
    with psycopg.connect(maintenance, autocommit=True) as conn:
        for statement in _ROLE_DDL:
            conn.execute(statement)


# -- Alembic helpers --------------------------------------------------------


def _alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "src" / "modulo" / "db" / "migrations"))
    # Alembic's fileConfig() would disable loggers for the rest of the process.
    config.config_file_name = None
    return config


def _head_revision() -> str:
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if not head:
        _fail("the migration chain has no head revision")
    return str(head)


def _parent_revision(head: str) -> str:
    revision = ScriptDirectory.from_config(_alembic_config()).get_revision(head)
    if revision is None or revision.down_revision is None:
        _fail(f"head revision {head} has no parent, so `-1` cannot be proven")
    parent = revision.down_revision
    if isinstance(parent, tuple):
        _fail(f"head revision {head} has multiple parents, so `-1` is ambiguous")
    return str(parent)


def _run_alembic(database_url: str, direction: str, revision: str) -> None:
    """Run ``alembic upgrade``/``downgrade`` against one database URL.

    ``env.py`` re-reads ``DATABASE_ADMIN_URL`` / ``DATABASE_URL`` on every
    run, so both are pinned here for the duration of the invocation.

    ``env.py`` also short-circuits an UPGRADE when the database is already at
    head (the boot fast-path). That skip is only valid for upgrades: a
    downgrade must always run. The direction is therefore marked explicitly
    on ``config.cmd_opts`` - a shape ``env.py`` documents as supported - and
    the caller asserts the recorded revision actually moved, so a skipped
    downgrade is a red gate rather than a silent pass.
    """
    keys = ("DATABASE_ADMIN_URL", "DATABASE_URL")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["DATABASE_ADMIN_URL"] = database_url
    os.environ["DATABASE_URL"] = database_url
    try:
        config = _alembic_config()
        if direction == "downgrade":
            config.cmd_opts = SimpleNamespace(command="downgrade")
        run = alembic_command.upgrade if direction == "upgrade" else alembic_command.downgrade
        run(config, revision)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _current_revision(database_url: str) -> str:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
    if row is None:
        _fail("alembic_version is empty")
    return str(row[0])


# -- LangGraph checkpoint tables (created by app startup, not migrations) ---


def _create_checkpoint_tables(database_url: str) -> None:
    from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver

    with psycopg.connect(database_url, autocommit=True) as conn:
        for statement in ModuloPostgresSaver.MIGRATIONS:
            conn.execute(statement)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (list(_CHECKPOINT_TABLES),),
        )
        found = {row[0] for row in cur.fetchall()}
    missing = set(_CHECKPOINT_TABLES) - found
    if missing:
        _fail(f"checkpoint tables were not created: {sorted(missing)}")


# -- Representative data ----------------------------------------------------


def _seed_data(database_url: str) -> dict[str, uuid.UUID]:
    """Insert an org + account + membership + pipeline + snapshot + run.

    Goes through the ORM models so the seed tracks the schema instead of
    re-typing a column list that a later migration would silently invalidate.
    """
    from modulo.db.models.account import Account
    from modulo.db.models.org_membership import OrgMembership
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.models.run import Run
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    ids = {key: uuid.uuid4() for key in ("org", "account", "membership", "pipeline", "snapshot", "run")}
    engine = create_engine(_sqlalchemy_url(database_url))
    try:
        with Session(engine) as session:
            session.add(
                Organisation(
                    id=ids["org"],
                    name="Backup Gate Org",
                    slug=f"backup-gate-{ids['org'].hex[:8]}",
                    settings_json={},
                    otel_config_json={},
                )
            )
            session.add(
                Account(
                    id=ids["account"],
                    email=f"backup-gate-{ids['account'].hex[:8]}@example.com",
                    display_name="Backup Gate Admin",
                    password_hash="not-a-real-hash",
                    auth_provider="local",
                    active=True,
                )
            )
            session.flush()
            session.add(
                OrgMembership(
                    id=ids["membership"],
                    organisation_id=ids["org"],
                    account_id=ids["account"],
                    role="admin",
                )
            )
            session.add(
                Pipeline(
                    id=ids["pipeline"],
                    organisation_id=ids["org"],
                    name=_SEED_PIPELINE_NAME,
                    account_id=ids["account"],
                    graph_nodes_json=[],
                    run_context_defaults={},
                )
            )
            session.flush()
            session.add(
                PipelineSnapshot(
                    id=ids["snapshot"],
                    pipeline_id=ids["pipeline"],
                    organisation_id=ids["org"],
                    snapshot_version=1,
                    graph_json={},
                    connector_bindings_json=[],
                    schema_pins_json=[],
                    prompt_pins_json=[],
                    model_backend_pins_json=[],
                    config_json={},
                    run_context_defaults={},
                )
            )
            session.flush()
            session.add(
                Run(
                    id=ids["run"],
                    organisation_id=ids["org"],
                    pipeline_id=ids["pipeline"],
                    snapshot_id=ids["snapshot"],
                    trigger_type="manual",
                    status="complete",
                    run_number=1,
                    input_hash=hashlib.sha256(b"far-19-gate").hexdigest(),
                    langgraph_thread_id=f"thread-{ids['run']}",
                )
            )
            session.commit()
    finally:
        engine.dispose()
    return ids


def _assert_integrity(database_url: str, expected: dict[str, uuid.UUID], stage: str) -> None:
    """Assert the seeded rows survived intact, naming the stage on failure."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, organisation_id FROM pipelines WHERE id = %s",
            (str(expected["pipeline"]),),
        )
        pipeline = cur.fetchone()
        cur.execute(
            "SELECT status, run_number, organisation_id FROM runs WHERE id = %s",
            (str(expected["run"]),),
        )
        run = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM pipeline_snapshots WHERE id = %s",
            (str(expected["snapshot"]),),
        )
        snapshots = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM org_memberships WHERE id = %s",
            (str(expected["membership"]),),
        )
        memberships = int(cur.fetchone()[0])

    org_id = str(expected["org"])
    if pipeline is None:
        _fail(f"{stage}: pipeline {expected['pipeline']} is missing")
    if pipeline[0] != _SEED_PIPELINE_NAME:
        _fail(f"{stage}: pipeline name changed to {pipeline[0]!r}")
    if str(pipeline[1]) != org_id:
        _fail(f"{stage}: pipeline organisation changed to {pipeline[1]}")
    if snapshots != 1:
        _fail(f"{stage}: snapshot row missing (count={snapshots})")
    if memberships != 1:
        _fail(f"{stage}: membership row missing (count={memberships})")
    if run is None:
        _fail(f"{stage}: run {expected['run']} is missing")
    if run[0] != "complete" or run[1] != 1:
        _fail(f"{stage}: run status/run_number changed to {run[0]!r}/{run[1]!r}")
    if str(run[2]) != org_id:
        _fail(f"{stage}: run organisation changed to {run[2]}")
    print(f"      {stage}: seeded rows intact")


# -- The real CLI -----------------------------------------------------------


def _ensure_settings_env() -> None:
    """Supply the settings ``get_settings()`` validates when the CLI runs.

    The gate only ever touches scratch databases it created itself, so a
    missing ``SECRET_KEY`` / ``FERNET_KEY`` is filled with a throwaway value
    for this process. A real value already in the environment is never
    overridden, and nothing is persisted.
    """
    from cryptography.fernet import Fernet

    if not os.environ.get("SECRET_KEY"):
        os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
        print("      SECRET_KEY unset: using a throwaway value for this run")
    if not os.environ.get("FERNET_KEY"):
        os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
        print("      FERNET_KEY unset: using a throwaway value for this run")


def _invoke_cli(argv: list[str]) -> None:
    from click.testing import CliRunner
    from modulo.cli.backup import cli

    result = CliRunner().invoke(cli, argv, catch_exceptions=False)
    if result.exit_code != 0:
        _fail(f"`modulo {argv[0]}` exited {result.exit_code}\n{result.output}")


# -- Gate -------------------------------------------------------------------


def _check_client_tools() -> None:
    for tool in ("pg_dump", "psql"):
        if shutil.which(tool) is None:
            _fail(f"'{tool}' is not on PATH - install the Postgres client tools (postgresql-client-16)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_backup_restore.py",
        description=(
            "Prove the modulo backup/restore CLI and the Alembic upgrade/downgrade path against a real Postgres."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="base Postgres URL of the server to test against (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--keep-databases",
        action="store_true",
        help="leave the scratch databases and backup artefacts behind for inspection",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        _fail("--database-url (or $DATABASE_URL) is required")
    _check_client_tools()

    database_url = _plain_url(args.database_url)
    maintenance = _maintenance_url(database_url)
    base_name = _base_db_name(database_url)
    source_name = f"{base_name}_backup_src"
    restored_name = f"{base_name}_restored"
    source_url = _with_db(database_url, source_name)
    restored_url = _with_db(database_url, restored_name)
    head = _head_revision()
    parent = _parent_revision(head)

    print("=== FAR-19 backup / restore + upgrade gate ===")
    print(f"server: {database_url.split('@')[-1]}")
    print(f"head revision: {head}\n")

    print(f"[1/10] Resetting scratch databases '{source_name}' / '{restored_name}'")
    _drop_database(maintenance, source_name)
    _drop_database(maintenance, restored_name)
    _create_database(maintenance, source_name)
    _create_database(maintenance, restored_name)

    print("[2/10] Provisioning the runtime roles the migration chain requires")
    _provision_roles(maintenance)

    print(f"[3/10] alembic upgrade heads from scratch on '{source_name}'")
    _run_alembic(source_url, "upgrade", "heads")
    current = _current_revision(source_url)
    if current != head:
        _fail(f"upgrade heads left the database at {current}, expected {head}")
    print(f"      at head: {current}")

    print("[4/10] Seeding representative data through the ORM")
    expected = _seed_data(source_url)
    print(f"      pipeline {expected['pipeline']} / run {expected['run']}")

    print("[5/10] Creating the LangGraph checkpoint tables (as app startup does)")
    _create_checkpoint_tables(source_url)

    print(f"[6/10] Running 'modulo backup' on '{source_name}'")
    _ensure_settings_env()
    backup_dir = Path(tempfile.mkdtemp(prefix="modulo-dr-gate-"))
    _invoke_cli(["backup", "--db-url", source_url, "--output-dir", str(backup_dir)])
    sql_dump = backup_dir / "database.sql"
    if not sql_dump.exists():
        _fail(f"backup produced no database.sql in {backup_dir}")
    for name in ("backup-info.json", "checkpoints.json"):
        if not (backup_dir / name).exists():
            _fail(f"backup produced no {name} in {backup_dir}")
    print(f"      database.sql: {sql_dump.stat().st_size} bytes")

    print(f"[7/10] Running 'modulo restore' into empty database '{restored_name}'")
    # The target is a scratch database created empty moments ago, so the
    # pre-restore safety dump protects nothing and would litter the checkout
    # with `pre-restore-dump-*` directories on every run.
    _invoke_cli(
        [
            "restore",
            str(backup_dir),
            "--db-url",
            restored_url,
            "--yes",
            "--no-safety-dump",
        ]
    )

    print("[8/10] Asserting data integrity after restore")
    _assert_integrity(restored_url, expected, "after restore")

    print(f"[9/10] alembic downgrade -1 on the restored database (expect {parent})")
    _run_alembic(restored_url, "downgrade", "-1")
    downgraded = _current_revision(restored_url)
    if downgraded == head:
        _fail(f"downgrade -1 left the database at head ({head}) - the downgrade was skipped")
    if downgraded != parent:
        _fail(f"downgrade -1 landed on {downgraded}, expected {parent}")
    print(f"      now at: {downgraded}")
    _assert_integrity(restored_url, expected, "after downgrade")

    print("[10/10] alembic upgrade heads again, back to head")
    _run_alembic(restored_url, "upgrade", "heads")
    upgraded = _current_revision(restored_url)
    if upgraded != head:
        _fail(f"re-upgrade landed on {upgraded}, expected {head}")
    print(f"      now at: {upgraded}")
    _assert_integrity(restored_url, expected, "after downgrade/upgrade")

    if not args.keep_databases:
        _drop_database(maintenance, source_name)
        _drop_database(maintenance, restored_name)
        shutil.rmtree(backup_dir, ignore_errors=True)
        print("\nScratch databases and backup artefacts removed.")
    else:
        print(f"\nKept for inspection: databases {source_name}, {restored_name}; backup artefacts in {backup_dir}.")

    print("\nGATE PASSED: pg_dump backup -> restore -> alembic downgrade -> upgrade round-trip verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

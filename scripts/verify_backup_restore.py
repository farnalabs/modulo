#!/usr/bin/env python3
"""FAR-19 gate: prove the pg_dump backup + Alembic upgrade/downgrade path.

This script is the automated evidence behind ``docs/operations/backup.md``.
It runs the real ``modulo backup`` / ``modulo restore`` CLI against a real
Postgres and proves the whole self-hosted disaster-recovery loop:

  1. reset two scratch databases on the target server (refusing a server that
     is not provably scratch unless ``--allow-role-reset`` is passed)
  2. provision the runtime roles the migration chain requires
  3. ``alembic upgrade heads`` from scratch, then assert the DB is at head
  4. seed representative data (org, account, membership, pipeline,
     snapshot, run) through the ORM
  5. create the LangGraph checkpoint tables exactly as app startup does, then
     seed one row in each of ``checkpoints`` / ``checkpoint_blobs`` /
     ``checkpoint_writes`` (an empty table would make the JSON export and
     restore path vacuous)
  6. ``modulo backup`` -> pg_dump + checkpoint JSON exports + manifest, then
     assert ALL six artefacts exist and ``database.sql`` is non-empty
  7. ``modulo restore`` that backup into the second, empty database
  8. assert the seeded rows AND the three seeded checkpoint rows survived the
     restore, then re-assert the runtime role posture for ``modulo_app``
     (NOBYPASSRLS + the expected grants) — the restore's own posture step
     swallows its failures, so the gate must check the end state itself, and
     the posture is deliberately broken before the restore so that check
     cannot pass vacuously
  9. ``alembic downgrade -1`` on the restored DB, assert it moved OFF head and
     the seeded rows are still there
 10. ``alembic upgrade heads`` again, assert it is back at head with the seeded
     rows still intact

Every step failure exits non-zero: no step is reported as having run unless
it ran, and the downgrade/upgrade steps assert the recorded revision actually
moved rather than trusting that the command was not skipped.

Usage
-----
Start a throwaway Postgres and run against it. Give the container a unique name
and let Docker assign the host port: a fixed name/port collides with any other
concurrent run, and a killed run leaves both held. This is the same
worktree-unique-port principle the local e2e harness applies::

    NAME=modulo-dr-gate-$(date +%Y%m%d-%H%M%S)
    docker run -d --rm --name "$NAME" \\
        -e POSTGRES_USER=modulo_test -e POSTGRES_PASSWORD=modulo_test \\
        -e POSTGRES_DB=modulo_test -p 0:5432 postgres:16-alpine
    PORT=$(docker port "$NAME" 5432/tcp | tail -1 | sed 's/.*://')

    export DATABASE_URL=postgresql+asyncpg://modulo_test:modulo_test@localhost:$PORT/modulo_test
    uv run --project backend python scripts/verify_backup_restore.py

Run it from the REPOSITORY ROOT — the script lives at ``scripts/`` and imports
the backend package from ``backend/``, so ``cd backend`` first is wrong, not
merely inconvenient.

Cluster-scoped roles
--------------------
This gate resets the runtime roles the migration chain requires
(``modulo_app``, ``modulo_migrate``, ``modulo_breakglass``) with
``DROP ROLE`` / ``CREATE ROLE``. Roles are CLUSTER-scoped, not database-scoped:
pointed at a shared server this rotates the role passwords of every deployment
living on it. The gate therefore refuses to proceed unless the database URL's
host is a loopback/private scratch host AND its database name ends in a scratch
suffix (``_test``, ``_src``, ``_restored``, ...), or ``--allow-role-reset`` is
passed. Only ever point it at a dedicated throwaway server.

``--database-url`` overrides ``$DATABASE_URL``. ``pg_dump`` and ``psql``
(major version >= the server's) must be on ``PATH``: ``modulo backup`` and
``modulo restore`` shell out to them.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
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

# Every artefact ``modulo backup`` must produce. All six are asserted after the
# backup so a silently-missing export fails the gate instead of leaving an
# untested restore step behind.
_BACKUP_ARTEFACTS = (
    "database.sql",
    "backup-info.json",
    "checkpoints.json",
    "checkpoint_blobs.json",
    "checkpoint_writes.json",
    "credentials_references.json",
)

# The runtime app role whose posture the restore must leave correct.
_APP_ROLE = "modulo_app"
# Password for the scratch app role (matches _ROLE_DDL below). Only ever set on
# a throwaway server the role guard has approved; not a real secret.
_APP_ROLE_PASSWORD = "apppass"

# Database-name suffixes that identify a provably scratch database. The role
# reset below is cluster-scoped, so a name alone is not proof — the guard pairs
# this with a loopback/private host requirement (and --allow-role-reset for
# everything else).
_SCRATCH_DB_SUFFIXES = ("_test", "_tests", "_src", "_restored", "_backup_src", "_scratch", "_gate")

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


def _db_name(url: str) -> str:
    """The database name exactly as written in the URL ("" when absent)."""
    return urlparse(url).path.lstrip("/").split("/")[0].split("?")[0]


def _base_db_name(url: str) -> str:
    return _db_name(url) or "modulo_test"


# -- Role-reset guard -------------------------------------------------------


def _is_scratch_host(host: str) -> bool:
    """True only for a loopback or private-range address.

    A hostname that is not an IP literal (``db``, a service name, a managed
    host) is NOT provably scratch — it could be a shared server — so it is
    refused and the operator must pass ``--allow-role-reset`` deliberately.
    """
    if host == "localhost":
        return True
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _assert_role_reset_allowed(database_url: str, allow_role_reset: bool) -> None:
    """Refuse a cluster-wide role reset against a server that may be shared.

    The gate runs ``DROP ROLE`` / ``CREATE ROLE`` for ``modulo_app``,
    ``modulo_migrate`` and ``modulo_breakglass`` with fixed passwords. Roles are
    CLUSTER-scoped: on a shared server that rotates the credentials of every
    deployment living there. The reset is only allowed when the target is
    provably scratch (loopback/private host AND a scratch database-name suffix)
    or when the operator explicitly opts in.
    """
    if allow_role_reset:
        print("      --allow-role-reset: cluster-scoped role reset explicitly permitted")
        return
    parsed = urlparse(database_url)
    host = parsed.hostname or ""
    name = _db_name(database_url)
    if _is_scratch_host(host) and name.endswith(_SCRATCH_DB_SUFFIXES):
        print(f"      role reset allowed: scratch target {host or '?'}/{name}")
        return
    _fail(
        "refusing to reset CLUSTER-scoped roles against a target that is not provably scratch. "
        f"Host {host or '<unset>'} must be loopback/private and database name "
        f"{name or '<unset>'} must end in one of {_SCRATCH_DB_SUFFIXES}. "
        "Roles belong to the whole Postgres server, so this script's DROP/CREATE ROLE statements "
        "would rotate the role passwords of every deployment on a shared server. "
        "Point the gate at a dedicated throwaway server, or pass --allow-role-reset to accept that."
    )


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


_CHECKPOINT_NAMESPACE = ""
_SEED_CHECKPOINT_ID = "ckpt-far-19-gate"
_SEED_TASK_ID = "task-far-19-gate"


def _seed_checkpoint_rows(database_url: str, expected: dict[str, uuid.UUID]) -> None:
    """Insert one row into each checkpoint table.

    Without a row the JSON exports are ``[]``, the restore helpers run only
    their ``TRUNCATE``, and every checkpoint assertion passes vacuously — the
    export/restore path would be untested while the gate stayed green.
    """
    thread_id = _thread_id(expected)
    org_id = str(expected["org"])
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO checkpoints "
            "(organisation_id, thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (%s, %s, %s, %s, NULL, 'json', %s::jsonb, %s::jsonb)",
            (
                org_id,
                thread_id,
                _CHECKPOINT_NAMESPACE,
                _SEED_CHECKPOINT_ID,
                json.dumps({"gate": "far-19", "step": 1}),
                json.dumps({"source": "far-19-gate"}),
            ),
        )
        conn.execute(
            "INSERT INTO checkpoint_blobs "
            "(organisation_id, thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (%s, %s, %s, %s, %s, 'json', %s)",
            (
                org_id,
                thread_id,
                _CHECKPOINT_NAMESPACE,
                "messages",
                "1",
                json.dumps({"gate": "far-19", "blob": True}).encode(),
            ),
        )
        conn.execute(
            "INSERT INTO checkpoint_writes "
            "(organisation_id, thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'json', %s)",
            (
                org_id,
                thread_id,
                _CHECKPOINT_NAMESPACE,
                _SEED_CHECKPOINT_ID,
                _SEED_TASK_ID,
                0,
                "messages",
                json.dumps({"gate": "far-19", "write": True}).encode(),
            ),
        )
    print(f"      seeded one row in each of {', '.join(_CHECKPOINT_TABLES)}")


def _thread_id(expected: dict[str, uuid.UUID]) -> str:
    """The seeded run's LangGraph thread, used as the checkpoint thread."""
    return f"thread-{expected['run']}"


def _assert_checkpoint_rows(database_url: str, expected: dict[str, uuid.UUID], stage: str) -> None:
    """Assert the three seeded checkpoint rows survived with their values."""
    thread_id = _thread_id(expected)
    org_id = str(expected["org"])
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT checkpoint->>'gate', metadata->>'source' FROM checkpoints "
            "WHERE organisation_id = %s AND thread_id = %s AND checkpoint_id = %s",
            (org_id, thread_id, _SEED_CHECKPOINT_ID),
        )
        checkpoint = cur.fetchone()
        cur.execute(
            "SELECT type, convert_from(blob, 'UTF8') FROM checkpoint_blobs "
            "WHERE organisation_id = %s AND thread_id = %s AND channel = %s",
            (org_id, thread_id, "messages"),
        )
        blob = cur.fetchone()
        cur.execute(
            "SELECT idx, convert_from(blob, 'UTF8') FROM checkpoint_writes "
            "WHERE organisation_id = %s AND thread_id = %s AND task_id = %s",
            (org_id, thread_id, _SEED_TASK_ID),
        )
        write = cur.fetchone()

    expected_blob = json.dumps({"gate": "far-19", "blob": True})
    expected_write = json.dumps({"gate": "far-19", "write": True})

    if checkpoint is None:
        _fail(f"{stage}: seeded checkpoint row {_SEED_CHECKPOINT_ID} is missing")
    if checkpoint[0] != "far-19" or checkpoint[1] != "far-19-gate":
        _fail(f"{stage}: checkpoint values changed to {checkpoint!r}")
    if blob is None:
        _fail(f"{stage}: seeded checkpoint_blobs row is missing")
    if blob[0] != "json" or blob[1] != expected_blob:
        _fail(f"{stage}: checkpoint_blobs values changed to {blob!r}")
    if write is None:
        _fail(f"{stage}: seeded checkpoint_writes row is missing")
    if write[0] != 0 or write[1] != expected_write:
        _fail(f"{stage}: checkpoint_writes values changed to {write!r}")
    print(f"      {stage}: checkpoint rows intact")


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
                    password_hash="!",  # restore fixture sentinel — not a credential
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


def _assert_backup_artefacts(backup_dir: Path) -> None:
    """Assert the backup wrote every artefact and that the dump is non-empty.

    Existence alone is not enough for ``database.sql``: a zero-byte dump would
    restore nothing while every later assertion still passed against an empty
    database. A missing JSON export is equally fatal — the restore would skip
    that table with a warning and the integrity checks would never look at it.
    """
    missing = [name for name in _BACKUP_ARTEFACTS if not (backup_dir / name).is_file()]
    if missing:
        _fail(f"backup wrote no {', '.join(missing)} in {backup_dir}")
    for name in _BACKUP_ARTEFACTS:
        print(f"      {name}: {(backup_dir / name).stat().st_size} bytes")
    dump_size = (backup_dir / "database.sql").stat().st_size
    if dump_size <= 0:
        _fail(f"backup produced an empty database.sql in {backup_dir}")


# -- Restore posture --------------------------------------------------------


def _url_with_user(url: str, user: str, password: str) -> str:
    """Rebuild ``url`` with a different user/password, same host/port/database."""
    parsed = urlparse(url)
    netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _break_app_role_posture(database_url: str) -> None:
    """Give ``modulo_app`` BYPASSRLS *before* the restore.

    Roles are CLUSTER-scoped and are not in the dump, so a restore cannot
    repair them. Breaking the posture first is what stops the post-restore
    assertion from being vacuous: it can only pass if the promoted posture path
    actually resets ``rolbypassrls``, rather than observing a value the
    provisioning step happened to leave correct.
    """
    from psycopg import sql

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("ALTER ROLE {} BYPASSRLS").format(sql.Identifier(_APP_ROLE)))
    print(f"      broke {_APP_ROLE}'s posture (BYPASSRLS) before the restore, so the check below can bite")


def _apply_runtime_role_posture(database_url: str) -> None:
    """Re-run the promoted bootstrap path with the REAL runtime app role.

    ``modulo restore`` derives its app role from ``--db-url``. In this gate
    that user is the scratch superuser psql needs to replay the dump, so the
    restore's posture step configures the superuser rather than ``modulo_app``.
    Running the same promoted path once with ``modulo_app`` as the app URL is
    what makes the grant assertions below meaningful — and it fails loudly if
    ``bootstrap_roles`` itself stops applying the posture.
    """
    import asyncio

    from modulo.db.bootstrap_role import bootstrap_roles

    app_url = _url_with_user(database_url, _APP_ROLE, _APP_ROLE_PASSWORD)
    asyncio.run(bootstrap_roles(database_url, app_url))
    print(f"      re-applied the promoted bootstrap posture for {_APP_ROLE}")


def _assert_restore_posture(database_url: str) -> None:
    """Observe ``modulo_app``'s role posture and grants directly after restore.

    ``modulo restore``'s own posture step (``_ensure_restore_posture``) catches
    every exception, logs a warning and returns — a restore that left
    ``modulo_app`` able to bypass RLS, or without the grants the app needs,
    still exits 0. The gate therefore checks the end state itself: a
    ``--clean --no-owner --no-acl`` dump strips every table ACL, so the grants
    below are re-created by the posture path and are the real signal.
    """
    from modulo.db.bootstrap_role import ACCOUNTS_WRITABLE_COLUMNS

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (_APP_ROLE,))
        role = cur.fetchone()
        cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'modulo_system'")
        system_role = cur.fetchone()
        cur.execute("SELECT has_schema_privilege(%s, 'public', 'USAGE')", (_APP_ROLE,))
        schema_usage = cur.fetchone()[0]
        cur.execute(
            "SELECT has_table_privilege(%s, 'public.pipelines', 'SELECT'), "
            "has_table_privilege(%s, 'public.pipelines', 'INSERT'), "
            "has_table_privilege(%s, 'public.pipelines', 'UPDATE'), "
            "has_table_privilege(%s, 'public.pipelines', 'DELETE')",
            (_APP_ROLE, _APP_ROLE, _APP_ROLE, _APP_ROLE),
        )
        table_privileges = cur.fetchone()
        cur.execute(
            "SELECT grantee FROM information_schema.role_table_grants "
            "WHERE table_schema = 'public' AND table_name = 'accounts' "
            "AND privilege_type = 'UPDATE' AND grantee IN (%s, 'PUBLIC')",
            (_APP_ROLE,),
        )
        table_level_updates = sorted(row[0] for row in cur.fetchall())
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'accounts'"
        )
        account_columns = {row[0] for row in cur.fetchall()}
        granted_updates: set[str] = set()
        for column in sorted(account_columns):
            cur.execute(
                "SELECT has_column_privilege(%s, 'public.accounts', %s, 'UPDATE')",
                (_APP_ROLE, column),
            )
            if cur.fetchone()[0]:
                granted_updates.add(column)
        cur.execute(
            "SELECT b.rolname FROM pg_auth_members m "
            "JOIN pg_roles a ON a.oid = m.member JOIN pg_roles b ON b.oid = m.roleid "
            "WHERE a.rolname = %s AND b.rolname IN ('modulo_migrate', 'modulo_breakglass')",
            (_APP_ROLE,),
        )
        memberships = sorted(row[0] for row in cur.fetchall())

    if role is None:
        _fail(f"after restore: runtime role {_APP_ROLE} does not exist")
    if role[0]:
        _fail(f"after restore: {_APP_ROLE} is a SUPERUSER")
    if role[1]:
        _fail(f"after restore: {_APP_ROLE} has BYPASSRLS - tenant isolation relies on RLS policies")
    if system_role is None or not system_role[0]:
        _fail("after restore: modulo_system is missing or lacks BYPASSRLS - system crons would see zero rows")
    if not schema_usage:
        _fail(f"after restore: {_APP_ROLE} has no USAGE on schema public")
    if not all(table_privileges):
        _fail(
            f"after restore: {_APP_ROLE} is missing DML grants on public.pipelines "
            f"(SELECT/INSERT/UPDATE/DELETE = {table_privileges})"
        )
    if table_level_updates:
        _fail(f"after restore: accounts has a table-level UPDATE grant for {table_level_updates}")
    expected_updates = set(ACCOUNTS_WRITABLE_COLUMNS) & account_columns
    if granted_updates != expected_updates:
        _fail(
            "after restore: accounts UPDATE grant drift "
            f"(granted={sorted(granted_updates)} allow_list={sorted(expected_updates)})"
        )
    if memberships:
        _fail(f"after restore: {_APP_ROLE} is a member of {memberships}")
    print(f"      after restore: {_APP_ROLE} posture and grants intact")


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
    parser.add_argument(
        "--allow-role-reset",
        action="store_true",
        help=(
            "permit the CLUSTER-scoped DROP/CREATE ROLE reset on a server that is not provably scratch. "
            "Roles belong to the whole Postgres server, so this rotates the role passwords of every "
            "deployment on a shared server. Only use against a dedicated throwaway server."
        ),
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        _fail("--database-url (or $DATABASE_URL) is required")
    _check_client_tools()

    database_url = _plain_url(args.database_url)
    # Before ANY connection: the gate resets cluster-scoped roles in step 2.
    _assert_role_reset_allowed(database_url, args.allow_role_reset)
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
    _seed_checkpoint_rows(source_url, expected)
    _assert_checkpoint_rows(source_url, expected, "after seeding")

    print(f"[6/10] Running 'modulo backup' on '{source_name}'")
    _ensure_settings_env()
    backup_dir = Path(tempfile.mkdtemp(prefix="modulo-dr-gate-"))
    _invoke_cli(["backup", "--db-url", source_url, "--output-dir", str(backup_dir)])
    _assert_backup_artefacts(backup_dir)

    print(f"[7/10] Running 'modulo restore' into empty database '{restored_name}'")
    # Roles are cluster-scoped and absent from the dump, so break modulo_app's
    # posture here: step 8's assertion must prove the posture path repairs it.
    _break_app_role_posture(database_url)
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
    _assert_checkpoint_rows(restored_url, expected, "after restore")
    # `modulo restore` derives its app role from --db-url, which here is the
    # scratch superuser, so re-run the promoted first-boot posture path with
    # the REAL runtime role before checking it.
    _apply_runtime_role_posture(restored_url)
    _assert_restore_posture(restored_url)

    print(f"[9/10] alembic downgrade -1 on the restored database (expect {parent})")
    _run_alembic(restored_url, "downgrade", "-1")
    downgraded = _current_revision(restored_url)
    if downgraded == head:
        _fail(f"downgrade -1 left the database at head ({head}) - the downgrade was skipped")
    if downgraded != parent:
        _fail(f"downgrade -1 landed on {downgraded}, expected {parent}")
    print(f"      now at: {downgraded}")
    _assert_integrity(restored_url, expected, "after downgrade")
    _assert_checkpoint_rows(restored_url, expected, "after downgrade")

    print("[10/10] alembic upgrade heads again, back to head")
    _run_alembic(restored_url, "upgrade", "heads")
    upgraded = _current_revision(restored_url)
    if upgraded != head:
        _fail(f"re-upgrade landed on {upgraded}, expected {head}")
    print(f"      now at: {upgraded}")
    _assert_integrity(restored_url, expected, "after downgrade/upgrade")
    _assert_checkpoint_rows(restored_url, expected, "after downgrade/upgrade")

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

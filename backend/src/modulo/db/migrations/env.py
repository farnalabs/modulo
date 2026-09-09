import logging
import os
import time
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from logging.config import fileConfig
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import sqlalchemy as sa
from alembic import context
from alembic.config import Config
from alembic.runtime.migration import MigrationContext, MigrationInfo
from alembic.script import Script, ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.pool import NullPool

from modulo.db.models import Base

_log = logging.getLogger(__name__)

_DRIVER_POSTGRES_SYNC = "postgresql+psycopg"
_DRIVER_MYSQL_SYNC = "mysql+pymysql"
_CONFIG_KEY_SQLALCHEMY_URL = "sqlalchemy.url"

_DRIVER_MAP: dict[str, str] = {
    "postgresql+asyncpg": _DRIVER_POSTGRES_SYNC,
    "sqlite+aiosqlite": "sqlite",
    "mysql+aiomysql": _DRIVER_MYSQL_SYNC,
    "mysql+asyncmy": _DRIVER_MYSQL_SYNC,
    "postgresql": _DRIVER_POSTGRES_SYNC,
    "postgres": _DRIVER_POSTGRES_SYNC,
    "mysql": _DRIVER_MYSQL_SYNC,
}


def _to_sync_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme in _DRIVER_MAP:
        parsed = parsed._replace(scheme=_DRIVER_MAP[parsed.scheme])
    qs = parse_qs(parsed.query, keep_blank_values=True)
    if qs.get("sslmode") == ["disable"]:
        del qs["sslmode"]
    if qs.get("ssl") == ["disable"]:
        del qs["ssl"]
    new_query = urlencode(qs, doseq=True) if qs else ""
    return urlunparse(parsed._replace(query=new_query))


# Migration advisory lock — the SAME key the app lifespan runner uses
# (modulo.api.main._MIGRATION_LOCK_KEY). Every migration path goes through
# env.py's run_migrations_online(): the entrypoint's raw `alembic upgrade heads`
# (web AND worker process groups booting concurrently on a fresh deploy) and
# the app's lifespan run. Holding this lock serialises them so two raw runs can
# never race a half-migrated schema. Advisory locks are session-scoped: held on
# a dedicated connection for the whole migration run and released afterwards.
_MIGRATION_LOCK_KEY = (72001, 1)
_MIGRATION_LOCK_POLL_ATTEMPTS = 240
_MIGRATION_LOCK_POLL_INTERVAL = 1.0

# True while the app lifespan (main.py) already holds the migration lock on its
# own connection. env.py must NOT re-acquire the same key on a different
# session in that case — advisory locks are per-session, so a second session
# would block on itself. Standalone `alembic upgrade heads` (entrypoint.sh)
# never sets the flag and therefore always takes the lock.
_lock_held_by_caller = False


def set_lock_held_by_caller(held: bool) -> None:
    """Set/clear whether the calling process already holds the migration lock."""
    global _lock_held_by_caller
    _lock_held_by_caller = held


@contextmanager
def _migration_advisory_lock(engine: Engine, url: str) -> Iterator[None]:
    """Hold the migration advisory lock (Postgres only) for the migration run.

    Uses ``pg_try_advisory_lock`` in a polling loop (per AGENTS.md: a bare
    ``pg_advisory_lock`` under a client timeout races server-side acquisition
    against the client). The lock is session-scoped and held on a dedicated
    connection so it survives the migration transaction. Non-Postgres backends
    (SQLite/MariaDB — dev-only) have no advisory locks and are skipped.
    """
    if not url.startswith("postgresql") or _lock_held_by_caller:
        yield
        return

    with engine.connect() as lock_conn:
        acquired = False
        for _ in range(_MIGRATION_LOCK_POLL_ATTEMPTS):
            result = lock_conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:k1, :k2)"),
                {"k1": _MIGRATION_LOCK_KEY[0], "k2": _MIGRATION_LOCK_KEY[1]},
            )
            if bool(result.scalar_one()):
                acquired = True
                break
            time.sleep(_MIGRATION_LOCK_POLL_INTERVAL)
        if not acquired:
            raise RuntimeError("Timed out waiting for the migration advisory lock")
        try:
            yield
        finally:
            lock_conn.execute(
                sa.text("SELECT pg_advisory_unlock(:k1, :k2)"),
                {"k1": _MIGRATION_LOCK_KEY[0], "k2": _MIGRATION_LOCK_KEY[1]},
            )


target_metadata = Base.metadata

# Module-level Alembic setup — only safe when context is properly configured
# (i.e. when env.py is executed via command.upgrade, not imported as a module).
try:
    config: Config | None = context.config
except AttributeError:
    config = None
if config is not None:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)

    # Allow DATABASE_ADMIN_URL env var to override the alembic.ini connection string.
    # Migrations connect with the owner role (not modulo_app runtime role) to
    # run DDL without RLS interference. Falls back to DATABASE_URL for compat.
    _db_url = os.environ.get("DATABASE_ADMIN_URL") or os.environ.get("DATABASE_URL")
    if _db_url:
        config.set_main_option(_CONFIG_KEY_SQLALCHEMY_URL, _to_sync_url(_db_url))


def _detect_backend(url: str) -> str:
    if url.startswith("postgresql"):
        return "postgresql"
    if url.startswith("mysql"):
        return "mysql"
    if url.startswith("sqlite"):
        return "sqlite"
    return "unknown"


def run_migrations_offline() -> None:
    if config is None:
        raise RuntimeError("Alembic config is unavailable")
    url = config.get_main_option(_CONFIG_KEY_SQLALCHEMY_URL) or ""
    backend = _detect_backend(url)
    _log.info("Running migrations offline for %s backend", backend)

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=backend == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    backend = _detect_backend(str(connection.engine.url))
    _log.info("Running migrations for %s backend", backend)

    # Alembic creates alembic_version with VARCHAR(32) by default, but
    # post-squash branch migration IDs exceed 32 chars (e.g.
    # 0006_post_squash_pipeline_archived_at is 44 chars).  Widen the column
    # before any migration runs so the version UPDATE never truncates.  On a
    # fresh database the table does not exist yet, so Alembic would create it
    # as VARCHAR(32) and the first UPDATE would truncate — pre-create it with
    # the wide type in that case.
    if backend == "postgresql":
        from sqlalchemy import inspect as sa_inspect

        if sa_inspect(connection).has_table("alembic_version"):
            # Widen ONLY when the column is actually narrow: a no-op ALTER on
            # an already-wide column opens a pointless side connection on
            # every migration run, and during rehearsal mode (FAR-717) any
            # side connection is refused outright (its effects would persist
            # outside the rehearsal transaction). NULL means unbounded (TEXT).
            max_len = connection.execute(
                sa.text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'alembic_version' "
                    "AND column_name = 'version_num'"
                )
            ).scalar()
            if max_len is None or max_len >= 255:
                _log.debug(
                    "alembic_version.version_num already wide enough (max_len=%s) -- skipping widening",
                    max_len,
                )
            else:
                _refuse_rehearsal_escape_hatch("the pre-flight alembic_version widening")
                # Use a separate connection so a failure here (e.g. non-owner role)
                # does not abort the outer migration transaction.
                try:
                    with connection.engine.begin() as alt_conn:
                        alt_conn.execute(
                            sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)")
                        )
                        _log.info("Widened alembic_version.version_num to VARCHAR(255)")
                except Exception:
                    _log.warning(
                        "Could not widen alembic_version (non-owner); bootstrap_db.py already created it with VARCHAR(255)"
                    )
        else:
            connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)"))
            _log.info("Pre-created alembic_version.version_num as VARCHAR(255)")

    if _rehearsal_mode:
        # Track each step as alembic applies it: names the revision in
        # escape-hatch refusals and feeds the rehearsal report. Only set in
        # rehearsal mode so the real migration path is untouched.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=backend == "sqlite",
            on_version_apply=_rehearsal_step_applied,
        )
    else:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=backend == "sqlite",
        )
    with context.begin_transaction():
        context.run_migrations()


def _db_is_at_head(engine: Engine) -> bool:
    """Return True when the DB's ``alembic_version`` already equals the head.

    Boot fast-path: multiple machines boot simultaneously on a fresh deploy and
    every process group ran ``alembic upgrade heads`` serialised by the
    advisory lock — machines that did not win the lock waited up to
    ``_MIGRATION_LOCK_POLL_ATTEMPTS`` seconds before FATALing, even when the
    schema was already up to date. When the DB is already at head there is no
    work to do, so the advisory lock acquisition and the alembic run are pure
    contention and are skipped entirely.

    Fail-safe: any failure (missing table, multiple heads, connection error)
    returns False so the caller proceeds through the normal lock + upgrade path.
    """
    try:
        head = ScriptDirectory.from_config(config).get_current_head() if config is not None else None
    except Exception:
        return False
    if not head:
        return False
    try:
        with engine.connect() as conn:
            result = conn.execute(sa.text("SELECT version_num FROM alembic_version"))
            versions = {row[0] for row in result.fetchall()}
    except Exception:
        return False
    return versions == {head}


def _invocation_is_upgrade() -> bool:
    """Return True when the current alembic invocation is an UPGRADE.

    The boot fast-path (:func:`run_migrations_online`) must only skip when there
    is genuinely nothing to do — the DB is already at head AND the invocation
    moves FORWARD. ``alembic downgrade`` must ALWAYS run: it exists to move the
    DB AWAY from head, so skipping it while at head made downgrades a silent
    no-op (dist/runtime-ops fix). The app lifespan calls
    ``command.upgrade(config, 'heads')`` programmatically, where ``cmd_opts``
    is absent — the direction is upgrade by construction, so the default is
    True.

    The CLI stores parsed options on ``config.cmd_opts``, and the sub-command
    lives in ``cmd_opts.cmd`` — a ``(fn, positional, kwarg)`` tuple whose first
    element is the command function, so the invocation name is that function's
    ``__name__`` (``"upgrade"`` / ``"downgrade"``). There is no
    ``cmd_opts.command`` attribute on the CLI namespace: reading it made every
    CLI invocation (including downgrades) classify as an upgrade-at-head and
    fast-path-skip into a permanent silent no-op. Tests may inject either shape
    explicitly (``SimpleNamespace(command="downgrade")``), so both are honoured.
    """
    opts = getattr(config, "cmd_opts", None) if config is not None else None
    command_name = getattr(opts, "command", None)
    if isinstance(command_name, str):
        return command_name != "downgrade"
    fn = getattr(opts, "cmd", None)
    if isinstance(fn, tuple):
        fn = fn[0]
    return getattr(fn, "__name__", None) != "downgrade"


# ---------------------------------------------------------------------------
# Rehearsal mode (FAR-717)
# ---------------------------------------------------------------------------
# ALEMBIC_REHEARSAL=1 executes the REAL upgrade chain against the REAL
# database inside ONE transaction and ALWAYS rolls back — proving migrations
# against real data with zero persistence. Born from the 2026-09-08 deploy
# outage (migration 0191 failed against populated databases while fresh-DB CI
# passed vacuously), where the only reliable diagnostic was executing the
# failing statements in a transaction and rolling back.

_REHEARSAL_ENV_VAR = "ALEMBIC_REHEARSAL"

_rehearsal_mode = False
#: Set once the rehearsal's main connection is checked out; any FURTHER
#: checkout from the rehearsal engine is an escape-hatch side connection.
_rehearsal_main_conn_acquired = False
#: Revision of the last COMPLETED migration step (on_version_apply fires
#: after a step's operations are applied, before its version-table update).
_rehearsal_current_revision: str | None = None
#: Revision ids of the steps completed during this rehearsal, in order.
_rehearsal_applied_steps: list[str] = []
#: Revision ids of the planned chain, in upgrade order.
_rehearsal_planned_steps: list[str] = []


def _rehearsal_requested() -> bool:
    return os.environ.get(_REHEARSAL_ENV_VAR, "") == "1"


def _rehearsal_print(message: str) -> None:
    """Print unconditionally to stdout.

    The rehearsal report is a machine-log diagnostic (release.sh output is
    the only thing that survives a failed pre-deploy machine), so it must
    not depend on alembic.ini's logging configuration. Logging alone is
    filtered/reformatted and can be lost entirely.
    """
    print(message, flush=True)  # noqa: T201 - diagnostic report, must reach stdout


def _rehearsal_escape_hatch_message(culprit: str) -> str:
    return (
        f"{culprit} uses the autocommit escape hatch and CANNOT be rehearsed "
        "(its effects would persist) - exclude it from rehearsal or run it for real"
    )


def _refuse_rehearsal_escape_hatch(culprit: str | None = None) -> None:
    """Refuse the autocommit escape hatch during a rehearsal.

    The escape hatch opens a SECOND connection whose transaction commits
    independently of the rehearsal's single transaction - its effects would
    persist for real. Called explicitly at the known escape-hatch site (the
    pre-flight ``alembic_version`` widening in :func:`do_run_migrations`);
    the rehearsal engine's checkout listener
    (:func:`_refuse_rehearsal_side_connection`) is the generic backstop that
    catches any other side connection, including one opened by a migration.
    """
    if not (_rehearsal_mode and _rehearsal_main_conn_acquired):
        return
    raise RuntimeError(_rehearsal_escape_hatch_message(culprit or _rehearsal_culprit_revision()))


def _refuse_rehearsal_side_connection(
    dbapi_connection: object,
    connection_record: object,
    connection_proxy: object,
) -> None:
    """Pool ``checkout`` listener on the rehearsal engine.

    The rehearsal's byte-identical guarantee depends on EVERY statement
    running inside the single rehearsal transaction. A second connection
    checked out from the rehearsal engine (a migration opening
    ``connection.engine.begin()`` directly - the autocommit escape hatch) would
    commit independently and persist for real. Refuse it immediately.
    """
    if not (_rehearsal_mode and _rehearsal_main_conn_acquired):
        return
    culprit = _rehearsal_culprit_revision()
    raise RuntimeError(_rehearsal_escape_hatch_message(culprit))


def _rehearsal_culprit_revision() -> str:
    """Best-known identity of the step in flight when an escape hatch fires.

    ``on_version_apply`` fires AFTER a step's operations complete, so while
    step N executes the tracker still names N-1 - the planned chain supplies
    the step that is actually executing.
    """
    idx = len(_rehearsal_applied_steps)
    if idx < len(_rehearsal_planned_steps):
        return f"migration {_rehearsal_planned_steps[idx]}"
    if _rehearsal_applied_steps:
        return f"migration {_rehearsal_applied_steps[-1]} (final planned step)"
    return "the pre-flight alembic_version widening (before any migration step)"


def _rehearsal_failure_step() -> str:
    """Identify the step the rehearsal failed on.

    ``on_version_apply`` fires after a step COMPLETES, so the tracker names
    the last completed step and the failing step is the next one in the
    planned chain (or the last one when the failure was in its finalisation).
    """
    idx = len(_rehearsal_applied_steps)
    if idx < len(_rehearsal_planned_steps):
        return _rehearsal_planned_steps[idx]
    if _rehearsal_applied_steps:
        return f"{_rehearsal_applied_steps[-1]} (finalisation)"
    return "(before any migration step applied)"


def _rehearsal_step_applied(
    ctx: MigrationContext,
    step: MigrationInfo,
    heads: Collection[Any],
    run_args: Mapping[str, Any],
) -> None:
    """``on_version_apply`` callback - record each revision alembic applies.

    Runs after a step's operations are applied but before its transaction is
    finalised, so this marks the last COMPLETED step.
    """
    global _rehearsal_current_revision
    # Defensive getattr: alembic always passes a MigrationInfo, but a malformed
    # step object must not crash the reporting path mid-migration.
    up_revision = getattr(step, "up_revision_id", None)
    if up_revision is not None:
        _rehearsal_current_revision = str(up_revision)
        _rehearsal_applied_steps.append(str(up_revision))


def _rehearsal_plan(engine: Engine) -> tuple[list[Script], str | None, str | None]:
    """Walk alembic's OWN revision graph from the DB's current revision to head.

    Uses :meth:`ScriptDirectory.iterate_revisions` - the same machinery
    ``alembic upgrade`` uses to compute its steps - so the planned list can
    never drift from what the run would actually apply.
    """
    cfg = config
    if cfg is None:
        raise RuntimeError("Alembic env config unavailable")
    script = ScriptDirectory.from_config(cfg)
    script_head = script.get_current_head()
    with engine.connect() as plan_conn:
        db_current = MigrationContext.configure(plan_conn).get_current_revision()
    if db_current == script_head:
        return [], db_current, script_head
    planned = list(script.iterate_revisions(script_head, db_current))
    return planned, db_current, script_head


def _run_migrations_rehearsal(engine: Engine, url: str) -> None:
    """Execute the real upgrade chain in ONE transaction, always rolling back.

    The database must be byte-identical after a rehearsal: the single
    transaction carries every DDL/DML change AND the ``alembic_version`` bump,
    and the always-rollback discards all of it. The advisory lock is still
    taken so a rehearsal cannot race a real migrate. ``transaction_per_migration``
    is never set and nothing commits mid-run - a whole-run single transaction
    is mandatory in rehearsal mode.
    """
    global \
        _rehearsal_mode, \
        _rehearsal_main_conn_acquired, \
        _rehearsal_current_revision, \
        _rehearsal_applied_steps, \
        _rehearsal_planned_steps

    cfg = config
    if cfg is None:
        raise RuntimeError("Alembic env config unavailable")
    _rehearsal_mode = True
    _rehearsal_main_conn_acquired = False
    _rehearsal_current_revision = None
    _rehearsal_applied_steps = []
    _rehearsal_planned_steps = []
    started = time.monotonic()
    # Generic escape-hatch backstop: any side connection checked out from the
    # rehearsal engine after the main connection is refused (see
    # _refuse_rehearsal_side_connection). The engine is disposed by the caller,
    # so the listener dies with it.
    event.listen(engine, "checkout", _refuse_rehearsal_side_connection)
    try:
        with _migration_advisory_lock(engine, url):
            # Plan FIRST (own short-lived connection) so the report shows the
            # full chain even when a later step fails.
            planned, db_current, script_head = _rehearsal_plan(engine)
            _rehearsal_planned_steps = [step.revision for step in planned]
            _rehearsal_print("=== MIGRATION REHEARSAL (ALEMBIC_REHEARSAL=1) ===")
            _rehearsal_print(f"DB current revision: {db_current or '(none - fresh database)'}")
            _rehearsal_print(f"Script head:         {script_head}")
            _rehearsal_print(f"Migrations that WOULD apply ({len(planned)}):")
            for step in planned:
                _rehearsal_print(f"  - {step.revision}  {step.doc}")
            if not planned:
                _rehearsal_print("  (none)")
            _rehearsal_print("--- executing the real chain in ONE transaction (always rolled back) ---")

            connection = engine.connect()
            try:
                _rehearsal_main_conn_acquired = True
                trans = connection.begin()
                try:
                    do_run_migrations(connection)
                finally:
                    # ALWAYS roll back - success or failure. The version bump
                    # rolls back with every DDL/DML change.
                    trans.rollback()
            finally:
                connection.close()

        duration = time.monotonic() - started
        _rehearsal_print(f"Rehearsal applied {len(_rehearsal_applied_steps)} migration step(s) in {duration:.1f}s.")
        _rehearsal_print("=== REHEARSAL ONLY - ALL CHANGES ROLLED BACK ===")
    except Exception as exc:
        duration = time.monotonic() - started
        _rehearsal_print("=== REHEARSAL FAILED ===")
        _rehearsal_print(f"Failing step: {_rehearsal_failure_step()}")
        _rehearsal_print(f"Exception: {type(exc).__name__}: {exc}")
        _rehearsal_print(f"Steps completed before failure: {len(_rehearsal_applied_steps)} after {duration:.1f}s")
        _rehearsal_print("REHEARSAL ONLY - ALL CHANGES ROLLED BACK")
        raise
    finally:
        _rehearsal_mode = False
        _rehearsal_main_conn_acquired = False


def run_migrations_online() -> None:
    """Run migrations via a sync engine — no event loop needed.

    env.py runs in a thread pool (asyncio.to_thread), so a sync engine
    avoids all event-loop conflicts with the main async context.

    Serialises against the app lifespan's migration runner and other raw
    `alembic upgrade heads` runs via the shared advisory lock, so the web and
    worker entrypoint migrations that both fire on a fresh deploy cannot race.

    Fast-path: when the DB is already at the head revision AND the invocation
    is an upgrade (never a downgrade), skip the advisory lock and the alembic
    run entirely so boot is instant and machines never contend for the lock.

    Rehearsal mode (ALEMBIC_REHEARSAL=1): the same online path executes the
    real chain inside one transaction and always rolls back (see
    :func:`_run_migrations_rehearsal`). The at-head fast-path still applies —
    with no pending migrations there is nothing to rehearse.
    """
    if config is None:
        raise RuntimeError("Alembic env config unavailable")
    url = config.get_main_option(_CONFIG_KEY_SQLALCHEMY_URL) or ""
    sync_url = _to_sync_url(url)

    engine = create_engine(sync_url, poolclass=NullPool)
    try:
        if _invocation_is_upgrade() and _db_is_at_head(engine):
            _log.info("startup.migrations_already_at_head -- skipping migration run")
            if _rehearsal_requested():
                _rehearsal_print(
                    "=== MIGRATION REHEARSAL (ALEMBIC_REHEARSAL=1): DB already at head - nothing to rehearse ==="
                )
            return
        if _rehearsal_requested():
            _run_migrations_rehearsal(engine, url)
            return
        with _migration_advisory_lock(engine, url), engine.begin() as connection:
            do_run_migrations(connection)
    finally:
        engine.dispose()


if config is not None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()

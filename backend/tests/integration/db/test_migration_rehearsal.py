"""Integration tests for the alembic migration REHEARSAL mode (FAR-717).

Rehearsal mode (``ALEMBIC_REHEARSAL=1``) executes the REAL upgrade chain
against a REAL database inside ONE transaction and ALWAYS rolls back —
proving migrations against real data with zero persistence. Born from the
2026-09-08 deploy outage (migration 0191 failed against populated databases
while fresh-DB CI passed vacuously), where the only reliable diagnostic was
executing the failing statements in a transaction and rolling back.

Runs against its own testcontainer Postgres (built from scratch, mirroring
``test_migration_0194_uuid_pk_server_defaults.py``'s fresh-migration-db
pattern) and proves:

  * a SUCCESSFUL rehearsal leaves the database byte-identical: the
    ``alembic_version`` value is unchanged and every public table keeps its
    pre-rehearsal row count (the version bump rolls back with everything);
  * the rehearsal report (plan, banner, rollback notice) reaches stdout;
  * a subsequent REAL upgrade of the same chain applies cleanly and keeps
    seeded data;
  * a rehearsal that FAILS mid-run rolls back probe DDL executed before the
    failure, reports the failing step, and re-raises (non-zero exit for
    callers);
  * the autocommit escape hatch is REFUSED during rehearsal: when the
    legacy-narrow ``alembic_version`` forces the pre-flight widening to open
    its side connection, rehearsal mode raises immediately (its effects
    would persist outside the rehearsal transaction) and nothing persists.

The test builds its own container rather than reusing the shared session
DB: the byte-identical guarantee must hold on a freshly migrated chain, not
only on whatever state the shared DB happens to be in.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from testcontainers.community.postgres import PostgresContainer

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

#: The rehearsal starts from the real chain applied up to this revision and
#: covers every later migration (0195..head) — a meaty, evolving rehearsal,
#: matching what a deploy rehearses (prod at revision N, image at N+k).
_INTERMEDIATE = "0194_uuid_pk_server_defaults"

_REHEARSAL_PROBE_TABLE = "far717_rehearsal_rollback_probe"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _with_credentials(database_url: str, user: str, password: str) -> str:
    from urllib.parse import quote

    prefix, _, rest = database_url.partition("://")
    host_part, _, db = rest.partition("/")
    host = host_part.split("@")[-1]
    return f"{prefix}://{quote(user)}:{quote(password)}@{host}/{db}"


def _asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def _sync_url(asyncpg_url: str) -> str:
    return asyncpg_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _script_head(db_url: str) -> str:
    script = ScriptDirectory.from_config(_alembic_config(db_url))
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("migration chain has no head revision")
    return head


def _table_counts(sync_db_url: str) -> dict[str, int]:
    """Exact row count of every public base table (schema fingerprint)."""
    engine = sa.create_engine(sync_db_url)
    try:
        with engine.connect() as conn:
            names = (
                conn.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
                .scalars()
                .all()
            )
            counts: dict[str, int] = {}
            for name in names:
                counts[name] = int(
                    conn.execute(
                        sa.text(f'SELECT count(*) FROM "{name.replace(chr(34), chr(34) * 2)}"')  # noqa: S608 - identifier from information_schema, quote-escaped
                    ).scalar_one()
                )
            return counts
    finally:
        engine.dispose()


def _alembic_version(sync_db_url: str) -> str | None:
    engine = sa.create_engine(sync_db_url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def _version_column_max_len(sync_db_url: str) -> int | None:
    engine = sa.create_engine(sync_db_url)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    sa.text(
                        "SELECT character_maximum_length FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'alembic_version' "
                        "AND column_name = 'version_num'"
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _seed_representative_data(sync_db_url: str) -> None:
    """Committed rows in two long-lived tables (the data the chain must not lose).

    ``system_config`` rides the 0194 ``gen_random_uuid()`` PK default (no id
    supplied) — exactly the raw-INSERT shape that broke migration 0191.
    """
    engine = sa.create_engine(sync_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "name": "FAR-717 Rehearsal Org",
                    "slug": "far-717-rehearsal",
                },
            )
            conn.execute(
                sa.text("INSERT INTO system_config (key, value) VALUES (:key, :value)"),
                {"key": "far-717-rehearsal", "value": "{}"},
            )
    finally:
        engine.dispose()


def _seeded_rows_alive(sync_db_url: str) -> bool:
    engine = sa.create_engine(sync_db_url)
    try:
        with engine.connect() as conn:
            org = conn.execute(
                sa.text("SELECT count(*) FROM organisations WHERE slug = 'far-717-rehearsal'")
            ).scalar_one()
            cfg = conn.execute(
                sa.text("SELECT count(*) FROM system_config WHERE key = 'far-717-rehearsal'")
            ).scalar_one()
            return int(org) == 1 and int(cfg) == 1
    finally:
        engine.dispose()


def _table_exists(sync_db_url: str, table: str) -> bool:
    engine = sa.create_engine(sync_db_url)
    try:
        with engine.connect() as conn:
            present = conn.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": table},
            ).scalar_one()
            return int(present) > 0
    finally:
        engine.dispose()


@pytest.fixture
def rehearsal_migration_db(monkeypatch):
    """A real Postgres migrated to ``_INTERMEDIATE``, ready to be rehearsed.

    Own container (independent of the shared session DB), migration roles
    provisioned, chain applied FOR REAL up to ``_INTERMEDIATE`` — the state a
    production database is in when a deploy rehearses (at revision N, image
    at N+k). Yields ``(async_url, config)``; DATABASE_URL/ADMIN_URL point at
    the container for the duration (monkeypatch context, restored at
    teardown before ``pg.stop()``). Tests read the URL from the fixture only.
    """
    pg = PostgresContainer("postgres:16-alpine")
    pg.start()
    raw = pg.get_connection_url().replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")

    def _provision():
        eng = sa.create_engine(_sync_url(raw))
        with eng.connect() as conn:
            conn.execute(sa.text('DROP ROLE IF EXISTS "modulo_migrate"'))
            conn.execute(sa.text('DROP ROLE IF EXISTS "modulo_breakglass"'))
            conn.execute(sa.text('DROP ROLE IF EXISTS "modulo_app"'))
            conn.execute(sa.text("CREATE ROLE modulo_migrate NOSUPERUSER NOLOGIN BYPASSRLS"))
            conn.execute(sa.text("CREATE ROLE modulo_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'"))
            conn.execute(sa.text("CREATE ROLE modulo_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'"))
            conn.commit()
        eng.dispose()

    _provision()

    app_url = _with_credentials(raw, "modulo_app", "apppass")
    bg_url = _with_credentials(raw, "modulo_breakglass", "bgpass")
    config = _alembic_config(raw)
    with monkeypatch.context() as m:
        m.setenv("DATABASE_URL", raw)
        m.setenv("DATABASE_ADMIN_URL", raw)
        m.setenv("MODULO_BREAK_GLASS_DATABASE_URL", bg_url)
        from modulo.db.bootstrap_role import bootstrap_roles

        _asyncio_run(bootstrap_roles(raw, app_url))
        command.upgrade(config, _INTERMEDIATE)
        yield raw, config
    pg.stop()


def test_rehearsal_success_rolls_back_and_real_upgrade_applies(
    rehearsal_migration_db,
    monkeypatch,
    capfd,
) -> None:
    raw, config = rehearsal_migration_db
    sync = _sync_url(raw)
    _seed_representative_data(sync)

    counts_before = _table_counts(sync)
    version_before = _alembic_version(sync)
    assert version_before == _INTERMEDIATE

    head = _script_head(raw)
    assert head != _INTERMEDIATE, "test must rehearse at least one migration"

    monkeypatch.setenv("ALEMBIC_REHEARSAL", "1")
    command.upgrade(config, "heads")  # must NOT raise — rehearsal succeeds

    assert _alembic_version(sync) == _INTERMEDIATE, "rehearsal must leave alembic_version unchanged"
    assert _table_counts(sync) == counts_before, "rehearsal must leave every table byte-identical"

    captured = capfd.readouterr()
    assert "MIGRATION REHEARSAL" in captured.out
    assert f"DB current revision: {_INTERMEDIATE}" in captured.out
    assert f"Script head:         {head}" in captured.out
    assert "REHEARSAL ONLY" in captured.out
    assert "ALL CHANGES ROLLED BACK" in captured.out

    # A subsequent REAL upgrade of the same chain applies cleanly.
    monkeypatch.delenv("ALEMBIC_REHEARSAL", raising=False)
    command.upgrade(config, "heads")
    assert _alembic_version(sync) == head, "real upgrade must reach the script head"
    assert _seeded_rows_alive(sync), "real migration runs must keep the seeded data"


def test_rehearsal_failure_rolls_back_and_reraises(
    rehearsal_migration_db,
    monkeypatch,
    capfd,
) -> None:
    """Mid-run failure: probe DDL before the failure MUST vanish, then re-raise."""
    from modulo.db.migrations import env as migration_env

    raw, config = rehearsal_migration_db
    sync = _sync_url(raw)
    version_before = _alembic_version(sync)
    counts_before = _table_counts(sync)

    def _explode(connection: sa.Connection) -> None:
        connection.execute(sa.text(f"CREATE TABLE {_REHEARSAL_PROBE_TABLE} (id INTEGER)"))
        raise RuntimeError("far-717 induced rehearsal failure")

    monkeypatch.setattr(migration_env, "do_run_migrations", _explode)
    monkeypatch.setenv("ALEMBIC_REHEARSAL", "1")

    with pytest.raises(RuntimeError, match="far-717 induced rehearsal failure"):
        command.upgrade(config, "heads")

    assert version_before == _INTERMEDIATE
    assert _alembic_version(sync) == _INTERMEDIATE, "failed rehearsal must leave alembic_version unchanged"
    assert _table_counts(sync) == counts_before, "failed rehearsal must leave every table byte-identical"
    assert not _table_exists(sync, _REHEARSAL_PROBE_TABLE), "DDL executed before the failure must roll back"

    captured = capfd.readouterr()
    assert "REHEARSAL FAILED" in captured.out
    assert "far-717 induced rehearsal failure" in captured.out
    assert "Failing step:" in captured.out
    assert "ALL CHANGES ROLLED BACK" in captured.out


def test_rehearsal_refuses_autocommit_escape_hatch(
    rehearsal_migration_db,
    monkeypatch,
    capfd,
) -> None:
    """Side-connection escape hatch: refused during rehearsal (FAR-717).

    Re-narrowing ``alembic_version`` to the legacy VARCHAR(32) makes the
    pre-flight widening need its separate autocommit connection; rehearsal
    mode must refuse it BEFORE any effect can persist (the widening's commit
    is independent of the rehearsal transaction and would survive it).
    """
    raw, config = rehearsal_migration_db
    sync = _sync_url(raw)

    engine = sa.create_engine(sync)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(32)"))
    finally:
        engine.dispose()

    assert _version_column_max_len(sync) == 32

    monkeypatch.setenv("ALEMBIC_REHEARSAL", "1")
    with pytest.raises(RuntimeError, match="escape hatch"):
        command.upgrade(config, "heads")

    assert _version_column_max_len(sync) == 32, "the refused widening must NOT have persisted"
    assert _alembic_version(sync) == _INTERMEDIATE, "refused rehearsal must leave alembic_version unchanged"

    captured = capfd.readouterr()
    assert "REHEARSAL FAILED" in captured.out
    assert "escape hatch" in captured.out
    assert "ALL CHANGES ROLLED BACK" in captured.out

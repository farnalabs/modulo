"""Isolated real-Postgres test for migration 0321 (FAR-1962 — the purge).

Runs the REAL Alembic ``upgrade`` against a live Postgres (testcontainers) on
private databases cloned from a template stepped to the PRE-PURGE head
(``0320_evidence_redaction_rules``), so the legacy ``eval_definitions`` table
still exists. The template is migrated exactly once; each test clones it, so
every attempt starts from the same pre-purge state and a rolled-back attempt
leaves no residue.

Covered:

* HAPPY PATH — a legacy row PLUS its ``evals`` twin (and a ``eval_results`` row
  referencing it, plus a non-terminal run) is seeded, the purge runs, and:
  the table is gone, ``eval_suites`` survives, ``eval_results`` still reads and
  writes through the FK to ``evals``, the tenant trigger resolves ``evals``
  (proven functionally with a cross-org insert), every enumerated catalogue
  object is gone, and the count the migration LOGGED matches the rows deleted.
* FAIL-CLOSED — a legacy row with NO ``evals`` twin makes the migration FAIL,
  and the table is still present afterwards (the transaction rolled back).
* ORDERED REPOINT — a trigger deliberately recreated against the legacy table is
  repointed onto ``evals`` before the drop (an out-of-band deployment).
* INBOUND FK GUARD — a foreign key still referencing the legacy table aborts the
  purge and rolls everything back (never assumed by name: 0294 renamed the old
  ``eval_results_eval_id_fkey`` to the composite ``fk_eval_results_eval_org``).
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

PREV_REV = "0320_evidence_redaction_rules"
PURGE_REV = "0321_purge_eval_definitions"
PURGE_LOGGER = f"alembic.{PURGE_REV}"

# The catalogue objects observed at the pre-purge head. The migration ENUMERATES
# these from the catalogue at run time; this tuple is the test's independent
# expectation of the inventory, so a silent shrink of the drop set fails here.
_EXPECTED_CONSTRAINTS = frozenset(
    {
        "ck_eval_definitions_failure_behaviour",
        "ck_eval_definitions_pass_threshold",
        "ck_eval_definitions_type",
        "ck_eval_definitions_version_gte_1",
        "eval_definitions_account_id_fkey",
        "eval_definitions_organisation_id_fkey",
        "eval_definitions_pipeline_id_fkey",
        "eval_definitions_pkey",
        "fk_eval_definitions_eval_suite_id",
    }
)
_EXPECTED_INDEXES = frozenset(
    {
        "eval_definitions_pkey",
        "ix_eval_definitions_account_id",
        "ix_eval_definitions_deleted_at",
        "ix_eval_definitions_eval_suite_id",
        "ix_eval_definitions_org_pipeline_type_active",
        "ix_eval_definitions_organisation_id",
        "ix_eval_definitions_organisation_id_deleted_at",
        "ix_eval_definitions_pipeline_id",
    }
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "trg_eval_definitions_account_id_tenant",
        "trg_eval_definitions_pipeline_id_tenant",
    }
)
_EXPECTED_POLICIES = frozenset({"rls_org_isolation"})

# Identifiers of the seeded world (fixed literals — test input, never caller
# data — so the DDL-ish statements below stay static).
_ACCOUNT_ID = uuid.UUID("c3210000-0000-0000-0000-000000000001")
_ORG_ID = uuid.UUID("c3210000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("c3210000-0000-0000-0000-000000000003")
_SNAPSHOT_ID = uuid.UUID("c3210000-0000-0000-0000-000000000004")
_SUITE_ID = uuid.UUID("c3210000-0000-0000-0000-000000000005")
_RUN_ID = uuid.UUID("c3210000-0000-0000-0000-000000000006")
_OTHER_ORG_ID = uuid.UUID("c3210000-0000-0000-0000-000000000007")
_OTHER_ACCOUNT_ID = uuid.UUID("c3210000-0000-0000-0000-000000000008")
_OTHER_PIPELINE_ID = uuid.UUID("c3210000-0000-0000-0000-000000000009")
_OTHER_SNAPSHOT_ID = uuid.UUID("c3210000-0000-0000-0000-00000000000a")
_OTHER_EVAL_ID = uuid.UUID("c3210000-0000-0000-0000-00000000000b")

# Two legacy rows on the happy path, so "the logged count matches" is not
# trivially satisfied by a single row.
_EVAL_IDS = (
    uuid.UUID("c3210000-0000-0000-0000-000000000011"),
    uuid.UUID("c3210000-0000-0000-0000-000000000012"),
)
_ORPHAN_EVAL_ID = uuid.UUID("c3210000-0000-0000-0000-000000000013")


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    # Skip fileConfig: its disable_existing_loggers=True would silence the
    # migration's own logger for the rest of the session (caplog assertions).
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _async_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


async def _admin_execute(db_url: str, sql: str, params: dict[str, Any] | None = None) -> None:
    """Run one statement on an AUTOCOMMIT connection (CREATE/DROP DATABASE)."""
    engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _drop_isolated_database(db_url: str, name: str) -> None:
    """Terminate the isolated database's backends, then drop it.

    ``name`` is generated internally, but it rides in as a bind parameter for the
    session query (identifiers in CREATE/DROP DATABASE cannot be bound, so those
    two keep the internally-generated name inline).
    """
    await _admin_execute(
        db_url,
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name AND pid <> pg_backend_pid()",
        {"name": name},
    )
    await _admin_execute(db_url, f'DROP DATABASE IF EXISTS "{name}"')


def _upgrade(url: str, revision: str) -> None:
    with patch.dict(os.environ, {"DATABASE_URL": url, "DATABASE_ADMIN_URL": url}):
        command.upgrade(_alembic_config(url), revision)


async def _provision_cluster_roles(db_url: str) -> None:
    """Create the migration/app roles the reconciliation chain re-owns objects to.

    Idempotent and CREATE-only: the roles are CLUSTER-level, so a session that
    already provisioned them (other integration modules) must not be disturbed —
    dropping a role that owns objects would fail and break the shared container.
    """
    statements = (
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_migrate') "
        "THEN CREATE ROLE modulo_migrate NOSUPERUSER NOLOGIN BYPASSRLS; END IF; END $$;",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_breakglass') "
        "THEN CREATE ROLE modulo_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'; END IF; END $$;",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_app') "
        "THEN CREATE ROLE modulo_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'; END IF; END $$;",
    )
    for sql in statements:
        await _admin_execute(db_url, sql)


@pytest.fixture(scope="session")
def purge_template_db(db_url: str) -> Generator[str, None, None]:
    """A session-scoped database migrated ONCE to the pre-purge head.

    Each test clones this template, so the expensive from-scratch upgrade runs a
    single time and every attempt starts from an identical pre-purge state.
    """
    import asyncio

    name = f"purge_tpl_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin_execute(db_url, f'CREATE DATABASE "{name}" WITH TEMPLATE template0'))
    template_url = _swap_db_name(db_url, name)

    asyncio.run(_provision_cluster_roles(db_url))

    async def _ensure_alembic_table() -> None:
        engine = _async_engine(template_url)
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
                )
                await conn.commit()
        finally:
            await engine.dispose()

    asyncio.run(_ensure_alembic_table())
    _upgrade(template_url, PREV_REV)

    try:
        yield template_url
    finally:
        asyncio.run(_drop_isolated_database(db_url, name))


@pytest_asyncio.fixture
async def pre_purge_url(db_url: str, purge_template_db: str) -> AsyncGenerator[str, None]:
    """A private clone of the pre-purge template, dropped afterwards."""
    template_name = purge_template_db.rsplit("/", 1)[-1]
    name = f"purge_iso_{uuid.uuid4().hex[:10]}"
    await _admin_execute(db_url, f'CREATE DATABASE "{name}" WITH TEMPLATE "{template_name}"')
    try:
        yield _swap_db_name(db_url, name)
    finally:
        await _drop_isolated_database(db_url, name)


async def _seed_world(url: str, *, include_orphan: bool = False) -> None:
    """Seed the org graph, ``eval_suites``, legacy evals, results and a live run.

    Every legacy row in ``_EVAL_IDS`` gets an ``evals`` twin (the happy path);
    ``include_orphan`` additionally seeds a legacy-ONLY row with NO twin — the
    fail-closed case.
    """
    engine = _async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                    "VALUES (:id, 'purge@example.com', 'purge', 'hash', 'local', true), "
                    "(:oid, 'purge-other@example.com', 'purge-other', 'hash', 'local', true)"
                ),
                {"id": str(_ACCOUNT_ID), "oid": str(_OTHER_ACCOUNT_ID)},
            )
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) VALUES "
                    "(:id, 'purge', 'purge', '{}'::jsonb), (:oid, 'purge-other', 'purge-other', '{}'::jsonb)"
                ),
                {"id": str(_ORG_ID), "oid": str(_OTHER_ORG_ID)},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, account_id, name, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json) VALUES "
                    "(:id, :oid, :aid, 'purge pipeline', 10, 30, 300, '{}'::jsonb, '[]'::json), "
                    "(:pid, :ooid, :oaid, 'purge other pipeline', 10, 30, 300, '{}'::jsonb, '[]'::json)"
                ),
                {
                    "id": str(_PIPELINE_ID),
                    "oid": str(_ORG_ID),
                    "aid": str(_ACCOUNT_ID),
                    "pid": str(_OTHER_PIPELINE_ID),
                    "ooid": str(_OTHER_ORG_ID),
                    "oaid": str(_OTHER_ACCOUNT_ID),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                    "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                    "model_backend_pins_json, run_context_defaults, config_json) VALUES "
                    "(:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, '[]'::json, "
                    "'{}'::jsonb, '{}'::json), "
                    "(:sid, :opid, :ooid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, '[]'::json, "
                    "'{}'::jsonb, '{}'::json)"
                ),
                {
                    "id": str(_SNAPSHOT_ID),
                    "pid": str(_PIPELINE_ID),
                    "oid": str(_ORG_ID),
                    "sid": str(_OTHER_SNAPSHOT_ID),
                    "opid": str(_OTHER_PIPELINE_ID),
                    "ooid": str(_OTHER_ORG_ID),
                },
            )
            await conn.execute(
                text("INSERT INTO eval_suites (id, organisation_id, name) VALUES (:id, :oid, 'purge suite')"),
                {"id": str(_SUITE_ID), "oid": str(_ORG_ID)},
            )

            # The legacy rows: every ``_EVAL_IDS`` row carries a live twin; the
            # optional orphan is legacy-ONLY (no ``evals`` row with that id).
            legacy_ids = list(_EVAL_IDS) + ([_ORPHAN_EVAL_ID] if include_orphan else [])
            for eval_id in legacy_ids:
                await conn.execute(
                    text(
                        "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, account_id, "
                        "name, eval_type, config_json, eval_suite_id) VALUES "
                        "(:id, :oid, :pid, :aid, :name, 'llm_judge', '{}'::jsonb, :sid)"
                    ),
                    {
                        "id": str(eval_id),
                        "oid": str(_ORG_ID),
                        "pid": str(_PIPELINE_ID),
                        "aid": str(_ACCOUNT_ID),
                        "name": f"legacy-{eval_id.int % 1000}",
                        "sid": str(_SUITE_ID),
                    },
                )
            for eval_id in _EVAL_IDS:
                await conn.execute(
                    text(
                        "INSERT INTO evals (id, organisation_id, pipeline_id, account_id, name, "
                        "eval_type, config_json) VALUES (:id, :oid, :pid, :aid, :name, 'llm_judge', '{}'::jsonb)"
                    ),
                    {
                        "id": str(eval_id),
                        "oid": str(_ORG_ID),
                        "pid": str(_PIPELINE_ID),
                        "aid": str(_ACCOUNT_ID),
                        "name": f"live-{eval_id.int % 1000}",
                    },
                )

            # A NON-TERMINAL run, so the purge is proven not to depend on a
            # quiesced run table (0254's drain gate is not repeated here).
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                    "run_number, input_hash, langgraph_thread_id) VALUES "
                    "(:id, :oid, :pid, :sid, 'manual', 'running', 1, 'ih', 'purge-thread')"
                ),
                {
                    "id": str(_RUN_ID),
                    "oid": str(_ORG_ID),
                    "pid": str(_PIPELINE_ID),
                    "sid": str(_SNAPSHOT_ID),
                },
            )
            # eval_results rows referencing the eval id — the composite FK
            # already targets ``evals`` (0294), so these must survive the drop.
            for eval_id in _EVAL_IDS:
                await conn.execute(
                    text(
                        "INSERT INTO eval_results (run_id, eval_id, organisation_id, passed, score) "
                        "VALUES (:rid, :eid, :oid, true, 0.9)"
                    ),
                    {"rid": str(_RUN_ID), "eid": str(eval_id), "oid": str(_ORG_ID)},
                )
    finally:
        await engine.dispose()


async def _scalar(url: str, sql: str, params: dict[str, Any] | None = None) -> Any:
    engine = _async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params or {})).scalar()
    finally:
        await engine.dispose()


async def _rows(url: str, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = _async_engine(url)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(sql), params or {})).fetchall())
    finally:
        await engine.dispose()


async def _legacy_oid(url: str) -> int:
    oid = await _scalar(url, "SELECT to_regclass('public.\"eval_definitions\"')::oid")
    assert oid is not None, "the pre-purge database must still hold eval_definitions"
    return int(oid)


async def _catalogue_inventory(url: str, oid: int) -> dict[str, set[str]]:
    """The drop set as the live catalogue reports it, keyed by object kind."""
    queries = {
        "constraints": "SELECT conname FROM pg_constraint WHERE conrelid = :oid",
        "indexes": "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = :oid",
        "triggers": "SELECT tgname FROM pg_trigger WHERE tgrelid = :oid AND NOT tgisinternal",
        "policies": "SELECT polname FROM pg_policy WHERE polrelid = :oid",
    }
    return {kind: {str(row[0]) for row in await _rows(url, sql, {"oid": oid})} for kind, sql in queries.items()}


def _logged_counts(caplog: pytest.LogCaptureFixture) -> list[int]:
    """Every numeric ``deleted N proven-redundant`` count the migration logged."""
    counts: list[int] = []
    for record in caplog.records:
        if record.name != PURGE_LOGGER:
            continue
        match = re.search(r"deleted (\d+) proven-redundant", record.getMessage())
        if match:
            counts.append(int(match.group(1)))
    return counts


async def test_purge_asserts_deletes_and_drops(pre_purge_url: str, caplog: pytest.LogCaptureFixture) -> None:
    """Happy path: twins present -> table dropped, dependents intact, count logged."""
    await _seed_world(pre_purge_url)
    legacy_oid = await _legacy_oid(pre_purge_url)
    legacy_rows = await _scalar(pre_purge_url, 'SELECT COUNT(*) FROM public."eval_definitions"')
    assert legacy_rows == 2

    # The drop set the migration will enumerate, read here INDEPENDENTLY, so it
    # is compared against a fixed expectation rather than against itself.
    inventory = await _catalogue_inventory(pre_purge_url, legacy_oid)
    assert inventory["constraints"] == set(_EXPECTED_CONSTRAINTS)
    assert inventory["indexes"] == set(_EXPECTED_INDEXES)
    assert inventory["triggers"] == set(_EXPECTED_TRIGGERS)
    assert inventory["policies"] == set(_EXPECTED_POLICIES)

    caplog.set_level(logging.INFO, logger=PURGE_LOGGER)
    _upgrade(pre_purge_url, PURGE_REV)

    # 1. The table is gone.
    assert await _scalar(pre_purge_url, "SELECT to_regclass('public.\"eval_definitions\"')") is None
    assert await _scalar(pre_purge_url, "SELECT COUNT(*) FROM pg_class WHERE relname = 'eval_definitions'") == 0

    # 2. eval_suites (and its row) survives.
    assert await _scalar(pre_purge_url, "SELECT to_regclass('public.\"eval_suites\"')") is not None
    assert await _scalar(pre_purge_url, "SELECT COUNT(*) FROM eval_suites WHERE id = :id", {"id": str(_SUITE_ID)}) == 1

    # 3. eval_results still reads AND writes through the FK to evals.
    joined = await _rows(
        pre_purge_url,
        "SELECT r.eval_id, e.name FROM eval_results r JOIN evals e ON e.id = r.eval_id "
        "WHERE r.eval_id = :eid ORDER BY e.name",
        {"eid": str(_EVAL_IDS[0])},
    )
    assert len(joined) == 1
    assert joined[0][1] == f"live-{_EVAL_IDS[0].int % 1000}"
    engine = _async_engine(pre_purge_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO eval_results (run_id, eval_id, organisation_id, passed, score) "
                    "VALUES (:rid, :eid, :oid, false, 0.1)"
                ),
                {"rid": str(_RUN_ID), "eid": str(_EVAL_IDS[1]), "oid": str(_ORG_ID)},
            )
        async with engine.connect() as conn:
            post = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM eval_results WHERE eval_id = :eid"), {"eid": str(_EVAL_IDS[1])}
                )
            ).scalar()
        assert post == 2
    finally:
        await engine.dispose()

    # 4. The tenant trigger resolves evals — by definition AND functionally.
    trigger_def = await _scalar(
        pre_purge_url,
        "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t WHERE t.tgname = 'trg_eval_results_eval_id_tenant'",
    )
    assert trigger_def is not None
    assert "'evals'" in trigger_def
    assert "eval_definitions" not in trigger_def

    engine = _async_engine(pre_purge_url)
    try:
        # An eval living in ANOTHER organisation must be rejected: the guard can
        # only do that by resolving the live ``evals`` row.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO evals (id, organisation_id, pipeline_id, account_id, name, "
                    "eval_type, config_json) VALUES (:id, :oid, :pid, :aid, 'other-org eval', "
                    "'llm_judge', '{}'::jsonb)"
                ),
                {
                    "id": str(_OTHER_EVAL_ID),
                    "oid": str(_OTHER_ORG_ID),
                    "pid": str(_OTHER_PIPELINE_ID),
                    "aid": str(_OTHER_ACCOUNT_ID),
                },
            )
        with pytest.raises(DBAPIError, match="cross-organisation reference"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO eval_results (run_id, eval_id, organisation_id, passed) "
                        "VALUES (:rid, :eid, :oid, true)"
                    ),
                    {"rid": str(_RUN_ID), "eid": str(_OTHER_EVAL_ID), "oid": str(_ORG_ID)},
                )
    finally:
        await engine.dispose()

    # 5. Every enumerated catalogue object is gone — checked by NAME for the
    #    table-specific kinds, and by the legacy relation's oid for the policy
    #    (``rls_org_isolation`` is a shared name on every org-scoped table).
    for kind, inventory_key, sql in (
        ("constraint", "constraints", "SELECT COUNT(*) FROM pg_constraint WHERE conname = ANY(:names)"),
        ("index", "indexes", "SELECT COUNT(*) FROM pg_class WHERE relkind = 'i' AND relname::text = ANY(:names)"),
        ("trigger", "triggers", "SELECT COUNT(*) FROM pg_trigger WHERE tgname::text = ANY(:names)"),
    ):
        names = sorted(inventory[inventory_key])
        assert names, f"no {kind} was enumerated for the legacy table"
        assert await _scalar(pre_purge_url, sql, {"names": names}) == 0, f"{kind}(s) survived the drop: {names}"
    assert (
        await _scalar(pre_purge_url, "SELECT COUNT(*) FROM pg_policy WHERE polrelid = :oid", {"oid": legacy_oid}) == 0
    )

    # 6. The logged count matches the rows actually deleted.
    assert _logged_counts(caplog) == [2]
    order = [
        record.getMessage()
        for record in caplog.records
        if record.name == PURGE_LOGGER and record.levelno >= logging.INFO
    ]
    assert any("assertion passed" in message for message in order)
    assert any("deleted 2 proven-redundant" in message for message in order)
    assert any("table dropped" in message for message in order)


async def test_purge_fails_closed_when_a_legacy_row_has_no_twin(
    pre_purge_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A legacy row with no ``evals`` twin ABORTS, and the transaction rolls back."""
    await _seed_world(pre_purge_url, include_orphan=True)
    legacy_rows = await _scalar(pre_purge_url, 'SELECT COUNT(*) FROM public."eval_definitions"')
    assert legacy_rows == 3  # two twinned rows + one legacy-only orphan

    caplog.set_level(logging.INFO, logger=PURGE_LOGGER)
    with pytest.raises(RuntimeError, match="no evals twin with the same id"):
        _upgrade(pre_purge_url, PURGE_REV)

    # The table AND both rows are still there: the failed transaction rolled back.
    assert await _scalar(pre_purge_url, "SELECT to_regclass('public.\"eval_definitions\"')") is not None
    assert await _scalar(pre_purge_url, 'SELECT COUNT(*) FROM public."eval_definitions"') == 3
    assert (
        await _scalar(
            pre_purge_url, "SELECT COUNT(*) FROM eval_definitions WHERE id = :id", {"id": str(_ORPHAN_EVAL_ID)}
        )
        == 1
    )
    # …and no delete was logged for this attempt.
    assert not _logged_counts(caplog)
    # Alembic did not record the revision: the chain is still at the pre-purge head.
    assert await _scalar(pre_purge_url, "SELECT version_num FROM alembic_version") == PREV_REV


async def test_purge_repoints_a_trigger_that_still_resolves_the_legacy_table(pre_purge_url: str) -> None:
    """An out-of-band trigger pointing at the legacy table is repointed first."""
    await _seed_world(pre_purge_url)
    engine = _async_engine(pre_purge_url)
    try:
        # Simulate a deployment whose trigger was recreated against the legacy
        # table (the pre-0254 shape) — the migration must repair it, in order,
        # before dropping the table.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER trg_eval_results_eval_id_tenant ON public.eval_results"))
            await conn.execute(
                text(
                    "CREATE TRIGGER trg_eval_results_eval_id_tenant BEFORE INSERT OR UPDATE OF eval_id, "
                    "organisation_id ON public.eval_results FOR EACH ROW "
                    "EXECUTE FUNCTION public.enforce_same_organisation('eval_definitions', 'eval_id')"
                )
            )
    finally:
        await engine.dispose()

    _upgrade(pre_purge_url, PURGE_REV)

    assert await _scalar(pre_purge_url, "SELECT to_regclass('public.\"eval_definitions\"')") is None
    trigger_def = await _scalar(
        pre_purge_url,
        "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t WHERE t.tgname = 'trg_eval_results_eval_id_tenant'",
    )
    assert "'evals'" in trigger_def
    assert "eval_definitions" not in trigger_def


async def test_purge_refuses_when_a_foreign_key_still_references_the_table(pre_purge_url: str) -> None:
    """An inbound FK (never assumed by name) aborts the purge and rolls back."""
    await _seed_world(pre_purge_url)
    engine = _async_engine(pre_purge_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE purge_inbound_fk_probe ("
                    "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                    "eval_id uuid REFERENCES eval_definitions(id) ON DELETE CASCADE)"
                )
            )
    finally:
        await engine.dispose()

    with pytest.raises(RuntimeError, match="foreign key"):
        _upgrade(pre_purge_url, PURGE_REV)

    assert await _scalar(pre_purge_url, "SELECT to_regclass('public.\"eval_definitions\"')") is not None
    assert await _scalar(pre_purge_url, 'SELECT COUNT(*) FROM public."eval_definitions"') == 2
    assert await _scalar(pre_purge_url, "SELECT version_num FROM alembic_version") == PREV_REV

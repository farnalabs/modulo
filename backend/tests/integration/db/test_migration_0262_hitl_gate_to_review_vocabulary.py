"""Integration test for migration 0262 hitl_gate -> review graph-JSON rewrite.

The 2026-10-03 production deploy rehearsal aborted on migration 0262 with
``psycopg.errors.InvalidParameterValue: cannot delete from scalar``. The
``pipeline_snapshots.graph_json`` rewrite applied the jsonb key-delete operator
(``- 'gate_id'``) to every edge whose top-level ``hitl_gate_config`` key existed,
but the guard was key *existence* (``edge ? 'hitl_gate_config'``), not value
*type*. Production held snapshots whose ``hitl_gate_config`` was a scalar
(string) or JSON ``null``, so ``-`` was called on a scalar and the whole
migration — and therefore the deploy — failed. This only surfaced against real
production data; fresh-DB CI and the unit tests (which assert SQL structure)
could not catch it.

This test runs the real Alembic ``upgrade`` of 0262 against a *fresh, isolated*
live Postgres (a private database cloned from ``template0`` so the shared
session schema is never touched) seeded with exactly the shapes that aborted
production:

  * a scalar (string) ``hitl_gate_config`` on a snapshot edge;
  * a JSON ``null`` ``hitl_gate_config``;
  * a normal object ``hitl_gate_config`` carrying ``gate_id``;
  * synthetic ``hitl_gate_<src>_<tgt>`` node IDs (and edge endpoints);
  * a scalar ``pipeline_edges.hitl_gate_config`` (so the sibling
    ``jsonb_each`` loop is proven not to blow up on a non-object either).

It then asserts the rewrite completes, renames every ``hitl_gate_`` / ``gate_id``
occurrence, preserves the scalar/null values under the new key, and leaves no
row that would re-match the batched loop's ``LIKE '%hitl_gate_%'`` selector (a
non-terminating loop is the other failure mode a bad fix could introduce).

The lesson (repo AGENTS.md): real-Postgres integration tests are the only thing
that catches SQL semantic breaks — substring-routed mocks prove nothing.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/
MIGRATION_REV = "0262_hitl_gate_to_review_vocabulary"
PREV_REV = "0261_decision_record_payload"


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
async def isolated_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    Mirrors ``test_migration_0126_eval_suite.py`` / ``test_migration_0191``: a
    private database cloned from ``template0`` (guaranteed empty) so this test's
    upgrade never mutates the shared session schema. ``env.py`` resolves the
    target DB from ``DATABASE_URL`` / ``DATABASE_ADMIN_URL`` (preferring the
    latter), so both are pinned to the isolated database for the whole test.
    """
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0262_iso_{uuid.uuid4().hex[:10]}"
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
                ).bindparams(n=db_name)
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await admin_engine.dispose()


async def _seed(engine: AsyncEngine) -> None:
    """Seed the exact shapes that aborted the production rehearsal."""
    account = "aaaaaaaa-0000-0000-0000-000000000001"
    org = "bbbbbbbb-0000-0000-0000-000000000001"
    pipeline = "cccccccc-0000-0000-0000-000000000001"
    edge = "dddddddd-0000-0000-0000-000000000001"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO accounts (id, email, display_name) VALUES (:a, 'm0262@example.com', 'm0262')"),
            {"a": account},
        )
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, status, authz_enforce, triggers_paused, "
                "guardrails_kill_switch, org_cumulative_spend_cents, settings_json, otel_config_json) "
                "VALUES (:o, 'm0262', 'm0262', 'active', false, false, false, 0, '{}', '{}')"
            ),
            {"o": org},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, name, account_id, organisation_id, graph_nodes_json) "
                'VALUES (:p, \'m0262\', :a, :o, \'[{"id":"hitl_gate_a_b"},{"id":"plain"}]\'::json)'
            ),
            {"p": pipeline, "a": account, "o": org},
        )
        # A scalar (non-object) pipeline_edges config must not trip jsonb_each.
        await conn.execute(
            text(
                "INSERT INTO pipeline_edges (id, pipeline_id, source_node_id, target_node_id, edge_type, "
                "organisation_id, source_port, target_port, hitl_gate_config) "
                "VALUES (:e, :p, '11111111-0000-0000-0000-000000000001', "
                "'22222222-0000-0000-0000-000000000002', 'normal', :o, 'out', 'in', '\"scalar-config\"'::json)"
            ),
            {"e": edge, "p": pipeline, "o": org},
        )
        snapshots = {
            1: (
                '{"nodes":[{"id":"n1"}],"edges":[{"id":"e1","source":"n1",'
                '"target":"hitl_gate_n1_n2","hitl_gate_config":"hitl_gate_n1_n2"}]}'
            ),
            2: '{"nodes":[{"id":"hitl_gate_a_b"}],"edges":[{"id":"e2","hitl_gate_config":null}]}',
            3: (
                '{"nodes":[{"id":"hitl_gate_x_y"}],"edges":[{"id":"e3",'
                '"hitl_gate_config":{"gate_id":"g3","label":"L"}}]}'
            ),
        }
        for version, graph_json in snapshots.items():
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                    "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                    "model_backend_pins_json, run_context_defaults) "
                    "VALUES (:id, :p, :o, :v, CAST(:g AS jsonb), '[]', '[]', '[]', '[]', '{}')"
                ),
                {"id": str(uuid.uuid4()), "p": pipeline, "o": org, "v": version, "g": graph_json},
            )


async def test_0262_rewrites_scalar_and_object_configs_without_error(isolated_db_url: str) -> None:
    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    try:
        await _seed(engine)

        # The production abort happened here.
        command.upgrade(_alembic_config(isolated_db_url), MIGRATION_REV)

        async with engine.connect() as conn:
            # Scalar string preserved under the renamed key; no hitl_gate_ left.
            scalar: dict[str, Any] = (
                await conn.execute(text("SELECT graph_json FROM pipeline_snapshots WHERE snapshot_version = 1"))
            ).scalar_one()
            assert scalar["edges"][0]["hitl_review_config"] == "hitl_review_n1_n2"
            assert scalar["edges"][0]["target"] == "hitl_review_n1_n2"
            assert "hitl_gate_" not in str(scalar)

            # JSON null preserved under the renamed key.
            nulled: dict[str, Any] = (
                await conn.execute(text("SELECT graph_json FROM pipeline_snapshots WHERE snapshot_version = 2"))
            ).scalar_one()
            assert "hitl_review_config" in nulled["edges"][0]
            assert nulled["edges"][0]["hitl_review_config"] is None
            assert nulled["nodes"][0]["id"] == "hitl_review_a_b"

            # Object config: key + inner gate_id renamed.
            obj: dict[str, Any] = (
                await conn.execute(text("SELECT graph_json FROM pipeline_snapshots WHERE snapshot_version = 3"))
            ).scalar_one()
            assert obj["edges"][0]["hitl_review_config"]["review_id"] == "g3"
            assert "hitl_gate_config" not in obj["edges"][0]
            assert "gate_id" not in obj["edges"][0]["hitl_review_config"]

            # pipelines node IDs renamed.
            nodes: list[dict[str, Any]] = (
                await conn.execute(text("SELECT graph_nodes_json FROM pipelines"))
            ).scalar_one()
            assert nodes == [{"id": "hitl_review_a_b"}, {"id": "plain"}]

            # pipeline_edges scalar config preserved, column renamed by the migration.
            edge_cfg: str = (await conn.execute(text("SELECT hitl_review_config FROM pipeline_edges"))).scalar_one()
            assert edge_cfg == "scalar-config"

            # No row still matches the batched-loop selector -> the while loop
            # would terminate rather than spin forever.
            residual: int = (
                await conn.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM pipeline_snapshots WHERE graph_json::text LIKE '%hitl_gate_%') "
                        "+ (SELECT count(*) FROM pipelines WHERE graph_nodes_json::text LIKE '%hitl_gate_%') "
                        "+ (SELECT count(*) FROM pipeline_edges WHERE hitl_review_config::text LIKE '%gate_id%')"
                    )
                )
            ).scalar_one()
            assert residual == 0
    finally:
        await engine.dispose()

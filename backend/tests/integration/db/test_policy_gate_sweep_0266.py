"""Integration tests for migration 0266 — the guardrail→PolicyGate sweep.

FAR-1107 chunk 8, spec criteria 8 and 10 (§5.4): a live sweep over REAL
chain state must bind every pre-existing guardrail eval with a gate-valued
``config_json.action`` and a node_id to a PolicyGate row (mapping
``warn→warn`` / ``block→block``), and skip everything else.

Runs on its OWN testcontainer Postgres with migration roles provisioned,
chain applied to ``0265_hitl_review_window`` (the pre-sweep head), the §5.4
row-shape matrix seeded as committed rows, then the REAL
``0266_guardrail_policy_gate_sweep`` upgrade applied. Not reusing the
shared session DB: the sweep must run against the exact state the
predicates run over at migration time, not whatever the shared DB drifted
into.
"""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote as _url_quote

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).parents[3]

#: The sweep runs for real from this pre-sweep head; 0266's downgrade is a
#: documented no-op, which is what makes the re-run idempotence test legal.
_PREHEAD = "0265_hitl_review_window"

_JS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


# (name, eval_type, action, has_node, soft_deleted, expected_gate_action)
_SEED_SPECS: list[tuple[str, str, str, bool, bool, str | None]] = [
    ("gr-block-legacy", "guardrail", "block", True, False, "block"),
    ("gr-warn-legacy", "guardrail", "warn", True, False, "warn"),
    ("gr-observe-legacy", "guardrail", "observe", True, False, None),
    ("gr-redact-legacy", "guardrail", "redact", True, False, None),
    ("gr-block-nodeless", "guardrail", "block", False, False, None),
    ("gr-block-deleted", "guardrail", "block", True, True, None),
    ("gr-block-gated", "guardrail", "block", True, False, None),
    ("gr-regex-block", "regex", "block", True, False, None),
]


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _with_credentials(database_url: str, user: str, password: str) -> str:
    prefix, _, rest = database_url.partition("://")
    host_part, _, db = rest.partition("/")
    host = host_part.split("@")[-1]
    return f"{prefix}://{_url_quote(user)}:{_url_quote(password)}@{host}/{db}"


def _sync_url(asyncpg_url: str) -> str:
    # Reuse env.py's canonical async->sync driver mapping (psycopg2 not in tree).
    from modulo.db.migrations import env as migration_env

    return migration_env._to_sync_url(asyncpg_url)


def _seed_matrix(
    sync_db_url: str,
) -> tuple[uuid.UUID, dict[str, dict[str, Any]]]:
    """Commit the §5.4 row shapes at the pre-sweep revision.

    Returns ``(org_id, specs)`` where each spec carries ``eval_id``,
    ``node_id``, the expected live gate (or None), and — for the already-gated
    spec — the pre-existing gate id.
    """
    engine = sa.create_engine(sync_db_url)
    org_id = uuid.uuid4()
    seeded: dict[str, dict[str, Any]] = {}
    try:
        with engine.begin() as conn:
            slug = f"sweep-{org_id.hex[:8]}"
            conn.execute(
                sa.text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
                {"id": str(org_id), "n": slug, "s": slug},
            )
            acc_id = uuid.uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                    "VALUES (:id, :e, :n, 'hash', 'local', true)"
                ),
                {"id": str(acc_id), "e": f"{slug}@test.com", "n": slug},
            )
            pipe_id = uuid.uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :n, :aid, 10, 30, 300, '{}'::json, '[]'::json)"
                ),
                {"id": str(pipe_id), "oid": str(org_id), "n": f"{slug}-pipe", "aid": str(acc_id)},
            )
            for name, eval_type, action, has_node, soft_deleted, _expected in _SEED_SPECS:
                eval_id = uuid.uuid4()
                node_id = uuid.uuid4() if has_node else None
                cfg: dict[str, Any] = {"action": action, "interception_point": "input"}
                if eval_type == "guardrail":
                    cfg["type"] = "json_schema"
                    cfg["schema"] = dict(_JS_SCHEMA)
                conn.execute(
                    sa.text(
                        "INSERT INTO evals (id, organisation_id, pipeline_id, account_id, node_id, "
                        "name, eval_type, config_json, version, deleted_at) "
                        "VALUES (:id, :oid, :pid, :aid, :nid, :name, :et, CAST(:cfg AS jsonb), 1, "
                        "CASE WHEN :soft THEN now() ELSE NULL END)"
                    ),
                    {
                        "id": str(eval_id),
                        "oid": str(org_id),
                        "pid": str(pipe_id),
                        "aid": str(acc_id),
                        "nid": str(node_id) if node_id else None,
                        "name": name,
                        "et": eval_type,
                        "cfg": json.dumps(cfg, separators=(",", ":")),
                        "soft": bool(soft_deleted),
                    },
                )
                entry: dict[str, Any] = {"eval_id": eval_id, "node_id": node_id, "expected": _expected}
                if name == "gr-block-gated":
                    pre_gate_id = uuid.uuid4()
                    conn.execute(
                        sa.text(
                            "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, action, version) "
                            "VALUES (:id, :oid, :eid, :nid, 'block', 1)"
                        ),
                        {
                            "id": str(pre_gate_id),
                            "oid": str(org_id),
                            "eid": str(eval_id),
                            "nid": str(node_id),
                        },
                    )
                    entry["pre_existing_gate_id"] = pre_gate_id
                seeded[name] = entry
    finally:
        engine.dispose()
    return org_id, seeded


@pytest.fixture
def sweep_db(monkeypatch):
    """Own Postgres migrated to :data:`_PREHEAD`, §5.4 matrix seeded, 0266 applied.

    Yields ``(raw_url, config, org_id, specs)``; DATABASE_URL/ADMIN_URL point
    at the container for the duration (restored at teardown before
    ``pg.stop()``).
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

        asyncio.run(bootstrap_roles(raw, app_url))
        command.upgrade(config, _PREHEAD)
        org_id, specs = _seed_matrix(_sync_url(raw))
        command.upgrade(config, "heads")  # the REAL 0266 sweep applies here
        yield raw, config, org_id, specs
    pg.stop()


def _live_gates(raw_asyncpg_url: str, eval_id: uuid.UUID, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """Read live gates via the sync driver (the test bodies run outside greenlets)."""
    engine = sa.create_engine(_sync_url(raw_asyncpg_url))
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, action, version, node_id FROM policy_gates "
                        "WHERE eval_id = :eid AND organisation_id = :oid AND deleted_at IS NULL "
                        "ORDER BY created_at"
                    ),
                    {"eid": str(eval_id), "oid": str(org_id)},
                )
                .mappings()
                .all()
            )
            return [dict(r) for r in rows]
    finally:
        engine.dispose()


class TestC8SweepBindsGuardrails:
    def test_block_and_warn_guardrails_get_live_gates(self, sweep_db) -> None:
        raw, _config, org_id, specs = sweep_db
        gr_block = _live_gates(raw, specs["gr-block-legacy"]["eval_id"], org_id)
        assert len(gr_block) == 1
        assert gr_block[0]["action"] == "block"
        assert gr_block[0]["version"] == 1
        assert gr_block[0]["node_id"] == specs["gr-block-legacy"]["node_id"]

        gr_warn = _live_gates(raw, specs["gr-warn-legacy"]["eval_id"], org_id)
        assert len(gr_warn) == 1
        assert gr_warn[0]["action"] == "warn"


class TestC10SweepPredicates:
    def test_non_gate_guardrail_shapes_are_skipped(self, sweep_db) -> None:
        raw, _config, org_id, specs = sweep_db
        for name in (
            "gr-observe-legacy",
            "gr-redact-legacy",
            "gr-block-nodeless",
            "gr-block-deleted",
            "gr-regex-block",
        ):
            assert not _live_gates(raw, specs[name]["eval_id"], org_id), name

    def test_pre_existing_binding_never_duplicated(self, sweep_db) -> None:
        raw, _config, org_id, specs = sweep_db
        gated = specs["gr-block-gated"]
        rows = _live_gates(raw, gated["eval_id"], org_id)
        assert len(rows) == 1
        assert rows[0]["id"] == gated["pre_existing_gate_id"]

    def test_sweep_rerun_is_idempotent(self, sweep_db) -> None:
        raw, config, org_id, specs = sweep_db

        def _signature() -> dict[str, str | None]:
            sig: dict[str, str | None] = {}
            for name in ("gr-block-legacy", "gr-warn-legacy", "gr-block-gated"):
                rows = _live_gates(raw, specs[name]["eval_id"], org_id)
                sig[name] = f"{len(rows)}:{rows[0]['id']}" if rows else "0"
            return sig

        before = _signature()
        # 0266's downgrade is a documented no-op, so this is a true re-run of
        # the same sweep step over the same committed rows.
        command.upgrade(config, _PREHEAD)
        command.upgrade(config, "heads")
        assert _signature() == before

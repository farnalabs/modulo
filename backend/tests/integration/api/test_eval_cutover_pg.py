"""FAR-1101 chunk-3b acceptance coverage - real-Postgres write cutover.

Real-Postgres acceptance coverage for chunk-3b criterion numbers 10, 11, 12,
``evals`` (+ ``PolicyGate``) through the real write helpers, with the session
factory pointed at a real RLS-migrated database - no mocks under the write
path. Soft-delete filters and org scoping are enforced by the real schema.

Criteria text lives in the internal chunk-3b spec (not in this public repo).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.future import select

from modulo.db.models.eval import Eval

pytestmark = [pytest.mark.integration]

_API_KEY = "mk_pgprefix_testsecretkey1234567890abc"


class McpEnv:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self.org_id = uuid.uuid4()
        self.admin_id = uuid.uuid4()
        self.pipeline_id = uuid.uuid4()

    async def one(self, sql: str, params: dict | None = None):
        async with self.engine.connect() as conn:
            return (await conn.execute(sa_text(sql), params or {})).one()

    async def scalar(self, sql: str, params: dict | None = None):
        async with self.engine.connect() as conn:
            return (await conn.execute(sa_text(sql), params or {})).scalar_one()

    async def seed(self) -> None:
        e = self.engine
        async with e.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {
                    "id": str(self.org_id),
                    "name": f"PG Org {self.org_id.hex[:8]}",
                    "slug": f"pg-{self.org_id.hex[:8]}",
                },
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, "
                    "auth_provider, active) VALUES (:id, :email, :name, :hash, 'local', true)"
                ),
                {
                    "id": str(self.admin_id),
                    "email": f"pg-admin-{self.admin_id.hex[:10]}@example.com",
                    "name": "PG Admin",
                    "hash": "unused",
                },
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:id, :aid, :oid, 'admin')"
                ),
                {"id": str(uuid.uuid4()), "aid": str(self.admin_id), "oid": str(self.org_id)},
            )
            await conn.execute(
                sa_text(
                    "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"
                ),
                {
                    "id": str(self.pipeline_id),
                    "oid": str(self.org_id),
                    "name": "PG Pipeline",
                    "uid": str(self.admin_id),
                },
            )


def _activate_mcp(env: McpEnv) -> SimpleNamespace:
    """Patch the MCP session factory onto the real DB engine + auth + ctx."""
    from modulo.api import mcp_server

    def fake_factory() -> async_sessionmaker:
        return async_sessionmaker(env.engine, expire_on_commit=False)

    patchers = [
        patch.object(mcp_server, "_get_session_factory", side_effect=fake_factory),
        patch.object(mcp_server, "validate_current_auth", return_value=True),
    ]
    for p in patchers:
        p.start()

    mcp_server._ctx_org_id.set(env.org_id)
    mcp_server._ctx_user_id.set(env.admin_id)
    mcp_server._ctx_role.set("admin")
    mcp_server._ctx_auth_token.set(_API_KEY)
    mcp_server._ctx_auth_type.set("api_key")

    def teardown() -> None:
        for p in patchers:
            p.stop()
        mcp_server._ctx_org_id.set(None)
        mcp_server._ctx_user_id.set(None)
        mcp_server._ctx_role.set(None)
        mcp_server._ctx_auth_token.set(None)
        mcp_server._ctx_auth_type.set(None)

    return SimpleNamespace(teardown=teardown)


@pytest_asyncio.fixture
async def pg_env(db_engine: AsyncEngine) -> McpEnv:
    env = McpEnv(db_engine)
    await env.seed()
    return env


@pytest.fixture
def mcp_on(pg_env: McpEnv) -> SimpleNamespace:
    return _activate_mcp(pg_env)


# ---------------------------------------------------------------------------
# Criteria 5 / 19 (MCP create / soft-delete)     MCP create / soft-delete / merged update, real DB
# ---------------------------------------------------------------------------


async def test_mcp_create_persists_eval_and_gate(pg_env: McpEnv, mcp_on: SimpleNamespace) -> None:
    """Criterion 10     MCP create persists Eval + PolicyGate on a real DB and
    writes nothing to eval_definitions."""
    from modulo.api.mcp_server import create_eval_definition

    try:
        node_id = uuid.uuid5(uuid.NAMESPACE_DNS, "pg-created-node")
        result = await create_eval_definition(
            pipeline_id=str(pg_env.pipeline_id),
            node_id=str(node_id),
            name="pg-created",
            eval_type="regex",
            config_json={"field": "output", "pattern": "^[A-Za-z]*$"},
        )

        assert result["name"] == "pg-created"
        assert (await pg_env_sql_count(pg_env, "evals", "name = 'pg-created'")) == 1
        assert (await pg_env_sql_count(pg_env, "eval_definitions")) == 0

        row = await pg_env.one("SELECT version, node_id, eval_type FROM evals WHERE name = 'pg-created'")
        assert row.version == 1
        assert row.node_id == node_id
        assert row.eval_type == "regex"

        gate = await pg_env.one(
            "SELECT action, deleted_at FROM policy_gates WHERE eval_id = "
            "(SELECT id FROM evals WHERE name = 'pg-created')"
        )
        assert gate.action == "warn"
        assert gate.deleted_at is None
    finally:
        mcp_on.teardown()


async def test_mcp_soft_delete_stamps_real_rows(pg_env: McpEnv, mcp_on: SimpleNamespace) -> None:
    """Criterion 19 (guardrail-eval soft delete): MCP soft delete stamps deleted_at + deleted_by on the
    real eval row (physically present, logically removed)."""
    from modulo.api.mcp_server import create_eval_definition, delete_eval_definition

    try:
        created = await create_eval_definition(
            pipeline_id=str(pg_env.pipeline_id),
            node_id=None,
            name="pg-mortal",
            eval_type="guardrail",
            config_json={"action": "observe", "type": "regex"},
        )
        assert "error" not in created, created
        result = await delete_eval_definition(eval_id=created["id"], hard=False)
        assert result["soft_deleted"] is True, result
        assert result["hard_deleted"] is False

        row = await pg_env.one(
            "SELECT deleted_at IS NOT NULL, deleted_by = :admin FROM evals WHERE id = :id",
            {"id": uuid.UUID(created["id"]), "admin": str(pg_env.admin_id)},
        )
        assert row[0] is True
        assert row[1] is True
        assert (await pg_env_sql_count(pg_env, "evals", "name = 'pg-mortal'")) == 1

        # Non-guardrail evals take the hard path even with hard=False.
        plain = await create_eval_definition(
            pipeline_id=str(pg_env.pipeline_id), node_id=None, name="pg-svelte", eval_type="regex"
        )
        assert "error" not in plain, plain
        plain_result = await delete_eval_definition(eval_id=plain["id"], hard=False)
        assert plain_result["hard_deleted"] is True
        assert (await pg_env_sql_count(pg_env, "evals", "name = 'pg-svelte'")) == 0
    finally:
        mcp_on.teardown()


async def test_mcp_merged_update_bumps_version_keeps_one_gate(pg_env: McpEnv, mcp_on: SimpleNamespace) -> None:
    """Criterion 17     repeated merged updates bump version and keep exactly
    one live gate row for the node-scoped eval."""
    from modulo.api.mcp_server import create_eval_definition, update_eval_definition

    try:
        created = await create_eval_definition(
            pipeline_id=str(pg_env.pipeline_id),
            node_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, "pg-gate-node")),
            name="pg-updatable",
            eval_type="regex",
            config_json={"field": "output", "pattern": "one"},
        )
        eval_id = uuid.UUID(created["id"])

        for pat in ("two", "three"):
            await update_eval_definition(eval_id=str(eval_id), config_json={"field": "output", "pattern": pat})

        row = await pg_env.one("SELECT version, name FROM evals WHERE id = :id", {"id": eval_id})
        assert row.version == 3
        assert row.name == "pg-updatable"

        gate_count = await pg_env.scalar(
            "SELECT COUNT(*) FROM policy_gates WHERE eval_id = :id AND deleted_at IS NULL",
            {"id": eval_id},
        )
        assert int(gate_count) == 1
    finally:
        mcp_on.teardown()


# ---------------------------------------------------------------------------
# RLS scoping + soft-delete filter + loader lens
# ---------------------------------------------------------------------------


async def test_other_orgs_evals_invisible_to_rls_app_role(
    db_engine: AsyncEngine, modulo_app_engine: AsyncEngine
) -> None:
    """Criterion 11     another org's evals are invisible through the real
    ``modulo_app`` role (NOBYPASSRLS     RLS applies), the shape the MCP write
    nodes use in production (set_rls_org + set_rls_user_context)."""
    from modulo.db.rls import set_rls_org, set_rls_user_context

    env = McpEnv(db_engine)
    await env.seed()
    other_org = uuid.uuid4()
    other_pipeline = uuid.uuid4()

    async with db_engine.begin() as conn:
        await conn.execute(
            sa_text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(other_org), "name": f"PG F Org {other_org.hex[:8]}", "slug": f"pgf-{other_org.hex[:8]}"},
        )
        await conn.execute(
            sa_text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {
                "id": str(other_pipeline),
                "oid": str(other_org),
                "name": "PG Foreign Pipeline",
                "uid": str(env.admin_id),
            },
        )

    app_factory = async_sessionmaker(modulo_app_engine, expire_on_commit=False)
    async with app_factory() as s, s.begin():
        await set_rls_org(s, other_org)
        await set_rls_user_context(s, env.admin_id, "admin")
        s.add(
            Eval(
                id=uuid.uuid4(),
                organisation_id=other_org,
                pipeline_id=other_pipeline,
                node_id=None,
                name="foreign-eval",
                eval_type="regex",
                config_json={},
                account_id=env.admin_id,
            )
        )

    async with app_factory() as s, s.begin():
        await set_rls_org(s, env.org_id)
        await set_rls_user_context(s, env.admin_id, "admin")
        visible_count = int((await s.execute(sa_text("SELECT COUNT(*) FROM evals"))).scalar_one())
    assert visible_count == 0, "a foreign-org eval must be invisible under local RLS"


async def test_soft_deleted_eval_dropped_from_pipeline_loader(pg_env: McpEnv, mcp_on: SimpleNamespace) -> None:
    """Criterion 14     after soft delete, the executor's live pipeline loader
    no longer surfaces the eval (the loader's read excludes deleted rows)."""
    from modulo.api.mcp_server import create_eval_definition, delete_eval_definition
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    try:
        created = await create_eval_definition(
            pipeline_id=str(pg_env.pipeline_id),
            node_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, "pg-loading-node")),
            name="pg-fleeting",
            eval_type="guardrail",
            config_json={"action": "observe", "type": "json_schema"},
        )
        eval_id = uuid.UUID(created["id"])
        assert "error" not in created, created

        factory = async_sessionmaker(pg_env.engine, expire_on_commit=False)
        async with factory() as s:
            loaded = await PipelineExecutor._load_eval_defs_for_pipeline(  # type: ignore[arg-type]
                None, s, pg_env.pipeline_id
            )
            names_live = [e.name for e, _gate in loaded]
        assert "pg-fleeting" in names_live

        await delete_eval_definition(eval_id=str(eval_id), hard=False)

        async with factory() as s:
            loaded = await PipelineExecutor._load_eval_defs_for_pipeline(  # type: ignore[arg-type]
                None, s, pg_env.pipeline_id
            )
            names_after = [e.name for e, _gate in loaded]
        assert "pg-fleeting" not in names_after

        row = await pg_env.scalar("SELECT deleted_at IS NOT NULL FROM evals WHERE id = :id", {"id": eval_id})
        assert row is True
    finally:
        mcp_on.teardown()


async def pg_env_sql_count(env: McpEnv, table: str, where: str = "1=1") -> int:
    async with env.engine.connect() as conn:
        raw = (
            await conn.execute(
                sa_text(f"SELECT COUNT(*) FROM {table} WHERE {where}")  # noqa: S608 - test-internal constants only
            )
        ).scalar_one()
    return int(raw)


# ---------------------------------------------------------------------------
# Criteria 10 / 11 - guardrail config-as-code apply (W7) on a real schema
# ---------------------------------------------------------------------------

_GUARDRAIL_YAML_V1 = """
version: 1
guardrails:
  - id: %(gid)s
    name: PG PII Regex
    action: block
    detection:
      type: regex
      pattern: "\\b(SSN)\\b"
      field: output
""".strip()

_GUARDRAIL_YAML_V2 = """
version: 1
guardrails:
  - id: %(gid)s
    name: PG PII Regex
    action: block
    detection:
      type: regex
      pattern: "\\b(SSN|MRN)\\b"
      field: output
""".strip()


async def _reconcile(env: McpEnv, config_yaml: str) -> list[str]:
    """Drive the production W7 reconciliation against a real DB session."""
    from modulo.api.routes.guardrail_config import _reconcile_guardrail_rows
    from modulo.core.guardrails.config import load_config_set

    config_set = load_config_set(config_yaml)
    factory = async_sessionmaker(env.engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        return await _reconcile_guardrail_rows(s, env.org_id, config_set, env.admin_id)


async def test_guardrail_config_apply_persists_eval_without_gate(
    pg_env: McpEnv,
) -> None:
    """Criterion 10 - applying a guardrail config persists an Eval row
    (eval_type=guardrail, suite-scoped) and creates NO PolicyGate row."""
    gid = f"pg-pii-regex-{uuid.uuid4().hex[:8]}"
    yaml_v1 = _GUARDRAIL_YAML_V1 % {"gid": gid}
    colliding = await _reconcile(pg_env, yaml_v1)
    assert colliding == [], f"reconcile must not report collisions: {colliding}"

    row = await pg_env.one("SELECT eval_type, node_id, version FROM evals WHERE name = :n", {"n": gid})
    assert row.eval_type == "guardrail"
    assert row.node_id is None
    assert row.version == 1

    gate_count = int(
        await pg_env.scalar(
            "SELECT COUNT(*) FROM policy_gates WHERE eval_id IN (SELECT id FROM evals WHERE name = :n)",
            {"n": gid},
        )
    )
    assert gate_count == 0, "guardrail evals must never produce a PolicyGate row"

    # Nothing leaks into eval_definitions.
    legacy_twin = int(await pg_env.scalar("SELECT COUNT(*) FROM eval_definitions WHERE name = :n", {"n": gid}))
    assert legacy_twin == 0


async def test_guardrail_config_reapply_upserts_with_version_snapshot(
    pg_env: McpEnv,
) -> None:
    """Criterion 11 - re-applying a modified guardrail config upserts the
    same Eval row: config_json updated, version bumped, pre_version_raw
    snapshots the pre-edit state."""
    gid = f"pg-pii-regex-{uuid.uuid4().hex[:8]}"
    yaml_v1 = _GUARDRAIL_YAML_V1 % {"gid": gid}
    yaml_v2 = _GUARDRAIL_YAML_V2 % {"gid": gid}

    assert not (await _reconcile(pg_env, yaml_v1))

    first = await pg_env.one("SELECT id, config_json FROM evals WHERE name = :n", {"n": gid})
    first_config = dict(first.config_json)

    assert not (await _reconcile(pg_env, yaml_v2))

    second = await pg_env.one(
        "SELECT id, version, config_json, pre_version_raw FROM evals WHERE name = :n",
        {"n": gid},
    )
    assert second.id == first.id, "re-apply must upsert, not duplicate"
    assert second.version == 2, "version must bump on the modified re-apply"
    assert second.pre_version_raw is not None, "pre-edit state must be snapshotted"

    # The pre-edit snapshot carries the ORIGINAL config_json.
    snap_config = second.pre_version_raw.get("config_json") if isinstance(second.pre_version_raw, dict) else None
    assert snap_config == first_config, f"pre_version_raw must capture the pre-edit config {first_config}"
    config = dict(second.config_json)
    assert config["pattern"].find("MRN") >= 0, config

    # Still gate-free after the upsert.
    gate_count = int(
        await pg_env.scalar(
            "SELECT COUNT(*) FROM policy_gates WHERE eval_id IN (SELECT id FROM evals WHERE name = :n)",
            {"n": gid},
        )
    )
    assert gate_count == 0

    # And still exactly one Eval row.
    assert (await pg_env_sql_count(pg_env, "evals", f"name = '{gid}'")) == 1


# ---------------------------------------------------------------------------
# Criteria 14 / 24 - schema-shape, suite-scoped no-gate
# ---------------------------------------------------------------------------


async def test_eval_definitions_table_shape_unchanged(db_engine: AsyncEngine) -> None:
    """Criterion 14 - reflect ``eval_definitions`` from the migrated schema
    and compare against the ORM model's column set (chunk 1's inspect
    assertion carried forward)."""
    from sqlalchemy import inspect

    from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionModel

    def _reflect(sync_conn):
        return sorted(c["name"] for c in inspect(sync_conn).get_columns("eval_definitions"))

    async with db_engine.connect() as conn:
        columns = await conn.run_sync(_reflect)
    expected = sorted(col.name for col in EvalDefinitionModel.__table__.columns)
    assert columns == expected


async def test_policy_gate_action_matches_failure_behaviour(pg_env: McpEnv) -> None:
    """Criterion 17 - non-guardrail W1 creates map ``failure_behaviour`` to
    ``PolicyGate.action`` exactly ('block' and 'warn')."""
    from modulo.core.eval_engine.eval_definition_write import create_or_update_eval
    from modulo.db.models.policy_gate import PolicyGate

    factory = async_sessionmaker(pg_env.engine, expire_on_commit=False)
    for behaviour in ("block", "warn"):
        created_name = f"pg-behaviour-{behaviour}"
        async with factory() as s, s.begin():
            eval_row = await create_or_update_eval(
                s,
                org_id=pg_env.org_id,
                account_id=pg_env.admin_id,
                pipeline_id=pg_env.pipeline_id,
                node_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"pg-action-{behaviour}"),
                name=created_name,
                eval_type="regex",
                config_json={"field": "output", "pattern": "x"},
                failure_behaviour=behaviour,
                pass_threshold=None,
                suite_id=None,
            )
            gate = (await s.execute(select(PolicyGate).where(PolicyGate.eval_id == eval_row.id))).scalar_one_or_none()

        assert gate is not None, f"no PolicyGate row for {created_name}"
        assert gate.action == behaviour, (behaviour, gate.action)


async def test_suite_scoped_non_guardrail_persists_with_no_gate(pg_env: McpEnv) -> None:
    """Criterion 24 - suite-scoped non-guardrail evals persist an Eval row
    but never gate a policy row (helper's branch 3: suite-scoped non-gate)."""
    from modulo.core.eval_engine.eval_definition_write import create_or_update_eval

    factory = async_sessionmaker(pg_env.engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        eval_row = await create_or_update_eval(
            s,
            org_id=pg_env.org_id,
            account_id=pg_env.admin_id,
            pipeline_id=pg_env.pipeline_id,
            node_id=None,
            name="pg-suitescoped",
            eval_type="regex",
            config_json={},
            failure_behaviour="warn",
            pass_threshold=None,
            suite_id="suite-24",
        )
        assert eval_row is not None
        assert eval_row.node_id is None
        assert eval_row.suite_id == "suite-24"

    assert (await pg_env_sql_count(pg_env, "evals", "name = 'pg-suitescoped'")) == 1
    # Only the suite-scoped eval's gates matter; the org table already has
    # live gates from node-scoped evals of other tests on the shared DB.
    gate_count = int(
        await pg_env.scalar(
            "SELECT COUNT(*) FROM policy_gates WHERE eval_id IN (SELECT id FROM evals WHERE name = 'pg-suitescoped')"
        )
    )
    assert gate_count == 0
    # The write must land ONLY in evals; C12's own seeds aside, the suite-24
    # eval must have no legacy twin.
    legacy_twin = int(
        await pg_env.scalar(
            "SELECT COUNT(*) FROM eval_definitions WHERE name = 'pg-suitescoped'",
        )
    )
    assert legacy_twin == 0

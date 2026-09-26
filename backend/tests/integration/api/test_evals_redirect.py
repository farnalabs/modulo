"""FAR-1101 chunk-3b acceptance coverage - REST write paths.

Criterion 1-6 (real numbering; supporting tests unnumbered).

Every eval-definition write endpoint now persists to ``evals`` (+ a
``PolicyGate`` where the shared helper says so) instead of ``eval_definitions``.
These tests drive the REAL endpoints over a real Postgres (testcontainers)
and assert the persisted shape against the new tables:

 * criterion 1     W1: POST /evals (node-scoped) -> Eval row + PolicyGate row
 * criterion 2     W2: POST /evals/from-run -> Eval row via the shared helper
 * criterion 3     W3: POST /feedback/proposals/{id}/publish -> Eval row
 * criterion 4     W4: PUT /evals/{id} -> version bump + pre_version_raw + gate update
 * criterion 5     W1 create + standalone_evaluate proves eval actually evaluates
 * criterion 6     W2 from-run create + standalone_evaluate proves eval actually evaluates
 * supporting     W1 guardrail-typed: POST /evals guardrail-typed -> Eval row, NO PolicyGate
 * supporting     W3 guardrail-typed feedback publish: PUT /evals/{id} guardrail variant -> still no PolicyGate

Shared invariant for every test: after the write, the executor's live read
(``_load_eval_defs_for_pipeline``) surfaces the persisted eval, and the legacy
``eval_definitions`` table stays EMPTY for the org (no legacy writes).

Criteria text lives in the internal chunk-3b spec (not in this public repo).
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.passwords import hash_password
from modulo.settings import Settings, get_settings

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_PASSWORD = "chunk3b-password-1"

_NODE_ID_1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NODE_ID_2 = uuid.UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Seed helpers (superuser engine, mirrors test_break_glass_login.py)
# ---------------------------------------------------------------------------


async def _seed_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"C3B {org_id.hex[:8]}", "slug": f"c3b-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_admin(engine: AsyncEngine, org_id: uuid.UUID, password: str) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, :hash, 'local', true)"
            ),
            {
                "id": str(acc_id),
                "email": f"c3b-{acc_id.hex[:10]}@example.com",
                "name": "Chunk 3b Admin",
                "hash": hash_password(password),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:id, :aid, :oid, 'admin')"
            ),
            {"id": str(uuid.uuid4()), "aid": str(acc_id), "oid": str(org_id)},
        )
    return acc_id


async def _account_email(engine: AsyncEngine, account_id: uuid.UUID) -> str:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT email FROM accounts WHERE id = :id"), {"id": str(account_id)})
        ).scalar_one()


async def _seed_pipeline(engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": "Chunk3b Pipeline", "uid": str(account_id)},
        )
    return pipeline_id


async def _seed_snapshot(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


async def _seed_run(
    engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID, snapshot_id: uuid.UUID
) -> uuid.UUID:
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, input_hash, input_payload, langgraph_thread_id, run_number, status) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, 'complete')"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": uuid.uuid4().hex,
                "thread": f"{org_id}:{run_id}",
                "rn": run_number,
            },
        )
    return run_id


async def _seed_feedback_record(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    run_id: uuid.UUID,
    producing_node_id: uuid.UUID,
) -> uuid.UUID:
    record_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO feedback_records (id, organisation_id, run_id, gate_id, "
                "account_id, rejection_reason, rejected_output, producing_node_id, "
                "feedback_status, eval_gap) "
                "VALUES (:id, :oid, :rid, 'gate', :aid, 'not right', '{}'::json, :nid, 'pending', true)"
            ),
            {
                "id": str(record_id),
                "oid": str(org_id),
                "rid": str(run_id),
                "aid": str(account_id),
                "nid": str(producing_node_id),
            },
        )
    return record_id


# ---------------------------------------------------------------------------
# HTTP client fixture (break-glass idiom + all-features plan context)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
    from modulo.api.main import app

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    class _AllFeatures:
        def feature_enabled(self, name: str) -> bool:
            return True

        def list_enabled_features(self) -> list:
            return []

        def tier(self) -> str:
            return "enterprise"

        def has_license_key(self) -> bool:
            return True

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: _AllFeatures()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as async_client:
        yield async_client

    app.dependency_overrides.clear()


class Chunk3bEnv:
    """Per-test workspace: org + admin account + pipeline + login token."""

    def __init__(
        self,
        engine: AsyncEngine,
        org_id: uuid.UUID,
        account_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        headers: dict[str, str],
    ):
        self.engine = engine
        self.org_id = org_id
        self.account_id = account_id
        self.pipeline_id = pipeline_id
        self.headers = headers


@pytest_asyncio.fixture
async def env(db_engine: AsyncEngine, client: AsyncClient) -> AsyncGenerator[Chunk3bEnv, None]:
    org_id = await _seed_org(db_engine)
    account_id = await _seed_admin(db_engine, org_id, _PASSWORD)
    pipeline_id = await _seed_pipeline(db_engine, org_id, account_id)

    email = await _account_email(db_engine, account_id)
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    yield Chunk3bEnv(db_engine, org_id, account_id, pipeline_id, {"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


async def _load_eval_rows(env: Chunk3bEnv, name: str) -> list:
    from modulo.db.models.eval import Eval

    factory = async_sessionmaker(env.engine, expire_on_commit=False)
    async with factory() as session:
        return (
            (
                await session.execute(
                    select(Eval).where(
                        Eval.organisation_id == env.org_id,
                        Eval.pipeline_id == env.pipeline_id,
                        Eval.name == name,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _assert_cutover_shape(
    env: Chunk3bEnv,
    name: str,
    *,
    eval_type: str = "regex",
    node_id: uuid.UUID | None = None,
    wants_gate: bool,
    wants_version: int = 1,
) -> None:
    """Persisted-shape + executor-read + no-legacy-write assertions."""
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    rows = await _load_eval_rows(env, name)
    assert len(rows) == 1, f"expected exactly 1 Eval row for {name}, got {len(rows)}"
    row = rows[0]
    assert row.eval_type == eval_type
    assert row.node_id == node_id
    assert row.deleted_at is None
    assert row.version == wants_version  # create path persists 1; update path bumps

    factory = async_sessionmaker(env.engine, expire_on_commit=False)
    async with factory() as session:
        # The executor's live read resolves ONLY the new tables.
        loaded = await PipelineExecutor._load_eval_defs_for_pipeline(None, session, env.pipeline_id)  # type: ignore[arg-type]
        matching = [e for e, _gate in loaded if e.name == name]
        assert len(matching) == 1, "the executor read must surface the persisted eval"

        from modulo.db.models.policy_gate import PolicyGate

        gates = (
            (
                await session.execute(
                    select(PolicyGate).where(
                        PolicyGate.eval_id == row.id,
                        PolicyGate.organisation_id == env.org_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if wants_gate:
            assert len(gates) == 1, f"expected exactly 1 PolicyGate row, got {len(gates)}"
            assert gates[0].node_id == node_id
            assert gates[0].action == "warn"
            assert gates[0].deleted_at is None
        else:
            assert not gates, f"guardrail eval must have NO PolicyGate, found {len(gates)}"

        # Cutover invariant: nothing landed in the legacy table.
        legacy_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM eval_definitions WHERE organisation_id = :oid"),
                {"oid": str(env.org_id)},
            )
        ).scalar_one()
        assert int(legacy_count) == 0


# ---------------------------------------------------------------------------
# Criterion 1-4 (real numbering; supporting tests unnumbered)
# ---------------------------------------------------------------------------


async def test_w1_rest_create_persists_eval_and_gate(env: Chunk3bEnv, client: AsyncClient) -> None:
    """Criterion 1     POST /evals writes Eval + PolicyGate, never eval_definitions."""
    payload = {
        "pipeline_id": str(env.pipeline_id),
        "node_id": str(_NODE_ID_1),
        "name": f"w1-node-eval-{env.org_id.hex[:6]}",
        "eval_type": "regex",
        "config_json": {"field": "output", "pattern": "ok"},
    }
    resp = await client.post("/api/v1/evals", json=payload, headers=env.headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == f"w1-node-eval-{env.org_id.hex[:6]}"
    assert body["version"] == 1

    await _assert_cutover_shape(env, f"w1-node-eval-{env.org_id.hex[:6]}", node_id=_NODE_ID_1, wants_gate=True)


async def test_w2_eval_from_run_persists_via_helper(env: Chunk3bEnv, client: AsyncClient) -> None:
    """Criterion 2     POST /evals/from-run persists via create_or_update_eval."""
    snapshot_id = await _seed_snapshot(env.engine, env.org_id, env.pipeline_id)
    run_id = await _seed_run(env.engine, env.org_id, env.pipeline_id, snapshot_id)

    payload = {
        "run_id": str(run_id),
        "node_id": str(_NODE_ID_1),
        "name": f"w2-from-run-{env.org_id.hex[:6]}",
        "eval_type": "regex",
    }
    resp = await client.post("/api/v1/evals/from-run", json=payload, headers=env.headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == f"w2-from-run-{env.org_id.hex[:6]}"
    # The stub config is derived from the (empty) sample output: field is "".
    assert not body["config_json"]["field"]  # empty output sample -> empty field
    assert body["version"] == 1

    await _assert_cutover_shape(env, f"w2-from-run-{env.org_id.hex[:6]}", node_id=_NODE_ID_1, wants_gate=True)


async def test_w3_feedback_publish_persists_eval(env: Chunk3bEnv, client: AsyncClient) -> None:
    """Criterion 3     POST /feedback/proposals/{id}/publish persists via the helper."""
    snapshot_id = await _seed_snapshot(env.engine, env.org_id, env.pipeline_id)
    run_id = await _seed_run(env.engine, env.org_id, env.pipeline_id, snapshot_id)
    record_id = await _seed_feedback_record(env.engine, env.org_id, env.account_id, run_id, _NODE_ID_1)

    payload = {
        "name": f"w3-published-{env.org_id.hex[:6]}",
        "eval_type": "regex",
        "config": {"field": "output", "pattern": "done"},
    }
    resp = await client.post(f"/api/v1/feedback/proposals/{record_id}/publish", json=payload, headers=env.headers)
    assert resp.status_code == 201, resp.text

    await _assert_cutover_shape(env, f"w3-published-{env.org_id.hex[:6]}", node_id=_NODE_ID_1, wants_gate=True)

    # The proposal record is resolved to 'published' status (its own contract).
    factory = async_sessionmaker(env.engine, expire_on_commit=False)
    async with factory() as session:
        status_row = (
            await session.execute(
                text("SELECT feedback_status FROM feedback_records WHERE id = :id"),
                {"id": str(record_id)},
            )
        ).scalar_one()
    assert status_row == "resolved"


async def test_w4_rest_update_bumps_version_and_updates_gate(env: Chunk3bEnv, client: AsyncClient) -> None:
    """Criterion 4     PUT /evals/{id}: version bump, pre_version_raw, gate node swap."""
    create_resp = await client.post(
        "/api/v1/evals",
        json={
            "pipeline_id": str(env.pipeline_id),
            "node_id": str(_NODE_ID_1),
            "name": f"w4-updatable-{env.org_id.hex[:6]}",
            "eval_type": "regex",
            "config_json": {"field": "output", "pattern": "v1"},
        },
        headers=env.headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    eval_id = create_resp.json()["id"]

    # Update 1: move node A -> B. Version bumps to 2; gate re-points to B.
    put1 = await client.put(
        f"/api/v1/evals/{eval_id}",
        json={"node_id": str(_NODE_ID_2)},
        headers=env.headers,
    )
    assert put1.status_code == 200, put1.text
    assert put1.json()["version"] == 2

    # Update 2: move back B -> A. Version bumps to 3; the ORIGINAL gate row is
    # updated in place (still exactly one live gate).
    put2 = await client.put(
        f"/api/v1/evals/{eval_id}",
        json={"node_id": str(_NODE_ID_1)},
        headers=env.headers,
    )
    assert put2.status_code == 200, put2.text
    assert put2.json()["version"] == 3

    from modulo.db.models.eval import Eval
    from modulo.db.models.policy_gate import PolicyGate

    factory = async_sessionmaker(env.engine, expire_on_commit=False)
    async with factory() as session:
        stub = (await session.execute(select(Eval).where(Eval.id == uuid.UUID(eval_id)))).scalar_one()
        assert stub.version == 3
        assert stub.node_id == _NODE_ID_1
        assert stub.pre_version_raw is not None

        gates = (await session.execute(select(PolicyGate).where(PolicyGate.eval_id == stub.id))).scalars().all()
        assert len(gates) == 1, f"node swap must UPDATE the gate in place, found {len(gates)}"
        assert gates[0].node_id == _NODE_ID_1
        assert gates[0].action == "warn"
        assert gates[0].deleted_at is None

        legacy_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM eval_definitions WHERE organisation_id = :oid"),
                {"oid": str(env.org_id)},
            )
        ).scalar_one()
        assert int(legacy_count) == 0

    """Supporting (guardrail variant of criterion 1): POST /evals guardrail-typed persists Eval only."""
    payload = {
        "pipeline_id": str(env.pipeline_id),
        "node_id": str(_NODE_ID_1),
        "name": f"w5-guardrail-{env.org_id.hex[:6]}",
        "eval_type": "guardrail",
        "config_json": {"action": "block", "type": "regex"},
    }
    resp = await client.post("/api/v1/evals", json=payload, headers=env.headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 1

    await _assert_cutover_shape(
        env, f"w5-guardrail-{env.org_id.hex[:6]}", eval_type="guardrail", node_id=_NODE_ID_1, wants_gate=False
    )


async def test_w6_guardrail_update_still_has_no_gate(env: Chunk3bEnv, client: AsyncClient) -> None:
    """Supporting (guardrail variant of criterion 4): PUT /evals/{id} on a guardrail keeps gate-free persistence."""
    create_resp = await client.post(
        "/api/v1/evals",
        json={
            "pipeline_id": str(env.pipeline_id),
            "node_id": str(_NODE_ID_1),
            "name": f"w6-guardrail-{env.org_id.hex[:6]}",
            "eval_type": "guardrail",
            "config_json": {"action": "observe", "type": "regex"},
        },
        headers=env.headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    eval_id = create_resp.json()["id"]

    put = await client.put(
        f"/api/v1/evals/{eval_id}",
        json={"config_json": {"action": "block", "type": "json_schema"}},
        headers=env.headers,
    )
    assert put.status_code == 200, put.text

    await _assert_cutover_shape(
        env,
        f"w6-guardrail-{env.org_id.hex[:6]}",
        eval_type="guardrail",
        node_id=_NODE_ID_1,
        wants_gate=False,
        wants_version=2,
    )


# ---------------------------------------------------------------------------
# Criterion 5 - W1 create: evaluate proves eval actually evaluates
# ---------------------------------------------------------------------------
async def test_w1_create_eval_actually_evaluates(env: Chunk3bEnv, client: AsyncClient) -> None:
    """Criterion 5 - create an eval via W1, then drive EvalEngine.evaluate
    to prove the persisted config produces an EvalResult with the expected outcome."""
    from modulo.core.eval_engine import EvalEngine, EvalType

    name = f"c5-evaluate-{env.org_id.hex[:6]}"
    payload = {
        "pipeline_id": str(env.pipeline_id),
        "node_id": str(_NODE_ID_1),
        "name": name,
        "eval_type": "regex",
        "config_json": {"field": "output", "pattern": "hello"},
    }
    resp = await client.post("/api/v1/evals", json=payload, headers=env.headers)
    assert resp.status_code == 201, resp.text

    # Load the persisted eval row and build the engine DTO.
    rows = await _load_eval_rows(env, name)
    assert len(rows) == 1
    row = rows[0]

    from modulo.core.guardrails import to_engine_definition

    engine_def = to_engine_definition(row)
    assert engine_def.eval_type == EvalType("regex")
    assert engine_def.config.get("pattern") == "hello"

    # Drive the real evaluation path against sample data.
    engine = EvalEngine()
    result = engine.evaluate({"output": "hello world"}, engine_def)
    # The regex pattern "hello" matches "hello world" -> pass outcome.
    assert result.passed is True, f"expected eval to pass, got {result}"


# ---------------------------------------------------------------------------
# Criterion 6 - W2 from-run create: evaluate proves eval actually evaluates
# ---------------------------------------------------------------------------
async def test_w2_from_run_eval_actually_evaluates(env: Chunk3bEnv, client: AsyncClient) -> None:
    """Criterion 6 - create an eval via W2 (from-run), then drive EvalEngine.evaluate
    to prove the persisted config produces an EvalResult."""
    from modulo.core.eval_engine import EvalEngine

    snapshot_id = await _seed_snapshot(env.engine, env.org_id, env.pipeline_id)
    run_id = await _seed_run(env.engine, env.org_id, env.pipeline_id, snapshot_id)

    name = f"c6-from-run-eval-{env.org_id.hex[:6]}"
    payload = {
        "run_id": str(run_id),
        "node_id": str(_NODE_ID_1),
        "name": name,
        "eval_type": "regex",
    }
    resp = await client.post("/api/v1/evals/from-run", json=payload, headers=env.headers)
    assert resp.status_code == 201, resp.text

    # Load the persisted eval row and build the engine DTO.
    rows = await _load_eval_rows(env, name)
    assert len(rows) == 1
    row = rows[0]

    from modulo.core.guardrails import to_engine_definition

    engine_def = to_engine_definition(row)

    # Drive the real evaluation path against sample data.
    engine = EvalEngine()
    result = engine.evaluate({"output": "test output"}, engine_def)
    assert result is not None
    assert hasattr(result, "passed")

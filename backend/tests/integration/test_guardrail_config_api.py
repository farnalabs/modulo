"""Integration tests for the guardrail config-as-code REST API (FAR-219 T3).

Real Postgres (testcontainers) + real migrations + real FastAPI routes. Covers
the propose → apply lifecycle (rows become ``eval_type='guardrail'`` bound to
the org's pipelines), apply-with-no-proposal 409, reject clearing the
proposal, drift detection after a live row is mutated, RLS cross-org
isolation, and the permission gate (viewer cannot propose/apply).
"""

import json
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32

_CONFIG_YAML = """
version: 1
guardrails:
  - id: no-aws-keys
    name: Block AWS keys
    action: block
    detection:
      type: regex
      pattern: 'AKIA[0-9A-Z]{16}'
      field: body
    redaction:
      - path: body
        mode: transform
  - id: valid-payload
    name: Require valid payload
    action: observe
    detection:
      type: json_schema
      schema:
        type: object
        properties:
          body:
            type: string
"""

_CONFIG_YAML_V2 = """
version: 1
guardrails:
  - id: no-aws-keys
    name: Block AWS keys
    action: warn
    detection:
      type: regex
      pattern: 'AKIA[0-9A-Z]{16}'
      field: body
"""


def _token(org_id: uuid.UUID, user_id: uuid.UUID, role: str) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role=role,
    )


def _auth_headers(org_id: uuid.UUID, user_id: uuid.UUID, role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(org_id, user_id, role)}"}


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str, role: str = "admin") -> uuid.UUID:
    async with db_engine.connect() as conn, conn.begin():
        existing = await conn.execute(text("SELECT id FROM accounts WHERE email = :email"), {"email": email})
        row = existing.first()
        if row is not None:
            account_id = uuid.UUID(str(row[0]))
        else:
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, "
                    "auth_provider, active) "
                    "VALUES (:id, :email, :name, 'hash', 'local', true)",
                ),
                {"id": str(account_id), "email": email, "name": email},
            )
        membership = await conn.execute(
            text("SELECT id FROM org_memberships WHERE account_id = :aid AND organisation_id = :oid"),
            {"aid": str(account_id), "oid": str(org_id)},
        )
        if membership.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, :role)",
                ),
                {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id), "role": role},
            )
    return account_id


async def _seed_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID, name: str) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(user_id),
            },
        )
    return pipeline_id


async def _count_guardrail_rows(db_engine: AsyncEngine, org_id: uuid.UUID) -> int:
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT count(*) FROM eval_definitions WHERE organisation_id = :oid AND eval_type = 'guardrail'"),
            {"oid": str(org_id)},
        )
        return int(row.scalar_one())


async def _guardrail_row_names(db_engine: AsyncEngine, org_id: uuid.UUID) -> list[str]:
    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT name FROM eval_definitions "
                "WHERE organisation_id = :oid AND eval_type = 'guardrail' ORDER BY name",
            ),
            {"oid": str(org_id)},
        )
        return [str(r[0]) for r in rows.all()]


@pytest_asyncio.fixture
async def integration_client(db_url: str, db_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: db_engine
    app.dependency_overrides[get_db_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60.0) as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "GR-Config-A")


@pytest_asyncio.fixture
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "GR-Config-B")


@pytest_asyncio.fixture
async def admin_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "gr-admin-a@test.local")


@pytest_asyncio.fixture
async def viewer_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "gr-viewer-a@test.local", role="viewer")


@pytest_asyncio.fixture
async def operator_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "gr-operator-a@test.local", role="operator")


@pytest_asyncio.fixture
async def admin_b(db_engine: AsyncEngine, org_b: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_b, "gr-admin-b@test.local")


@pytest_asyncio.fixture
async def pipeline_a(db_engine: AsyncEngine, org_a: uuid.UUID, admin_a: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_a, admin_a, "GR-Pipeline-A")


@pytest_asyncio.fixture
async def pipeline_b(db_engine: AsyncEngine, org_b: uuid.UUID, admin_b: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_b, admin_b, "GR-Pipeline-B")


async def _propose(client: AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID, yaml_text: str) -> dict:
    resp = await client.post(
        "/api/v1/guardrails/config/propose",
        json={"config_yaml": yaml_text},
        headers=_auth_headers(org_id, user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _apply(client: AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    resp = await client.post(
        "/api/v1/guardrails/config/apply",
        headers=_auth_headers(org_id, user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Propose → apply lifecycle
# ---------------------------------------------------------------------------


async def test_propose_then_apply_creates_guardrail_rows(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    pipeline_a: uuid.UUID,
):
    resp = await integration_client.post(
        "/api/v1/guardrails/config/propose",
        json={"config_yaml": _CONFIG_YAML},
        headers=_auth_headers(org_a, admin_a),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proposed"] is True
    assert body["status"] == "proposed"
    assert len(body["diff"]) == 2
    assert {change["action"] for change in body["diff"]} == {"add"}
    proposal_hash = body["hash"]

    apply_body = await _apply(integration_client, org_a, admin_a)
    assert apply_body["applied"] is True
    assert apply_body["hash"] == proposal_hash
    assert apply_body["status"] == "clean"

    assert await _count_guardrail_rows(db_engine, org_a) == 2
    assert set(await _guardrail_row_names(db_engine, org_a)) == {"no-aws-keys", "valid-payload"}

    # The rows are bound to the org's pipeline so the interception seam picks
    # them up per-pipeline.
    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT pipeline_id, eval_type, config_json FROM eval_definitions "
                "WHERE organisation_id = :oid AND name = 'no-aws-keys'",
            ),
            {"oid": str(org_a)},
        )
        bound = rows.all()
    assert len(bound) == 1
    assert uuid.UUID(str(bound[0][0])) == pipeline_a
    assert bound[0][1] == "guardrail"
    assert bound[0][2]["action"] == "block"
    assert bound[0][2]["type"] == "regex"

    # GET exports the applied snapshot with a clean status.
    get_resp = await integration_client.get(
        "/api/v1/guardrails/config",
        headers=_auth_headers(org_a, admin_a),
    )
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["status"] == "clean"
    assert get_body["hash"] == proposal_hash
    assert "no-aws-keys" in get_body["config_yaml"]


async def test_apply_with_no_proposal_returns_409(
    integration_client: AsyncClient,
    org_b: uuid.UUID,
    admin_b: uuid.UUID,
):
    resp = await integration_client.post(
        "/api/v1/guardrails/config/apply",
        headers=_auth_headers(org_b, admin_b),
    )
    assert resp.status_code == 409


async def test_get_config_echoes_proposal_while_pending(
    integration_client: AsyncClient,
    org_b: uuid.UUID,
    admin_b: uuid.UUID,
):
    # While a proposal is pending (proposed, not yet applied), GET /config must
    # export the PROPOSED YAML the operator is reviewing — not the stale
    # (empty) applied snapshot.
    await _propose(integration_client, org_b, admin_b, _CONFIG_YAML)

    get_body = (
        await integration_client.get(
            "/api/v1/guardrails/config",
            headers=_auth_headers(org_b, admin_b),
        )
    ).json()
    assert get_body["status"] == "proposed"
    assert "no-aws-keys" in get_body["config_yaml"]
    assert "valid-payload" in get_body["config_yaml"]


async def test_reject_clears_proposal(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
):
    await _propose(integration_client, org_a, admin_a, _CONFIG_YAML)

    get_body = (
        await integration_client.get(
            "/api/v1/guardrails/config",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert get_body["status"] == "proposed"

    resp = await integration_client.post(
        "/api/v1/guardrails/config/reject",
        headers=_auth_headers(org_a, admin_a),
    )
    assert resp.status_code == 200
    assert resp.json()["rejected"] is True

    get_body = (
        await integration_client.get(
            "/api/v1/guardrails/config",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert get_body["status"] == "clean"
    assert get_body["hash"] is None

    # Rejecting again (no proposal) is a 409.
    resp = await integration_client.post(
        "/api/v1/guardrails/config/reject",
        headers=_auth_headers(org_a, admin_a),
    )
    assert resp.status_code == 409

    # No guardrail rows were created by the rejected proposal.
    assert await _count_guardrail_rows(db_engine, org_a) == 0


async def test_propose_validates_bad_detection_type(
    integration_client: AsyncClient,
    org_b: uuid.UUID,
    admin_b: uuid.UUID,
):
    bad_yaml = _CONFIG_YAML.replace("type: regex", "type: llm_judge")
    resp = await integration_client.post(
        "/api/v1/guardrails/config/propose",
        json={"config_yaml": bad_yaml},
        headers=_auth_headers(org_b, admin_b),
    )
    assert resp.status_code == 422


async def test_propose_empty_config_rejected(
    integration_client: AsyncClient,
    org_b: uuid.UUID,
    admin_b: uuid.UUID,
):
    resp = await integration_client.post(
        "/api/v1/guardrails/config/propose",
        json={"config_yaml": "   \n"},
        headers=_auth_headers(org_b, admin_b),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


async def test_drift_reported_after_row_mutation(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    pipeline_a: uuid.UUID,
):
    await _propose(integration_client, org_a, admin_a, _CONFIG_YAML)
    applied = await _apply(integration_client, org_a, admin_a)
    applied_hash = applied["hash"]

    # Mutate the live row behind the config layer's back.
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE eval_definitions SET config_json = jsonb_set(config_json::jsonb, "
                "'{pattern}', '\"SK-[0-9A-Za-z]{32}\"'::jsonb) "
                "WHERE organisation_id = :oid AND name = 'no-aws-keys'",
            ),
            {"oid": str(org_a)},
        )

    resp = await integration_client.get(
        "/api/v1/guardrails/config/drift",
        headers=_auth_headers(org_a, admin_a),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "drift"
    assert body["applied_hash"] == applied_hash
    assert body["current_hash"] != applied_hash


async def test_drift_poll_preserves_pending_proposal(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    pipeline_a: uuid.UUID,
):
    # Apply V1, then mutate a live row so the pin drifts.
    await _propose(integration_client, org_a, admin_a, _CONFIG_YAML)
    await _apply(integration_client, org_a, admin_a)
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE eval_definitions SET config_json = jsonb_set(config_json::jsonb, "
                "'{pattern}', '\"SK-[0-9A-Za-z]{32}\"'::jsonb) "
                "WHERE organisation_id = :oid AND name = 'no-aws-keys'",
            ),
            {"oid": str(org_a)},
        )

    drift = (
        await integration_client.get(
            "/api/v1/guardrails/config/drift",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert drift["status"] == "drift"

    # Operator proposes a fix while rows are drifting. A drift poll must not
    # orphan the proposal — the pin stays "proposed" and apply/reject keep
    # working.
    await _propose(integration_client, org_a, admin_a, _CONFIG_YAML_V2)
    drift_again = (
        await integration_client.get(
            "/api/v1/guardrails/config/drift",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert drift_again["status"] == "proposed"

    apply_body = await _apply(integration_client, org_a, admin_a)
    assert apply_body["applied"] is True

    post_apply_drift = (
        await integration_client.get(
            "/api/v1/guardrails/config/drift",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert post_apply_drift["status"] == "clean"


async def test_apply_preserves_node_bound_guardrails(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    pipeline_a: uuid.UUID,
):
    # A guardrail authored via the graph-save flow is bound to a node — it is
    # NOT config-as-code's row, and apply must not delete it.
    node_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO nodes (id, organisation_id, pipeline_id, name, account_id, timeout_seconds) "
                "VALUES (:nid, :oid, :pid, 'eval-node', :aid, 300) "
                "ON CONFLICT (id) DO NOTHING",
            ),
            {"nid": str(node_id), "oid": str(org_a), "pid": str(pipeline_a), "aid": str(admin_a)},
        )
        await conn.execute(
            text(
                "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, node_id, name, "
                "eval_type, config_json, failure_behaviour, account_id) "
                "VALUES (:id, :oid, :pid, :nid, 'graph-node-guard', 'guardrail', :cfg, 'warn', :aid)",
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_a),
                "pid": str(pipeline_a),
                "nid": str(node_id),
                "cfg": json.dumps(
                    {
                        "interception_point": "input",
                        "action": "observe",
                        "type": "regex",
                        "pattern": "graph",
                        "field": "body",
                    }
                ),
                "aid": str(admin_a),
            },
        )

    await _propose(integration_client, org_a, admin_a, _CONFIG_YAML)
    await _apply(integration_client, org_a, admin_a)

    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT name, node_id FROM eval_definitions "
                "WHERE organisation_id = :oid AND eval_type = 'guardrail' ORDER BY name",
            ),
            {"oid": str(org_a)},
        )
        by_name = {str(r[0]): r[1] for r in rows.all()}
    assert "graph-node-guard" in by_name
    assert by_name["graph-node-guard"] is not None
    assert set(by_name) == {"graph-node-guard", "no-aws-keys", "valid-payload"}

    # Drift must read clean right after apply, even with the node-bound row
    # present. The drift boundary must exclude node-bound rows (they are not
    # config-as-code's to own) or this freshly applied, correct config would
    # be reported as permanent drift with no remediation path.
    drift = (
        await integration_client.get(
            "/api/v1/guardrails/config/drift",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert drift["status"] == "clean"
    assert drift["current_hash"] == drift["applied_hash"]

    # GET /config agrees with /drift — the export is also clean.
    get_body = (
        await integration_client.get(
            "/api/v1/guardrails/config",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert get_body["status"] == "clean"


async def test_apply_does_not_clobber_node_bound_row_on_name_collision(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    pipeline_a: uuid.UUID,
):
    # A node-bound guardrail (graph-save flow) whose name collides with a
    # config-as-code id cannot be materialized as an org-level row. Apply must
    # fail closed with a 409 (in-band remediation: rename the config id) — it
    # must NOT report clean and then drift on the very next poll.
    node_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO nodes (id, organisation_id, pipeline_id, name, account_id, timeout_seconds) "
                "VALUES (:nid, :oid, :pid, 'eval-node', :aid, 300) "
                "ON CONFLICT (id) DO NOTHING",
            ),
            {"nid": str(node_id), "oid": str(org_a), "pid": str(pipeline_a), "aid": str(admin_a)},
        )
        await conn.execute(
            text(
                "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, node_id, name, "
                "eval_type, config_json, failure_behaviour, account_id) "
                "VALUES (:id, :oid, :pid, :nid, 'no-aws-keys', 'guardrail', :cfg, 'warn', :aid)",
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_a),
                "pid": str(pipeline_a),
                "nid": str(node_id),
                "cfg": json.dumps(
                    {
                        "interception_point": "input",
                        "action": "observe",
                        "type": "regex",
                        "pattern": "graph-save-pattern",
                        "field": "body",
                    }
                ),
                "aid": str(admin_a),
            },
        )

    await _propose(integration_client, org_a, admin_a, _CONFIG_YAML)

    apply_resp = await integration_client.post(
        "/api/v1/guardrails/config/apply",
        headers=_auth_headers(org_a, admin_a),
    )
    assert apply_resp.status_code == 409, apply_resp.text
    assert "no-aws-keys" in apply_resp.json()["detail"]

    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT node_id, config_json FROM eval_definitions "
                "WHERE organisation_id = :oid AND eval_type = 'guardrail' AND name = 'no-aws-keys'",
            ),
            {"oid": str(org_a)},
        )
        colliding = rows.all()
    # The graph-save row survives apply untouched: still node-bound and still
    # carrying the graph-save-authored config (not the config-as-code content).
    assert len(colliding) == 1
    assert colliding[0][0] is not None
    assert colliding[0][1]["action"] == "observe"
    assert colliding[0][1]["pattern"] == "graph-save-pattern"

    # Apply was a clean no-op: no org-level (node_id IS NULL) row was created
    # for the colliding id, and no phantom drift is reported — the pin is still
    # "proposed", consistent with apply never having run.
    async with db_engine.connect() as conn:
        org_rows = await conn.execute(
            text(
                "SELECT count(*) FROM eval_definitions "
                "WHERE organisation_id = :oid AND eval_type = 'guardrail' AND node_id IS NULL",
            ),
            {"oid": str(org_a)},
        )
        assert int(org_rows.scalar_one()) == 0
    drift = (
        await integration_client.get(
            "/api/v1/guardrails/config/drift",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert drift["status"] == "proposed"

    # The operator resolves the collision by renaming the config id; apply then
    # succeeds clean and drift stays clean (node-bound rows are outside the
    # drift boundary).
    renamed_yaml = _CONFIG_YAML.replace("no-aws-keys", "no-aws-keys-renamed")
    await _propose(integration_client, org_a, admin_a, renamed_yaml)
    apply_body = await _apply(integration_client, org_a, admin_a)
    assert apply_body["applied"] is True
    assert apply_body["status"] == "clean"

    async with db_engine.connect() as conn:
        names = await conn.execute(
            text(
                "SELECT name, node_id FROM eval_definitions "
                "WHERE organisation_id = :oid AND eval_type = 'guardrail' ORDER BY name",
            ),
            {"oid": str(org_a)},
        )
        by_name = {str(r[0]): r[1] for r in names.all()}
    # The node-bound row is preserved and the renamed org-level rows are
    # materialized (node_id NULL).
    assert set(by_name) == {"no-aws-keys", "no-aws-keys-renamed", "valid-payload"}
    assert by_name["no-aws-keys"] is not None
    assert by_name["no-aws-keys-renamed"] is None

    post_resolve_drift = (
        await integration_client.get(
            "/api/v1/guardrails/config/drift",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert post_resolve_drift["status"] == "clean"


async def test_get_config_and_drift_fail_closed_on_legacy_guardrail_name(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    pipeline_a: uuid.UUID,
):
    # A legacy org-level guardrail authored via the direct evals API (shipped
    # with T1 before config-as-code) may carry a name the config id pattern
    # rejects — "Block AWS keys" contains spaces. The read surface must fail
    # closed with a clear message naming the offending guardrail, not a generic
    # validation 422 and never a 500.
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, node_id, name, "
                "eval_type, config_json, failure_behaviour, account_id) "
                "VALUES (:id, :oid, :pid, NULL, 'Block AWS keys', 'guardrail', :cfg, 'warn', :aid)",
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_a),
                "pid": str(pipeline_a),
                "cfg": json.dumps(
                    {
                        "interception_point": "input",
                        "action": "observe",
                        "type": "regex",
                        "pattern": "AKIA[0-9A-Z]{16}",
                        "field": "body",
                    }
                ),
                "aid": str(admin_a),
            },
        )

    for path in ("/api/v1/guardrails/config", "/api/v1/guardrails/config/drift"):
        resp = await integration_client.get(path, headers=_auth_headers(org_a, admin_a))
        assert resp.status_code == 422, resp.text
        assert "Block AWS keys" in resp.json()["detail"]


async def test_drift_clean_without_mutation(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    pipeline_a: uuid.UUID,
):
    # Propose V2 (drops valid-payload) and apply — drift must then be clean
    # and the dropped guardrail must be gone from the live rows (apply
    # reconciles removals, not just upserts).
    resp = await integration_client.post(
        "/api/v1/guardrails/config/propose",
        json={"config_yaml": _CONFIG_YAML_V2},
        headers=_auth_headers(org_a, admin_a),
    )
    assert resp.status_code == 200
    await _apply(integration_client, org_a, admin_a)

    drift = (
        await integration_client.get(
            "/api/v1/guardrails/config/drift",
            headers=_auth_headers(org_a, admin_a),
        )
    ).json()
    assert drift["status"] == "clean"
    assert drift["current_hash"] == drift["applied_hash"]

    assert set(await _guardrail_row_names(db_engine, org_a)) == {"no-aws-keys"}


# ---------------------------------------------------------------------------
# RLS cross-org isolation
# ---------------------------------------------------------------------------


async def test_cross_org_isolation(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    org_b: uuid.UUID,
    admin_b: uuid.UUID,
    pipeline_a: uuid.UUID,
    pipeline_b: uuid.UUID,
):
    # Org A applies a full config.
    await _propose(integration_client, org_a, admin_a, _CONFIG_YAML)
    await _apply(integration_client, org_a, admin_a)

    # Org B sees an empty config — A's applied snapshot and proposal are not
    # visible across orgs.
    get_b = (
        await integration_client.get(
            "/api/v1/guardrails/config",
            headers=_auth_headers(org_b, admin_b),
        )
    ).json()
    assert get_b["status"] == "clean"
    assert get_b["hash"] is None
    assert "no-aws-keys" not in get_b["config_yaml"]

    drift_b = (
        await integration_client.get(
            "/api/v1/guardrails/config/drift",
            headers=_auth_headers(org_b, admin_b),
        )
    ).json()
    assert drift_b["status"] == "clean"

    # Org A's rows are not in org B's pipeline scope.
    assert await _count_guardrail_rows(db_engine, org_b) == 0


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------


async def test_viewer_cannot_propose_or_apply(
    integration_client: AsyncClient,
    org_a: uuid.UUID,
    viewer_a: uuid.UUID,
):
    resp = await integration_client.post(
        "/api/v1/guardrails/config/propose",
        json={"config_yaml": _CONFIG_YAML},
        headers=_auth_headers(org_a, viewer_a, role="viewer"),
    )
    assert resp.status_code == 403

    resp = await integration_client.post(
        "/api/v1/guardrails/config/apply",
        headers=_auth_headers(org_a, viewer_a, role="viewer"),
    )
    assert resp.status_code == 403


async def test_operator_cannot_apply_or_reject(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    admin_a: uuid.UUID,
    operator_a: uuid.UUID,
):
    """An operator holds ``eval.definition.create`` (the permission gate) but is
    NOT an admin — the apply/reject reconcile mutates ``eval_definitions`` rows,
    so it must be gated by the same admin check the direct evals API enforces
    (which returns 403 for a non-admin operator). Without the admin gate the
    operator would get 200 here, a side-channel past the stricter API."""
    await _propose(integration_client, org_a, admin_a, _CONFIG_YAML)

    apply_resp = await integration_client.post(
        "/api/v1/guardrails/config/apply",
        headers=_auth_headers(org_a, operator_a, role="operator"),
    )
    assert apply_resp.status_code == 403

    # The proposal is still pending (apply was denied) — reject must also be
    # denied for the operator, not 200 (or 409 from reaching the reconcile).
    reject_resp = await integration_client.post(
        "/api/v1/guardrails/config/reject",
        headers=_auth_headers(org_a, operator_a, role="operator"),
    )
    assert reject_resp.status_code == 403

    # The denied attempts left no guardrail rows behind.
    assert await _count_guardrail_rows(db_engine, org_a) == 0

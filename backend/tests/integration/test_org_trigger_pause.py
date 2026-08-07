"""Integration tests for the org-wide "pause all pipeline triggers" kill-switch.

Real Postgres (testcontainers) + real migrations + real FastAPI routes. Covers
the deterministic webhook 202-paused contract, the pause PUT endpoint,
cross-tenant isolation, the create_run authority gate (manual runs pass),
redelivery-on-unpause, the running-run immunity, and the widened
``ck_trigger_events_validation_result`` constraint.
"""

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32

# A single flat ``manual`` node (no agent_id) — enough for
# ``create_snapshot_from_live_graph`` to produce a runnable snapshot and for
# the ``POST /api/v1/runs`` basic graph validation (entry node exists) to pass.
_MINIMAL_GRAPH_NODES = [
    {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
    }
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hmac_sign(body: bytes, secret: str, ts: int) -> str:
    payload = f"{ts}.".encode() + body
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _valid_timestamp() -> str:
    return str(int(time.time()))


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


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    async with db_engine.connect() as conn, conn.begin():
        existing = await conn.execute(text("SELECT id FROM accounts WHERE email = :email"), {"email": email})
        row = existing.first()
        if row is not None:
            account_id = uuid.UUID(str(row[0]))
        else:
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, "
                    "auth_provider, active, password_hash) "
                    "VALUES (:id, :email, :name, 'local', true, 'hash')",
                ),
                {"id": str(account_id), "email": email, "name": f"Admin {email}"},
            )
        membership = await conn.execute(
            text(
                "SELECT id FROM org_memberships WHERE account_id = :aid AND organisation_id = :oid",
            ),
            {"aid": str(account_id), "oid": str(org_id)},
        )
        if membership.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, 'admin')",
                ),
                {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
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
                "'{}'::json, (:graph)::json, 'manual_approval', 'org')",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(user_id),
                "graph": json.dumps(_MINIMAL_GRAPH_NODES),
            },
        )
    return pipeline_id


async def _seed_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        version_row = await conn.execute(
            text("SELECT COALESCE(MAX(snapshot_version), 0) FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        snapshot_version = int(version_row.scalar_one()) + 1
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, organisation_id, pipeline_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "composite_bindings_json, run_context_defaults) "
                "VALUES (:id, :oid, :pid, :ver, (:graph)::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '[]'::json, '{}'::json)"
            ),
            {
                "id": str(snapshot_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "ver": snapshot_version,
                "graph": json.dumps(_MINIMAL_GRAPH_NODES),
            },
        )
    return snapshot_id


async def _seed_webhook_trigger(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    user_id: uuid.UUID,
    secret: str,
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (:id, :oid, :pid, 'webhook', true, 5, (:config)::json, :uid)",
            ),
            {
                "id": str(trigger_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "config": json.dumps({"hmac_secret": secret}),
                "uid": str(user_id),
            },
        )
    return trigger_id


async def _set_paused(db_engine: AsyncEngine, org_id: uuid.UUID, paused: bool) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE organisations SET triggers_paused = :paused, "
                "triggers_paused_at = CASE WHEN :paused THEN now() ELSE NULL END "
                "WHERE id = :oid"
            ),
            {"paused": paused, "oid": str(org_id)},
        )


_COUNT_SQL = {
    "trigger_events": "SELECT count(*) FROM trigger_events WHERE organisation_id = :oid",
    "runs": "SELECT count(*) FROM runs WHERE organisation_id = :oid",
    "webhook_dedup_hashes": "SELECT count(*) FROM webhook_dedup_hashes WHERE organisation_id = :oid",
    "pipeline_snapshots": "SELECT count(*) FROM pipeline_snapshots WHERE organisation_id = :oid",
}

_COUNT_PAUSED_SQL = "SELECT count(*) FROM trigger_events WHERE organisation_id = :oid AND validation_result = 'paused'"


async def _count(db_engine: AsyncEngine, table: str, org_id: uuid.UUID) -> int:
    async with db_engine.connect() as conn:
        row = await conn.execute(text(_COUNT_SQL[table]), {"oid": str(org_id)})
        return int(row.scalar_one())


async def _count_paused_events(db_engine: AsyncEngine, org_id: uuid.UUID) -> int:
    async with db_engine.connect() as conn:
        row = await conn.execute(text(_COUNT_PAUSED_SQL), {"oid": str(org_id)})
        return int(row.scalar_one())


async def _pause_via_api(client: AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID, paused: bool) -> None:
    resp = await client.put(
        f"/api/v1/admin/orgs/{org_id}/triggers/pause",
        json={"paused": paused},
        headers=_auth_headers(org_id, user_id),
    )
    assert resp.status_code == 200, resp.text


async def _post_webhook(client: AsyncClient, trigger_id: uuid.UUID, secret: str, body: bytes) -> tuple[int, dict]:
    ts = _valid_timestamp()
    sig = _hmac_sign(body, secret, int(ts))
    resp = await client.post(
        f"/api/v1/triggers/{trigger_id}/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Modulo-Timestamp": ts,
            "X-Modulo-Webhook-Secret": sig,
        },
    )
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    return resp.status_code, payload


# ---------------------------------------------------------------------------
# HTTP client fixture bound to the migrated testcontainer
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fixtures: orgs, users, pipelines, triggers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "Pause-OrgA")


@pytest_asyncio.fixture(scope="module")
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "Pause-OrgB")


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "pause-admin-a@test.local")


@pytest_asyncio.fixture(scope="module")
async def user_b(db_engine: AsyncEngine, org_b: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_b, "pause-admin-b@test.local")


@pytest_asyncio.fixture(scope="module")
async def pipeline_a(db_engine: AsyncEngine, org_a: uuid.UUID, user_a: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_a, user_a, "Pause-PipelineA")


@pytest_asyncio.fixture(scope="module")
async def pipeline_b(db_engine: AsyncEngine, org_b: uuid.UUID, user_b: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_b, user_b, "Pause-PipelineB")


@pytest_asyncio.fixture(scope="module")
async def trigger_a(db_engine: AsyncEngine, org_a: uuid.UUID, pipeline_a: uuid.UUID, user_a: uuid.UUID) -> uuid.UUID:
    return await _seed_webhook_trigger(db_engine, org_a, pipeline_a, user_a, "secret-a-1234567890")


@pytest_asyncio.fixture(scope="module")
async def trigger_b(db_engine: AsyncEngine, org_b: uuid.UUID, pipeline_b: uuid.UUID, user_b: uuid.UUID) -> uuid.UUID:
    return await _seed_webhook_trigger(db_engine, org_b, pipeline_b, user_b, "secret-b-1234567890")


async def _reset_pauses(db_engine: AsyncEngine, org_a: uuid.UUID, org_b: uuid.UUID) -> None:
    await _set_paused(db_engine, org_a, False)
    await _set_paused(db_engine, org_b, False)


@pytest.fixture(autouse=True)
def _no_dispatch() -> Generator[None, None, None]:
    """Never actually dispatch runs during integration webhook tests."""
    with patch("modulo.api.routes.webhooks._dispatch_webhook_run", lambda *a, **k: None):
        yield


# ===================================================================
# 1. Deterministic webhook contract through the REAL route
# ===================================================================


async def test_paused_webhook_returns_202_paused_with_exactly_one_event(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    org_a: uuid.UUID,
    pipeline_a: uuid.UUID,
    trigger_a: uuid.UUID,
    user_a: uuid.UUID,
) -> None:
    await _reset_pauses(db_engine, org_a, uuid.uuid4())

    before_events = await _count(db_engine, "trigger_events", org_a)
    before_snapshots = await _count(db_engine, "pipeline_snapshots", org_a)

    await _pause_via_api(integration_client, org_a, user_a, paused=True)

    body = b'{"event": "push", "ref": "refs/heads/main"}'
    status, payload = await _post_webhook(integration_client, trigger_a, "secret-a-1234567890", body)

    assert status == 202
    assert payload == {"status": "paused"}
    assert "run_id" not in payload

    # FRESH session assertions: exactly one paused event, zero runs, zero dedup
    # hashes, zero snapshots created.
    paused_count = await _count_paused_events(db_engine, org_a)
    assert paused_count == 1
    assert (await _count(db_engine, "trigger_events", org_a)) == before_events + 1
    assert await _count(db_engine, "runs", org_a) == 0
    assert await _count(db_engine, "webhook_dedup_hashes", org_a) == 0
    assert (await _count(db_engine, "pipeline_snapshots", org_a)) == before_snapshots


async def test_paused_replay_returns_202_paused(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    org_a: uuid.UUID,
    trigger_a: uuid.UUID,
    user_a: uuid.UUID,
) -> None:
    await _reset_pauses(db_engine, org_a, uuid.uuid4())
    await _pause_via_api(integration_client, org_a, user_a, paused=True)

    # Seed an accepted original event to replay from.
    event_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO trigger_events (id, organisation_id, trigger_id, trigger_type, "
                "raw_payload_hash, validation_result) "
                "VALUES (:id, :oid, :tid, 'webhook', :hash, 'accepted')"
            ),
            {"id": str(event_id), "oid": str(org_a), "tid": str(trigger_a), "hash": "0" * 64},
        )

    resp = await integration_client.post(
        f"/api/v1/triggers/{trigger_a}/webhook/replay/{event_id}",
        headers=_auth_headers(org_a, user_a),
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "paused"}
    assert await _count(db_engine, "runs", org_a) == 0


# ===================================================================
# 3. Race backstop: engine gate raises, route catch commits one paused event
# ===================================================================


async def test_race_backstop_engine_gate_commits_paused_event(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    org_a: uuid.UUID,
    trigger_a: uuid.UUID,
    user_a: uuid.UUID,
) -> None:
    await _reset_pauses(db_engine, org_a, uuid.uuid4())
    await _pause_via_api(integration_client, org_a, user_a, paused=True)

    before_paused = await _count_paused_events(db_engine, org_a)

    # Route pre-check sees NOT paused (patched False -> TOCTOU window), so the
    # route proceeds to snapshot + handle_webhook, whose REAL gate reads the
    # DB (org paused) and raises — the route's inner catch then commits the
    # paused event. An orphan snapshot may commit on this path (benign).
    body = b'{"event": "push", "ref": "refs/heads/main"}'
    ts = _valid_timestamp()
    sig = _hmac_sign(body, "secret-a-1234567890", int(ts))
    # Neutralise ONLY the route pre-check (a no-op = "saw not paused" -> TOCTOU
    # window opens). The engine's own real gate (ensure_triggers_resumable ->
    # settings_resolver.org_is_paused) still reads the DB, sees the paused org,
    # and raises — the route's inner catch then commits the paused event.
    with patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock):
        resp = await integration_client.post(
            f"/api/v1/triggers/{trigger_a}/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Modulo-Timestamp": ts,
                "X-Modulo-Webhook-Secret": sig,
            },
        )

    assert resp.status_code == 202
    assert resp.json() == {"status": "paused"}
    assert await _count(db_engine, "runs", org_a) == 0
    paused_count = await _count_paused_events(db_engine, org_a)
    assert paused_count == before_paused + 1


# ===================================================================
# 4. Cross-tenant isolation
# ===================================================================


async def test_cross_tenant_paused_a_accepted_b(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    org_a: uuid.UUID,
    org_b: uuid.UUID,
    trigger_a: uuid.UUID,
    trigger_b: uuid.UUID,
) -> None:
    await _reset_pauses(db_engine, org_a, org_b)
    await _set_paused(db_engine, org_a, True)

    # Org A paused -> dropped.
    body_a = b'{"event": "push", "ref": "refs/heads/main"}'
    status_a, payload_a = await _post_webhook(integration_client, trigger_a, "secret-a-1234567890", body_a)
    assert status_a == 202
    assert payload_a == {"status": "paused"}
    assert "run_id" not in payload_a

    # Org B NOT paused -> run created.
    body_b = b'{"event": "push", "ref": "refs/heads/main"}'
    status_b, payload_b = await _post_webhook(integration_client, trigger_b, "secret-b-1234567890", body_b)
    assert status_b == 202
    assert payload_b.get("status") == "accepted"
    assert "run_id" in payload_b

    assert await _count(db_engine, "runs", org_a) == 0
    assert await _count(db_engine, "runs", org_b) == 1
    await _set_paused(db_engine, org_a, False)


# ===================================================================
# 5. Validation-result constraint: full vocabulary + bogus rejection
# ===================================================================


async def test_validation_result_constraint_accepts_full_vocabulary(
    db_engine: AsyncEngine, org_a: uuid.UUID, trigger_a: uuid.UUID
) -> None:
    from modulo.db.models.trigger_event import VALIDATION_RESULT_VALUES

    async with db_engine.connect() as conn:
        for value in VALIDATION_RESULT_VALUES:
            stmt = text(
                "INSERT INTO trigger_events (id, organisation_id, trigger_id, trigger_type, "
                "raw_payload_hash, validation_result) "
                "VALUES (:id, :oid, :tid, 'webhook', :hash, :vr)"
            )
            try:
                await conn.execute(
                    stmt,
                    {
                        "id": str(uuid.uuid4()),
                        "oid": str(org_a),
                        "tid": str(trigger_a),
                        "hash": hashlib.sha256(value.encode()).hexdigest(),
                        "vr": value,
                    },
                )
                await conn.commit()
            except IntegrityError:
                await conn.rollback()
                pytest.fail(f"validation_result '{value}' rejected by constraint")


async def test_validation_result_constraint_rejects_bogus(
    db_engine: AsyncEngine, org_a: uuid.UUID, trigger_a: uuid.UUID
) -> None:
    async with db_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO trigger_events (id, organisation_id, trigger_id, trigger_type, "
                    "raw_payload_hash, validation_result) "
                    "VALUES (:id, :oid, :tid, 'webhook', :hash, 'bogus')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_a),
                    "tid": str(trigger_a),
                    "hash": "0" * 64,
                },
            )
        await conn.commit()


# ===================================================================
# 6. Redelivery on unpause yields exactly one run
# ===================================================================


async def test_redelivery_after_unpause_creates_exactly_one_run(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    org_a: uuid.UUID,
    trigger_a: uuid.UUID,
) -> None:
    await _reset_pauses(db_engine, org_a, uuid.uuid4())

    await _set_paused(db_engine, org_a, True)
    body = b'{"event": "push", "ref": "refs/heads/main"}'
    for _ in range(3):
        status, payload = await _post_webhook(integration_client, trigger_a, "secret-a-1234567890", body)
        assert status == 202
        assert payload == {"status": "paused"}

    assert await _count(db_engine, "runs", org_a) == 0

    # Unpause and redeliver the SAME payload -> exactly one run (no dedup slot
    # was consumed during the pause).
    await _set_paused(db_engine, org_a, False)
    status, payload = await _post_webhook(integration_client, trigger_a, "secret-a-1234567890", body)
    assert status == 202
    assert payload.get("status") == "accepted"
    assert await _count(db_engine, "runs", org_a) == 1


# ===================================================================
# 7. Running run continues during a pause
# ===================================================================


async def test_pause_does_not_mutate_running_run(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    org_a: uuid.UUID,
    pipeline_a: uuid.UUID,
    user_a: uuid.UUID,
) -> None:
    await _reset_pauses(db_engine, org_a, uuid.uuid4())

    run_id = uuid.uuid4()
    snapshot_id = await _seed_snapshot(db_engine, org_a, pipeline_a)
    async with db_engine.connect() as conn, conn.begin():
        run_number_row = await conn.execute(
            text("SELECT COALESCE(MAX(run_number), 0) FROM runs WHERE organisation_id = :oid"),
            {"oid": str(org_a)},
        )
        run_number = int(run_number_row.scalar_one()) + 1
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, status, "
                "trigger_type, run_number, input_hash, langgraph_thread_id, input_payload) "
                "VALUES (:id, :oid, :pid, :sid, 'running', 'manual', :rn, :hash, :thread, '{}'::json)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_a),
                "pid": str(pipeline_a),
                "sid": str(snapshot_id),
                "rn": run_number,
                "hash": "0" * 64,
                "thread": f"{org_a}:{run_id}",
            },
        )

    await _pause_via_api(integration_client, org_a, user_a, paused=True)

    async with db_engine.connect() as conn:
        row = await conn.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": str(run_id)})
        assert row.scalar_one() == "running"


# ===================================================================
# 8. Manual run while paused is created (escape hatch)
# ===================================================================


async def test_manual_run_created_while_paused(
    db_engine: AsyncEngine,
    integration_client: AsyncClient,
    org_a: uuid.UUID,
    pipeline_a: uuid.UUID,
    user_a: uuid.UUID,
) -> None:
    await _reset_pauses(db_engine, org_a, uuid.uuid4())
    await _pause_via_api(integration_client, org_a, user_a, paused=True)

    before = await _count(db_engine, "runs", org_a)
    with patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock):
        resp = await integration_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(pipeline_a), "input_payload": {}},
            headers=_auth_headers(org_a, user_a),
        )

    assert resp.status_code in (201, 202), resp.text
    assert await _count(db_engine, "runs", org_a) == before + 1

"""FAR-1106 chunk-6 real-Postgres acceptance tests for the /policy-gate routes.

Mirrors the HTTP idiom of ``test_evals_redirect.py``: real migrations
(testcontainers Postgres), real ASGI HTTP client, superuser engine for
seeding + white-box row assertions, ``app_engine`` (``SET ROLE
modulo_integration_app``) backing every API call so tenant RLS applies.

Coverage map (internal spec section 7; criteria numbering only):

* criterion 5  cross-tenant eval hidden: POST/GET → 404 "Eval definition
*              not found" with no identifier leakage (spec-5 divergence
*              note: the route's org-filtered eval lookup returns 404
*              BEFORE ``validate_binding`` can ever see org mismatch, so
*              the validator's own cross-tenancy branch stays covered by
*              tests/unit/core/test_chunk3_read_cutover.py instead)
* criterion 6  guardrail-typed eval → NOW BINDABLE (FAR-1107 chunk 8 retired
*              the ``guardrail_eval`` exclusion; POST 201 + PUT 200)
* criterion 7  suite-scoped eval (node_id NULL) → generic 400 (POST);
*              PUT on the (gated-but-invalid) eval → 404 contract
* criterion 8  node_id mismatch (gate vs eval) → generic 400 (PUT), gate row untouched
* criterion 9  structured violation context ONLY in the WARNING log on
*              logger ``modulo.api.routes.evals`` — HTTP body carries the
*              generic message, no exclusion names / org ids / eval id.
*              Triggered via a suite-scoped eval: chunk 8 retired the
*              guardrail_eval exclusion, so it is no longer a POST-path
*              violation
* criterion 10 create → delete → create replaces (exactly one live gate)
* criterion 11 advisory-lock serialization positive + no-lock negative
* criterion 12 DELETE soft-deletes (deleted_at/deleted_by + audit) and
*              re-DELETE → 404; FK RESTRICT proves the hard-delete guard
* criterion 18 version bumps 1→2→3 monotonically with pre_version_raw snapshot

White-box facts used below (verified against
``backend/src/modulo/api/routes/evals.py``): routes mount under ``/api/v1``,
POST→201, PUT/GET→200, DELETE→204, binding 400 detail =
``Policy gate binding is invalid. Check the eval configuration.``
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import get_settings
from modulo.auth.passwords import hash_password
from modulo.settings import Settings

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_PASSWORD = "pg6-password-1"

# Message constants replicated from the route module (private there; expected
# values pinned here so a change in the shipped copy fails loudly).
_MSG_EVAL_NOT_FOUND = "Eval definition not found"
_MSG_GATE_NOT_FOUND = "Policy gate not found for this eval"
_MSG_BINDING_400 = "Policy gate binding is invalid. Check the eval configuration."
_VIOLATION_LOG_MSG = "Policy gate binding violation"
_EVALS_LOGGER = "modulo.api.routes.evals"


# ---------------------------------------------------------------------------
# Seed helpers (superuser engine; mirrors test_evals_redirect.py)
# ---------------------------------------------------------------------------


async def _seed_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"PG6 {org_id.hex[:8]}", "slug": f"pg6-{org_id.hex[:8]}"},
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
                "email": f"pg6-{acc_id.hex[:10]}@example.com",
                "name": "PG6 Admin",
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
            {"id": str(pipeline_id), "oid": str(org_id), "name": "PG6 Pipeline", "uid": str(account_id)},
        )
    return pipeline_id


async def _seed_eval(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    eval_type: str,
    node_id: uuid.UUID | None,
) -> uuid.UUID:
    eval_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO evals (id, organisation_id, pipeline_id, node_id, name, "
                "account_id, eval_type, config_json) "
                "VALUES (:id, :oid, :pid, :nid, :name, :aid, :etype, '{}'::json)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "nid": str(node_id) if node_id is not None else None,
                "name": f"eval-{eval_id.hex[:8]}",
                "aid": str(account_id),
                "etype": eval_type,
            },
        )
    return eval_id


async def _seed_gate(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    eval_id: uuid.UUID,
    node_id: uuid.UUID,
    *,
    action: str = "warn",
) -> uuid.UUID:
    """Direct-SQL gate row (bypassing the route) for PUT-side violation seeds."""
    gate_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, action) "
                "VALUES (:id, :oid, :eid, :nid, :action)"
            ),
            {"id": str(gate_id), "oid": str(org_id), "eid": str(eval_id), "nid": str(node_id), "action": action},
        )
    return gate_id


async def _load_gates(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    eval_id: uuid.UUID,
    *,
    live_only: bool = True,
) -> list[tuple]:
    """White-box gate rows; (id, action, version, deleted_at is not null)."""
    if live_only:
        stmt = text(
            "SELECT id, action, version, deleted_at FROM policy_gates "
            "WHERE organisation_id = :oid AND eval_id = :eid AND deleted_at IS NULL "
            "ORDER BY created_at"
        )
    else:
        stmt = text(
            "SELECT id, action, version, deleted_at FROM policy_gates "
            "WHERE organisation_id = :oid AND eval_id = :eid "
            "ORDER BY created_at"
        )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt, {"oid": str(org_id), "eid": str(eval_id)})).all()
    return [(r[0], r[1], r[2], r[3] is not None) for r in rows]


async def _audit_event_types(engine: AsyncEngine, org_id: uuid.UUID, resource_id: uuid.UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT event_type FROM audit_events "
                    "WHERE organisation_id = :oid AND resource_id = :rid ORDER BY created_at"
                ),
                {"oid": str(org_id), "rid": str(resource_id)},
            )
        ).all()
    return [r[0] for r in rows]


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


class _Env:
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


async def _login(client: AsyncClient, engine: AsyncEngine, account_id: uuid.UUID) -> dict[str, str]:
    email = await _account_email(engine, account_id)
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest_asyncio.fixture
async def env(db_engine: AsyncEngine, client: AsyncClient) -> AsyncGenerator[_Env, None]:
    org_id = await _seed_org(db_engine)
    account_id = await _seed_admin(db_engine, org_id, _PASSWORD)
    pipeline_id = await _seed_pipeline(db_engine, org_id, account_id)
    headers = await _login(client, db_engine, account_id)
    yield _Env(db_engine, org_id, account_id, pipeline_id, headers)


# ---------------------------------------------------------------------------
# criterion 5 — cross-tenant eval hidden, no identifier leakage
# ---------------------------------------------------------------------------


async def test_pg_spec_c5_cross_tenant_eval_is_hidden_with_no_identifier_leakage(
    db_engine: AsyncEngine,
    client: AsyncClient,
    env: _Env,
) -> None:
    """Org B's admin must get 404 "Eval definition not found" on org A's eval.

    GET + POST both return the generic 404 with NO identifier leakage (the
    eval id, org ids, or any cross_* violation name).  This is the API-
    surface contract; the validator's own cross-tenancy branch never fires
    here because the org-filtered eval lookup short-circuits first.
    """
    other_org_id = await _seed_org(db_engine)
    other_acc_id = await _seed_admin(db_engine, other_org_id, _PASSWORD)
    await _seed_pipeline(db_engine, other_org_id, other_acc_id)
    other_headers = await _login(client, db_engine, other_acc_id)

    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=node_id
    )

    got = await client.get(f"/api/v1/evals/{eval_id}/policy-gate", headers=other_headers)
    assert got.status_code == 404, f"GET: {got.status_code} {got.text}"
    assert got.json()["detail"] == _MSG_EVAL_NOT_FOUND

    create = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=other_headers)
    assert create.status_code == 404, create.text
    assert create.json()["detail"] == _MSG_EVAL_NOT_FOUND

    # No identifier leakage in any body
    for resp_text in (create.text,):
        assert str(eval_id) not in resp_text
        assert str(env.org_id) not in resp_text
        assert str(other_org_id) not in resp_text
        assert "cross_tenancy" not in resp_text

    # Nothing was persisted for org B
    gates = await _load_gates(db_engine, env.org_id, eval_id)
    assert len(gates) == 0
    gates_other = await _load_gates(db_engine, other_org_id, eval_id)
    assert len(gates_other) == 0


# ---------------------------------------------------------------------------
# criterion 6 — guardrail-typed eval is now BINDABLE (exclusion retired)
#
# FAR-1107 chunk 8 made the Policy Gate the single enforcement authority for
# guardrails and retired the ``guardrail_eval`` binding exclusion, so an
# otherwise-valid binding on a guardrail-typed eval now succeeds (201/200).
# Criterion 6's original "guardrail eval -> generic 400" contract is retired;
# the residual exclusions are suite-scoped (criterion 7) and node_id mismatch
# (criterion 8), covered at unit level by
# tests/unit/core/eval_engine/test_policy_gate.py::TestC7GuardrailBindingPermitted.
# ---------------------------------------------------------------------------


async def test_pg_spec_c6_guardrail_eval_binds_on_create(
    db_engine: AsyncEngine, client: AsyncClient, env: _Env
) -> None:
    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="guardrail", node_id=node_id
    )

    resp = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=env.headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["action"] == "warn"

    # Chunk 8 binds guardrails, so the gate row persists live
    gates = await _load_gates(db_engine, env.org_id, eval_id, live_only=False)
    assert len(gates) == 1
    assert gates[0][1] == "warn"


async def test_pg_spec_c6_update_variant_guardrail_eval_binds_on_update(
    db_engine: AsyncEngine, client: AsyncClient, env: _Env
) -> None:
    """PUT variant: a pre-seeded gate on a guardrail eval now accepts the update."""
    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="guardrail", node_id=node_id
    )
    gate_id = await _seed_gate(db_engine, env.org_id, eval_id, node_id, action="warn")

    resp = await client.put(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "block"}, headers=env.headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "block"

    # The seeded gate is updated in place (same id, action flipped, version bumped)
    gates = await _load_gates(db_engine, env.org_id, eval_id, live_only=True)
    assert len(gates) == 1
    assert str(gates[0][0]) == str(gate_id)
    assert gates[0][1] == "block"
    assert int(gates[0][2]) == 2


# ---------------------------------------------------------------------------
# criterion 7 — suite-scoped eval (node_id NULL) rejected with generic 400
# ---------------------------------------------------------------------------


async def test_pg_spec_c7_suite_scoped_eval_rejected_on_create(
    db_engine: AsyncEngine, client: AsyncClient, env: _Env
) -> None:
    eval_id = await _seed_eval(db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=None)

    resp = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "block"}, headers=env.headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Policy gate binding is invalid. Check the eval configuration."

    gates = await _load_gates(db_engine, env.org_id, eval_id, live_only=False)
    assert len(gates) == 0

    # PUT contract on the same ungated eval: 404 "Policy gate not found for this eval"
    put = await client.put(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=env.headers)
    assert put.status_code == 404, put.text
    assert put.json()["detail"] == _MSG_GATE_NOT_FOUND


# ---------------------------------------------------------------------------
# criterion 8 — node_id mismatch (gate vs eval) rejected on PUT
# ---------------------------------------------------------------------------


async def test_pg_spec_c8_node_id_mismatch_gate_rejected_on_update(
    db_engine: AsyncEngine, client: AsyncClient, env: _Env
) -> None:
    eval_node = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=eval_node
    )
    drifting_node = uuid.uuid4()
    assert drifting_node != eval_node
    await _seed_gate(db_engine, env.org_id, eval_id, drifting_node, action="warn")

    resp = await client.put(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "block"}, headers=env.headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Policy gate binding is invalid. Check the eval configuration."

    # The seeded gate row is untouched (action and version unchanged)
    gates = await _load_gates(db_engine, env.org_id, eval_id, live_only=True)
    assert len(gates) == 1
    assert gates[0][1] == "warn"
    assert int(gates[0][2]) == 1


# ---------------------------------------------------------------------------
# criterion 9 — structured context only in the WARNING log, generic HTTP 400
# ---------------------------------------------------------------------------


async def test_pg_spec_c9_violation_context_only_in_logs_not_http_body(
    db_engine: AsyncEngine,
    client: AsyncClient,
    env: _Env,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A suite-scoped eval (node_id NULL) is a still-live binding exclusion
    # (FAR-1107 chunk 8 retired only the guardrail_eval exclusion), so it is
    # the POST-path trigger for the structured-violation-log contract.
    eval_id = await _seed_eval(db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=None)

    caplog.set_level(logging.WARNING, logger=_EVALS_LOGGER)
    resp = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=env.headers)
    assert resp.status_code == 400, resp.text

    records = [r for r in caplog.records if r.name == _EVALS_LOGGER and r.getMessage() == _VIOLATION_LOG_MSG]
    assert len(records) == 1, f"expected exactly one violation record, got {records}"
    violations = getattr(records[0], "violations", None)
    assert isinstance(violations, list)
    assert len(violations) >= 1
    assert all(isinstance(v, dict) and "exclusion" in v for v in violations), violations

    body = resp.text
    for banned in (
        "cross_tenancy",
        "suite_scoped_eval",
        "guardrail_eval",
        "node_id_mismatch",
        str(env.org_id),
        str(env.account_id),
        str(eval_id),
    ):
        assert banned not in body, f"leaked {banned!r} in HTTP body: {body}"
    assert body.count("Policy gate binding is invalid. Check the eval configuration.") == 1


# ---------------------------------------------------------------------------
# criterion 10 — create → delete → create replaces (one live gate)
# ---------------------------------------------------------------------------


async def test_pg_spec_c10_recreate_after_delete_leaves_single_live_gate(
    db_engine: AsyncEngine, client: AsyncClient, env: _Env
) -> None:
    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=node_id
    )

    first = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=env.headers)
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    deleted = await client.delete(f"/api/v1/evals/{eval_id}/policy-gate", headers=env.headers)
    assert deleted.status_code == 204, deleted.text

    second = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "block"}, headers=env.headers)
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    assert second_id != first_id

    rows = await _load_gates(db_engine, env.org_id, eval_id, live_only=False)
    assert len(rows) == 2, f"expected 1 live + 1 soft-deleted gate, got {rows}"
    live = [r for r in rows if not r[3]]
    assert len(live) == 1
    assert str(live[0][0]) == second_id
    assert live[0][1] == "block"

    # Re-DELETE soft-deletes the replacement (204), then a further DELETE → 404
    redo = await client.delete(f"/api/v1/evals/{eval_id}/policy-gate", headers=env.headers)
    assert redo.status_code == 204, redo.text
    gone = await client.delete(f"/api/v1/evals/{eval_id}/policy-gate", headers=env.headers)
    assert gone.status_code == 404, gone.text
    assert gone.json()["detail"] == _MSG_GATE_NOT_FOUND


def _principal(env: _Env) -> object:
    from modulo.auth.jwt import TenantPrincipal

    return TenantPrincipal(
        username="pg6-acceptance-admin",
        organisation_id=env.org_id,
        account_id=env.account_id,
        org_role="admin",
    )


# ---------------------------------------------------------------------------
# criterion 11 — advisory-lock serialization (positive + negative)
# ---------------------------------------------------------------------------


async def test_pg_spec_c11_concurrent_creates_serialize_via_advisory_lock(db_engine: AsyncEngine, env: _Env) -> None:
    """Three concurrent gate creators land exactly ONE live gate.

    Direct call to the route's shared helper from three concurrent sessions;
    the transaction-scoped advisory lock must serialize them so the
    partial-unique index never fires.
    """
    from modulo.api.routes import evals as evals_module  # local import; avoid app import at module scope

    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=node_id
    )
    principal = _principal(env)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _writer(action: str) -> str:
        async with factory() as session, session.begin():
            gate = await evals_module._create_or_replace_gate(
                eval_id,
                {"action": action, "node_id": node_id},
                session,
                principal,
            )
            return str(gate.id)

    results = await asyncio.gather(_writer("warn"), _writer("warn"), _writer("warn"))
    assert len(set(results)) == 3, f"each writer must persist its own row, got {results}"

    rows = await _load_gates(db_engine, env.org_id, eval_id, live_only=False)
    live = [r for r in rows if not r[3]]
    assert len(rows) == 3, f"all three writers committed, got {len(rows)}"
    assert len(live) == 1, f"exactly one live gate expected, got {rows}"


async def test_pg_spec_c11_negative_without_lock_second_writer_dies_with_unique_violation(
    db_engine: AsyncEngine,
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the advisory lock the second writer hits the partial-unique index."""
    from modulo.api.routes import evals as evals_module

    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=node_id
    )
    principal = _principal(env)

    async def _bypass(_eval_id: uuid.UUID, _session: AsyncSession, fn: object) -> object:
        return await fn()

    monkeypatch.setattr(evals_module, "_with_gate_advisory_lock", _bypass)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    session_a = factory()
    await session_a.begin()
    await evals_module._create_or_replace_gate(
        eval_id, {"action": "warn", "node_id": node_id}, session_a, principal
    )  # uncommitted

    session_b = factory()
    await session_b.begin()
    task_b = asyncio.create_task(
        evals_module._create_or_replace_gate(eval_id, {"action": "warn", "node_id": node_id}, session_b, principal)
    )
    try:
        await asyncio.sleep(0.5)  # writer B is now blocked on A's uncommitted index entry
        await session_a.commit()  # release B's block → writer B raises UNIQUE violation
        with pytest.raises(IntegrityError):
            await asyncio.wait_for(task_b, timeout=10.0)
    finally:
        if not task_b.done():
            task_b.cancel()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()

    rows = await _load_gates(db_engine, env.org_id, eval_id, live_only=False)
    live = [r for r in rows if not r[3]]
    assert len(live) == 1, f"writer A's gate is the only live row, got {rows}"


# ---------------------------------------------------------------------------
# criterion 12 — DELETE soft-deletes, audit records it, FK RESTRICT guards
# ---------------------------------------------------------------------------


async def test_pg_spec_c12_delete_soft_deletes_row_with_audit_trail(
    db_engine: AsyncEngine, client: AsyncClient, env: _Env
) -> None:
    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=node_id
    )
    created = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=env.headers)
    assert created.status_code == 201, created.text
    gate_id = created.json()["id"]

    resp = await client.delete(f"/api/v1/evals/{eval_id}/policy-gate", headers=env.headers)
    assert resp.status_code == 204, resp.text

    rows = await _load_gates(db_engine, env.org_id, eval_id, live_only=False)
    assert len(rows) == 1
    assert str(rows[0][0]) == str(gate_id)
    assert rows[0][3] is True, "the row must persist soft-deleted, not hard-deleted"

    # deleted_by carries the acting admin's account id
    async with db_engine.connect() as conn:
        deleted_by = (
            await conn.execute(text("SELECT deleted_by FROM policy_gates WHERE id = :id"), {"id": str(gate_id)})
        ).scalar_one()
    assert str(deleted_by) == str(env.account_id)

    # GET now returns the not-found contract
    gone = await client.get(f"/api/v1/evals/{eval_id}/policy-gate", headers=env.headers)
    assert gone.status_code == 404, gone.text
    assert gone.json()["detail"] == _MSG_GATE_NOT_FOUND

    # Audit chain records the deletion
    events = await _audit_event_types(db_engine, env.org_id, gate_id)
    assert "policy_gate.created" in events
    assert "policy_gate.deleted" in events


async def test_pg_spec_c12_fk_restrict_blocks_hard_delete_with_decision_records(
    db_engine: AsyncEngine,
    env: _Env,
) -> None:
    """The policy_gate_decisions FK is ON DELETE RESTRICT: a hard DELETE of
    a gate with decision rows raises IntegrityError at the real Postgres
    level (the DELETE route soft-deletes — see the divergence report where
    the route's 409 cascade branch relies on constraint errors which
    plain UPDATE-soft-delete cannot trigger).
    """
    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=node_id
    )
    gate_id = await _seed_gate(db_engine, env.org_id, eval_id, node_id, action="warn")

    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO policy_gate_decisions (id, organisation_id, policy_gate_id, eval_id, resolved_action) "
                "VALUES (:id, :oid, :gid, :eid, 'warn')"
            ),
            {"id": str(uuid.uuid4()), "oid": str(env.org_id), "gid": str(gate_id), "eid": str(eval_id)},
        )

    async with db_engine.begin() as conn:
        with pytest.raises(IntegrityError, match="policy_gate_decisions"):
            await conn.execute(
                text("DELETE FROM policy_gates WHERE id = :id AND organisation_id = :oid"),
                {"id": str(gate_id), "oid": str(env.org_id)},
            )

    # Both rows survive: the gate AND its decision record
    async with db_engine.connect() as conn:
        gate_count = (
            await conn.execute(text("SELECT count(*) FROM policy_gates WHERE id = :id"), {"id": str(gate_id)})
        ).scalar_one()
        decision_count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM policy_gate_decisions WHERE policy_gate_id = :gid AND organisation_id = :oid"
                ),
                {"gid": str(gate_id), "oid": str(env.org_id)},
            )
        ).scalar_one()
    assert int(gate_count) == 1, "the blocked DELETE must leave the gate row intact"
    assert int(decision_count) == 1, "the decision record must remain attached"


# ---------------------------------------------------------------------------
# criterion 18 — version bumps 1→2→3 monotonically, pre_version_raw snapshot
# ---------------------------------------------------------------------------


async def test_pg_spec_c18_versions_bump_monotonically_with_snapshot(
    db_engine: AsyncEngine, client: AsyncClient, env: _Env
) -> None:
    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=node_id
    )

    first = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=env.headers)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["version"] == 1
    assert body["action"] == "warn"
    assert body["pre_version_raw"] is None  # a fresh gate has nothing to snapshot

    second = await client.put(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "block"}, headers=env.headers)
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["version"] == 2
    assert body2["action"] == "block"
    assert body2["pre_version_raw"] == {"action": "warn"}

    third = await client.put(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=env.headers)
    assert third.status_code == 200, third.text
    body3 = third.json()
    assert body3["version"] == 3
    assert body3["pre_version_raw"] == {"action": "block"}

    # DB state mirrors the API surface
    rows = await _load_gates(db_engine, env.org_id, eval_id, live_only=True)
    assert len(rows) == 1
    assert int(rows[0][2]) == 3

    # GET surfaces the same live state
    got = await client.get(f"/api/v1/evals/{eval_id}/policy-gate", headers=env.headers)
    assert got.status_code == 200, got.text
    assert got.json()["version"] == 3
    assert got.json()["action"] == "warn"

    # Extra contract while here: a 404 for a nonexistent eval id (unknown gate)
    missing = uuid.uuid4()
    got_missing = await client.get(f"/api/v1/evals/{missing}/policy-gate", headers=env.headers)
    assert got_missing.status_code == 404, got_missing.text
    assert got_missing.json()["detail"] == _MSG_EVAL_NOT_FOUND


async def test_pg_spec_toggle_gate_disable_reenable_roundtrip(
    db_engine: AsyncEngine, client: AsyncClient, env: _Env
) -> None:
    """FAR-967 F9 (operator-control acceptance): ``PATCH .../policy-gate/toggle``
    flips ``enabled`` end-to-end against real Postgres — the advisory lock is a
    real ``pg_advisory_xact_lock`` here, the symmetric CHECK invariant holds
    after EACH flip, the response reports the new state, the gate VERSION is
    untouched (a toggle is state, not an edit), and the
    ``policy_gate.toggled`` audit event lands."""
    node_id = uuid.uuid4()
    eval_id = await _seed_eval(
        db_engine, env.org_id, env.pipeline_id, env.account_id, eval_type="regex", node_id=node_id
    )
    created = await client.post(f"/api/v1/evals/{eval_id}/policy-gate", json={"action": "warn"}, headers=env.headers)
    assert created.status_code == 201, created.text
    gate_id = created.json()["id"]
    assert created.json()["enabled"] is True

    async def _db_state() -> tuple[bool, object, object, int]:
        async with db_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT enabled, enabled_at, disabled_at, version FROM policy_gates "
                        "WHERE id = :id AND deleted_at IS NULL"
                    ),
                    {"id": str(gate_id)},
                )
            ).one()
        return row[0], row[1], row[2], int(row[3])

    # ── disable ────────────────────────────────────────────────────────────
    disabled = await client.patch(
        f"/api/v1/evals/{eval_id}/policy-gate/toggle", json={"enabled": False}, headers=env.headers
    )
    assert disabled.status_code == 200, disabled.text
    body = disabled.json()
    assert body["id"] == gate_id
    assert body["enabled"] is False
    assert body["enabled_at"] is None
    assert body["disabled_at"] is not None

    enabled, enabled_at, disabled_at, version = await _db_state()
    assert enabled is False
    assert enabled_at is None
    assert disabled_at is not None, "the disabled state must satisfy the symmetric CHECK"
    assert version == 1, "a toggle must not bump the gate version"

    # ── re-enable ──────────────────────────────────────────────────────────
    reenabled = await client.patch(
        f"/api/v1/evals/{eval_id}/policy-gate/toggle", json={"enabled": True}, headers=env.headers
    )
    assert reenabled.status_code == 200, reenabled.text
    body2 = reenabled.json()
    assert body2["enabled"] is True
    assert body2["enabled_at"] is not None
    assert body2["disabled_at"] is None

    enabled, enabled_at, disabled_at, version = await _db_state()
    assert enabled is True
    assert enabled_at is not None
    assert disabled_at is None, "re-enabling must clear disabled_at (symmetric CHECK)"
    assert version == 1

    # ── audit trail: created + one toggled event per flip ──────────────────
    events = await _audit_event_types(db_engine, env.org_id, gate_id)
    assert "policy_gate.created" in events
    assert events.count("policy_gate.toggled") == 2

"""Integration tests for the FAR-211 agent-loop interior interception (T3).

Drives the loop-interception bridge against REAL Postgres (testcontainers)
under the non-superuser RLS role:

  1. ``load_loop_intercept_guardrails`` loads a pipeline's bound guardrails
     org-scoped (own org visible, foreign org hidden under RLS).
  2. A stub agent command drives a tool-call event through the callback server
     + sandbox-side bridge client -> blocked / warned / redacted outcomes.
  3. Audit records are persisted org-scoped (``guardrail.loop_*`` events).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.core.eval_engine import EvalEngine
from modulo.core.guardrails.loop_intercept import (
    LoopInterceptCallbackServer,
    LoopInterceptConfig,
    load_loop_intercept_guardrails,
    persist_loop_interception_audit,
)
from modulo.core.guardrails.sandbox_bridge import BridgeClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

_SECRET = "ghp_" + "b" * 24


# ---------------------------------------------------------------------------
# Seed helpers (raw SQL, minimal)
# ---------------------------------------------------------------------------


async def _seed_org(engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_account(engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": email, "name": f"Admin {email}"},
        )
    return account_id


async def _seed_pipeline(engine: AsyncEngine, org_id: uuid.UUID, name: str, account_id: uuid.UUID) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    # Suffix with a short uuid so repeated calls within a session (e.g. many
    # tests sharing test_org) never collide on the (organisation_id, name) unique
    # constraint under parallel execution.
    unique_name = f"{name}-{pipeline_id.hex[:8]}"
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": unique_name, "uid": str(account_id)},
        )
    return pipeline_id


async def _seed_guardrail(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    name: str,
    action: str,
) -> uuid.UUID:
    """Seed a regex guardrail firing on ``args.url`` containing a ghp_ token."""
    eval_id = uuid.uuid4()
    config: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "args.url",
        "pattern": r"ghp_[A-Za-z0-9]{20,}",
    }
    if action == "redact":
        config["redaction"] = [{"path": "args.url", "mode": "transform"}]
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, name, "
                "eval_type, config_json, failure_behaviour, account_id) "
                "VALUES (:id, :oid, :pid, :name, 'guardrail', CAST(:cfg AS json), "
                "CAST(:fb AS varchar), :uid)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "name": name,
                "cfg": json.dumps(config),
                "fb": "warn",
                "uid": str(account_id),
            },
        )
    return eval_id


async def _make_session_factory(engine: AsyncEngine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


async def _audit_events_for(engine: AsyncEngine, org_id: uuid.UUID, event_type: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT event_type, payload_json FROM audit_events "
                    "WHERE organisation_id = :oid AND event_type = :et ORDER BY created_at"
                ),
                {"oid": str(org_id), "et": event_type},
            )
        ).fetchall()
    return [{"event_type": r[0], "payload": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# 1. Guardrail row loading under RLS
# ---------------------------------------------------------------------------


async def test_load_loop_intercept_guardrails_same_org_visible(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    pipe = await _seed_pipeline(db_engine, test_org, "Loop-own", test_user)
    await _seed_guardrail(db_engine, test_org, pipe, test_user, name="own-block", action="block")
    factory = await _make_session_factory(app_engine)  # non-superuser role: RLS filters rows
    defs = await load_loop_intercept_guardrails(factory, org_id=test_org, pipeline_id=pipe)
    assert [d.name for d in defs] == ["own-block"]
    assert defs[0].config["action"] == "block"


async def test_load_loop_intercept_guardrails_cross_org_hidden(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    pipe = await _seed_pipeline(db_engine, test_org, "Loop-own", test_user)
    await _seed_guardrail(db_engine, test_org, pipe, test_user, name="own-block", action="block")
    other_org = await _seed_org(db_engine, "Loop-other")
    other_user = await _seed_account(db_engine, other_org, "loop-other@test.local")
    other_pipe = await _seed_pipeline(db_engine, other_org, "Loop-other-pipe", other_user)
    await _seed_guardrail(db_engine, other_org, other_pipe, other_user, name="foreign-block", action="block")

    factory = await _make_session_factory(app_engine)
    # Scoped to test_org's pipeline: the foreign guardrail is invisible under RLS.
    defs = await load_loop_intercept_guardrails(factory, org_id=test_org, pipeline_id=pipe)
    assert [d.name for d in defs] == ["own-block"]
    assert "foreign-block" not in [d.name for d in defs]


async def test_load_loop_intercept_guardrails_zero_guardrails_returns_empty(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    pipe = await _seed_pipeline(db_engine, test_org, "Loop-empty", test_user)
    factory = await _make_session_factory(app_engine)
    defs = await load_loop_intercept_guardrails(factory, org_id=test_org, pipeline_id=pipe)
    assert defs == []


# ---------------------------------------------------------------------------
# 2. Stub agent bridge outcomes (blocked / warned / redacted) + audit
# ---------------------------------------------------------------------------


async def test_stub_agent_blocked_outcome_persists_audit(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    """A block-action guardrail on a tool-call pattern REFUSES the call through
    the bridge, and the ``guardrail.loop_blocked`` audit event is persisted
    org-scoped."""
    pipe = await _seed_pipeline(db_engine, test_org, "Loop-block", test_user)
    await _seed_guardrail(db_engine, test_org, pipe, test_user, name="block-token", action="block")
    factory = await _make_session_factory(app_engine)
    defs = await load_loop_intercept_guardrails(factory, org_id=test_org, pipeline_id=pipe)
    assert defs

    run_id = uuid.uuid4()
    server = LoopInterceptCallbackServer(
        EvalEngine(),
        defs,
        LoopInterceptConfig(),
        audit_sink=lambda records: persist_loop_interception_audit(
            factory, org_id=test_org, run_id=run_id, node_id="n1", records=records
        ),
    )
    port = await server.start()
    try:

        def _stub_agent_call() -> tuple[bool, dict | None, str]:
            client = BridgeClient(f"http://127.0.0.1:{port}", timeout=5.0)
            return client.decide_before("git push", {"url": f"https://x-access-token:{_SECRET}@github.com/a/b.git"})

        allowed, masked, action = await asyncio.to_thread(_stub_agent_call)
        assert allowed is False
        assert action == "block"
        assert masked is None
    finally:
        await server.close()

    await asyncio.sleep(0.1)
    events = await _audit_events_for(db_engine, test_org, "guardrail.loop_blocked")
    assert events, "no guardrail.loop_blocked audit event persisted"
    payload = events[0]["payload"]
    assert payload["tool"] == "git push"
    assert payload["guardrail"] == "block-token"
    assert _SECRET not in json.dumps(payload)


async def test_stub_agent_warned_outcome_proceeds(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    pipe = await _seed_pipeline(db_engine, test_org, "Loop-warn", test_user)
    await _seed_guardrail(db_engine, test_org, pipe, test_user, name="warn-token", action="warn")
    factory = await _make_session_factory(app_engine)
    defs = await load_loop_intercept_guardrails(factory, org_id=test_org, pipeline_id=pipe)

    run_id = uuid.uuid4()
    server = LoopInterceptCallbackServer(
        EvalEngine(),
        defs,
        LoopInterceptConfig(),
        audit_sink=lambda records: persist_loop_interception_audit(
            factory, org_id=test_org, run_id=run_id, node_id="n1", records=records
        ),
    )
    port = await server.start()
    try:

        def _stub_agent_call() -> tuple[bool, dict | None, str]:
            client = BridgeClient(f"http://127.0.0.1:{port}", timeout=5.0)
            return client.decide_before("git push", {"url": f"https://x-access-token:{_SECRET}@github.com/a/b.git"})

        allowed, _, action = await asyncio.to_thread(_stub_agent_call)
        assert allowed is True
        assert action == "warn"
    finally:
        await server.close()

    await asyncio.sleep(0.1)
    events = await _audit_events_for(db_engine, test_org, "guardrail.loop_warned")
    assert events


async def test_stub_agent_redacted_outcome_masks_args(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    pipe = await _seed_pipeline(db_engine, test_org, "Loop-redact", test_user)
    await _seed_guardrail(db_engine, test_org, pipe, test_user, name="redact-url", action="redact")
    factory = await _make_session_factory(app_engine)
    defs = await load_loop_intercept_guardrails(factory, org_id=test_org, pipeline_id=pipe)

    server = LoopInterceptCallbackServer(EvalEngine(), defs, LoopInterceptConfig())
    port = await server.start()
    try:

        def _stub_agent_call() -> tuple[bool, dict | None, str]:
            client = BridgeClient(f"http://127.0.0.1:{port}", timeout=5.0)
            return client.decide_before("git push", {"url": f"https://x-access-token:{_SECRET}@github.com/a/b.git"})

        allowed, masked, action = await asyncio.to_thread(_stub_agent_call)
        assert allowed is True
        assert action == "redact"
        assert masked is not None
        assert _SECRET not in masked["url"]
    finally:
        await server.close()


async def test_stub_agent_pass_outcome_for_low_risk_tool(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    pipe = await _seed_pipeline(db_engine, test_org, "Loop-pass", test_user)
    await _seed_guardrail(db_engine, test_org, pipe, test_user, name="block-token", action="block")
    factory = await _make_session_factory(app_engine)
    defs = await load_loop_intercept_guardrails(factory, org_id=test_org, pipeline_id=pipe)

    server = LoopInterceptCallbackServer(EvalEngine(), defs, LoopInterceptConfig())
    port = await server.start()
    try:

        def _stub_agent_call() -> tuple[bool, dict | None, str]:
            client = BridgeClient(f"http://127.0.0.1:{port}", timeout=5.0)
            return client.decide_before("git status", {"repo": "/tmp/repo"})

        allowed, masked, action = await asyncio.to_thread(_stub_agent_call)
        assert allowed is True
        assert masked is None
        assert action == "pass"
    finally:
        await server.close()

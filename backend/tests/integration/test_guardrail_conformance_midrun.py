"""Integration tests for the FAR-215 mid-run capability re-check.

Drives the node-start conformance seam against REAL Postgres (testcontainers)
under the non-superuser RLS role:

  1. ``check_node_start`` manifest/decision paths — present proceeds, absent
     blocks (fail closed), unknown blocks (fail closed), advisory never blocks.
  2. A full ``PipelineExecutor.execute`` run whose bound block-action guardrail
     declares a capability the node's bound surface no longer provides — the
     node is BLOCKED and the run routes to ``awaiting_human`` (HITL), never a
     silent abort or a fail-open continuation.
  3. The same run WITHOUT the conformance claim completes normally (zero-claim
     fast path through the real node seam).
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import BaseMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import modulo.core.pipeline_execution as pe
from modulo.core.guardrails.conformance import check_node_start
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_model_backend_hub
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


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


async def _seed_pipeline(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    max_concurrent: int = 10,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, :mcr, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(account_id),
                "mcr": max_concurrent,
            },
        )
    return pipeline_id


async def _seed_snapshot(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    graph: dict,
    *,
    environment_profile_id: uuid.UUID | None = None,
) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json, environment_profile_id) "
                "VALUES (:id, :pid, :oid, 1, CAST(:graph AS json), '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json, :env)"
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "graph": json.dumps(graph),
                "env": str(environment_profile_id) if environment_profile_id is not None else None,
            },
        )
    return snapshot_id


async def _seed_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    status: str = "pending",
    claim_token: str | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    base_params: dict[str, object] = {
        "id": str(run_id),
        "oid": str(org_id),
        "pid": str(pipeline_id),
        "sid": str(snapshot_id),
        "ih": uuid.uuid4().hex,
        "thread": f"{org_id}:{run_id}",
        "rn": run_number,
        "st": status,
    }
    async with engine.connect() as conn, conn.begin():
        if claim_token is None:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                    "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                    "run_number, status) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :st)"
                ),
                base_params,
            )
        else:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                    "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                    "run_number, status, claim_token) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :st, :tok)"
                ),
                {**base_params, "tok": claim_token},
            )
    return run_id


async def _seed_guardrail(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    name: str,
    action: str,
    required_capabilities: list[str] | None = None,
) -> uuid.UUID:
    eval_id = uuid.uuid4()
    config: dict[str, Any] = {"action": action, "interception_point": "input"}
    if required_capabilities is not None:
        config["required_capabilities"] = required_capabilities
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
                "fb": "block" if action == "block" else "warn",
                "uid": str(account_id),
            },
        )
    return eval_id


async def _seed_environment_profile(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    capabilities: list[str],
    status: str = "active",
) -> uuid.UUID:
    profile_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO environment_profiles (id, organisation_id, account_id, name, "
                "provider_type, capabilities_json, config_json, network_policy, "
                "initialisation_strategy, secret_refs_json, persistence_policy, status) "
                "VALUES (:id, :oid, :uid, :name, 'e2b', CAST(:caps AS json), '{}'::json, "
                "'outbound', 'git_clone', '[]'::json, 'ephemeral', :status)"
            ),
            {
                "id": str(profile_id),
                "oid": str(org_id),
                "uid": str(account_id),
                "name": f"profile-{profile_id.hex[:6]}",
                "caps": json.dumps(capabilities),
                "status": status,
            },
        )
    return profile_id


async def _run_status(engine: AsyncEngine, run_id: uuid.UUID) -> str:
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT status FROM runs WHERE id=:rid"), {"rid": str(run_id)})).fetchone()
    assert row is not None
    return row[0]


class _StubAdapter(ModelBackendBase):
    """Adapts StubModelBackend (BaseChatModel) to ModelBackendBase async invoke."""

    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "stub"


def _one_agent_graph(node_id: str, backend_id: str) -> dict:
    return {
        "nodes": [
            {
                "id": node_id,
                "agent_id": str(uuid.uuid4()),
                "role": "agent",
                "prompt_template": "Hello {{ state.run_context.input.name }}",
                "model_backend_id": backend_id,
            },
        ],
        "edges": [],
    }


async def _make_session_factory(engine: AsyncEngine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


async def _dispatch_and_execute(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    migrated_db_url: str,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    run_id: uuid.UUID,
    backend_id: str,
) -> str:
    """Dispatch -> RLS claim -> real executor -> return final run status."""
    from modulo.core import dispatch as dispatch_mod
    from modulo.core.pipeline_engine.executor import PipelineExecutor
    from modulo.settings import get_settings

    enqueue_stub = AsyncMock(return_value=("saq:job:runs:midrun", False))
    with patch("modulo.core.dispatch._enqueue_saq", new=enqueue_stub):
        outcome, _job_id = await dispatch_mod.dispatch_run(str(run_id), str(org_id))
    assert outcome == "enqueued"

    claim_token = await pe.claim_run_async(app_engine, str(run_id), str(org_id))
    assert claim_token is not None

    hub = ModelBackendHub()
    await hub.__aenter__()
    hub.register(
        uuid.UUID(backend_id),
        _StubAdapter({"Hello World": json.dumps({"greeting": "Hello, World!"})}),
    )
    set_model_backend_hub(hub)

    settings = get_settings()
    conn_string = str(settings.database_url).replace("+asyncpg", "").replace("+psycopg", "")
    executor = PipelineExecutor(db_engine, checkpointer_conn_string=conn_string)
    try:
        final = await executor.execute(
            run_id=run_id,
            org_id=org_id,
            input_payload={"name": "World"},
            claim_token=claim_token,
        )
        return final.status
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 1. check_node_start against real Postgres (RLS) — manifest/decision paths
# ---------------------------------------------------------------------------


async def test_check_node_start_present_proceeds(db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID):
    factory = await _make_session_factory(db_engine)
    pipe = await _seed_pipeline(db_engine, test_org, "CNS-Present", test_user)
    profile = await _seed_environment_profile(db_engine, test_org, test_user, capabilities=["sandbox.e2b"])
    await _seed_guardrail(
        db_engine, test_org, pipe, test_user, name="block-cap", action="block", required_capabilities=["sandbox.e2b"]
    )
    result = await check_node_start(
        factory,
        org_id=test_org,
        pipeline_id=pipe,
        node_id="n1",
        connector_instance_ids=[],
        environment_profile_id=profile,
        agent_id=None,
    )
    assert result.blocked is False
    assert result.state == "present"


async def test_check_node_start_absent_blocks_fail_closed(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    factory = await _make_session_factory(db_engine)
    pipe = await _seed_pipeline(db_engine, test_org, "CNS-Absent", test_user)
    # Active profile WITHOUT the required capability -> confirmed absent.
    profile = await _seed_environment_profile(db_engine, test_org, test_user, capabilities=["git"])
    await _seed_guardrail(
        db_engine, test_org, pipe, test_user, name="block-cap", action="block", required_capabilities=["sandbox.e2b"]
    )
    result = await check_node_start(
        factory,
        org_id=test_org,
        pipeline_id=pipe,
        node_id="n1",
        connector_instance_ids=[],
        environment_profile_id=profile,
        agent_id=None,
    )
    assert result.blocked is True
    assert result.state == "unknown"


async def test_check_node_start_inactive_profile_blocks_fail_closed(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    factory = await _make_session_factory(db_engine)
    pipe = await _seed_pipeline(db_engine, test_org, "CNS-Inactive", test_user)
    # Profile deactivated mid-run -> its capabilities are no longer granted.
    profile = await _seed_environment_profile(
        db_engine, test_org, test_user, capabilities=["sandbox.e2b"], status="inactive"
    )
    await _seed_guardrail(
        db_engine, test_org, pipe, test_user, name="block-cap", action="block", required_capabilities=["sandbox.e2b"]
    )
    result = await check_node_start(
        factory,
        org_id=test_org,
        pipeline_id=pipe,
        node_id="n1",
        connector_instance_ids=[],
        environment_profile_id=profile,
        agent_id=None,
    )
    assert result.blocked is True
    assert result.state == "unknown"


async def test_check_node_start_advisory_never_blocks(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
):
    factory = await _make_session_factory(db_engine)
    pipe = await _seed_pipeline(db_engine, test_org, "CNS-Warn", test_user)
    profile = await _seed_environment_profile(db_engine, test_org, test_user, capabilities=["git"])
    await _seed_guardrail(
        db_engine, test_org, pipe, test_user, name="warn-cap", action="warn", required_capabilities=["sandbox.e2b"]
    )
    result = await check_node_start(
        factory,
        org_id=test_org,
        pipeline_id=pipe,
        node_id="n1",
        connector_instance_ids=[],
        environment_profile_id=profile,
        agent_id=None,
    )
    assert result.blocked is False
    assert result.state == "unknown"


async def test_check_node_start_zero_claim_fast_path(db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID):
    factory = await _make_session_factory(db_engine)
    pipe = await _seed_pipeline(db_engine, test_org, "CNS-Fast", test_user)
    await _seed_guardrail(db_engine, test_org, pipe, test_user, name="plain", action="block", required_capabilities=[])
    result = await check_node_start(
        factory,
        org_id=test_org,
        pipeline_id=pipe,
        node_id="n1",
        connector_instance_ids=[],
        environment_profile_id=None,
        agent_id=None,
    )
    assert result.blocked is False
    assert result.claimed is False


# ---------------------------------------------------------------------------
# 2. Full executor run: block -> awaiting_human (HITL), never fail-open
# ---------------------------------------------------------------------------


async def test_full_run_conformance_block_routes_to_hitl(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """A bound block-action guardrail whose required capability is absent at
    node start blocks the node and routes the run to ``awaiting_human`` — never
    a silent abort and never a fail-open completion."""
    org_id = await _seed_org(db_engine, "MidrunBlock")
    account_id = await _seed_account(db_engine, org_id, "midrun-block@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeMidrunBlock", account_id)
    node_id = "n1"
    backend_id = str(uuid.uuid4())
    # The node's bound EnvironmentProfile does NOT grant the capability the
    # block guardrail requires -> the node must be blocked at start.
    profile = await _seed_environment_profile(db_engine, org_id, account_id, capabilities=["git"])
    snap = await _seed_snapshot(
        db_engine, org_id, pipe, _one_agent_graph(node_id, backend_id), environment_profile_id=profile
    )
    await _seed_guardrail(
        db_engine,
        org_id,
        pipe,
        account_id,
        name="block-cap",
        action="block",
        required_capabilities=["sandbox.e2b"],
    )
    run_id = await _seed_run(db_engine, org_id, pipe, snap, status="pending")

    final_status = await _dispatch_and_execute(
        db_engine, app_engine, migrated_db_url, org_id, pipe, snap, run_id, backend_id
    )
    assert final_status == "awaiting_human"
    assert await _run_status(db_engine, run_id) == "awaiting_human"


async def test_full_run_no_conformance_claim_completes(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """Without a conformance claim (zero-claim fast path through the real node
    seam) the run completes normally — the re-check adds no false positives."""
    org_id = await _seed_org(db_engine, "MidrunFast")
    account_id = await _seed_account(db_engine, org_id, "midrun-fast@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeMidrunFast", account_id)
    node_id = "n1"
    backend_id = str(uuid.uuid4())
    snap = await _seed_snapshot(db_engine, org_id, pipe, _one_agent_graph(node_id, backend_id))
    await _seed_guardrail(
        db_engine, org_id, pipe, account_id, name="plain-block", action="block", required_capabilities=[]
    )
    run_id = await _seed_run(db_engine, org_id, pipe, snap, status="pending")

    final_status = await _dispatch_and_execute(
        db_engine, app_engine, migrated_db_url, org_id, pipe, snap, run_id, backend_id
    )
    assert final_status == "complete"
    assert await _run_status(db_engine, run_id) == "complete"

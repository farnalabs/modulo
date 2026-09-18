"""HITL resume round-trip (dist/runtime-reconcile B1) — real Postgres.

A pipeline with a HITL gate runs until ``awaiting_human``; a human commits a
decision WITH a ``decision_payload``; the run is resumed through the real
``resume_run`` seam with the reconstructed payload. The committed decision is
reconstructed VERBATIM (a committed rejection must resume as rejected — the
live-bug fix — never as an empty ``{}`` that auto-approves the gate).

The checkpointer (ModuloPostgresSaver) is used throughout, so interrupts and
resume use the real persisted LangGraph checkpoint.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import modulo.core.pipeline_execution as pe
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_model_backend_hub
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


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
    ) -> AsyncIterator[BaseMessage]:
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "stub"


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
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": name, "uid": str(account_id)},
        )
    return pipeline_id


def _hitl_graph(node_a: str, node_b: str, backend_id: str, gate_config: dict) -> dict:
    return {
        "nodes": [
            {
                "id": node_a,
                "agent_id": str(uuid.uuid4()),
                "role": "agent",
                "prompt_template": "Hello {{ state.run_context.input.name }}",
                "model_backend_id": backend_id,
            },
            {
                "id": node_b,
                "agent_id": str(uuid.uuid4()),
                "role": "agent",
                "prompt_template": "Bye {{ state.run_context.input.name }}",
                "model_backend_id": backend_id,
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": node_a,
                "target": node_b,
                "type": "normal",
                "hitl_gate_config": gate_config,
            },
        ],
    }


async def _seed_snapshot(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    graph: dict,
) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, CAST(:graph AS json), '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "graph": json.dumps(graph),
            },
        )
    return snapshot_id


async def _seed_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                "run_number, status) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, 'pending')"
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


async def _run_status(engine: AsyncEngine, run_id: uuid.UUID) -> tuple[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT status, completed_at FROM runs WHERE id=:rid"), {"rid": str(run_id)})
        ).fetchone()
    assert row is not None
    return row[0], row[1]


async def _read_gate_id(engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT gate_id FROM hitl_claims WHERE organisation_id=:oid AND run_id=:rid LIMIT 1"),
                {"oid": str(org_id), "rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None, "no HITL gate was created for the interrupted run"
    return str(row[0])


async def _commit_decision(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    gate_id: str,
    *,
    decision: str,
    decision_payload: dict[str, Any],
) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE hitl_claims SET decision=:d, decision_at=now(), "
                "decision_payload=CAST(:p AS json) "
                "WHERE organisation_id=:oid AND run_id=:rid AND gate_id=:gid"
            ),
            {
                "d": decision,
                "p": json.dumps(decision_payload),
                "oid": str(org_id),
                "rid": str(run_id),
                "gid": gate_id,
            },
        )


def _run_executor_hub(backend_id: uuid.UUID, fixtures: dict[str, str]):
    hub = ModelBackendHub()

    async def _aenter() -> ModelBackendHub:
        await hub.__aenter__()
        hub.register(backend_id, _StubAdapter(fixtures))
        set_model_backend_hub(hub)
        return hub

    return _aenter


async def _interrupt_run(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    backend_id: uuid.UUID,
    fixtures: dict[str, str],
) -> None:
    """Claim + execute until the gate interrupts; run must reach awaiting_human."""
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    claim_token = await pe.claim_run_async(db_engine, str(run_id), str(org_id))
    assert claim_token is not None

    # Build the checkpointer DSN from the migrated-testcontainer fixture rather
    # than get_settings().database_url: the fixture is the DB this test actually
    # seeded, so the checkpointer cannot be sent to a different (or dead) server
    # if some earlier test leaves DATABASE_URL pointing elsewhere.
    conn_string = migrated_db_url.replace("+asyncpg", "").replace("+psycopg", "")
    executor = PipelineExecutor(db_engine, checkpointer_conn_string=conn_string)
    setup_hub = _run_executor_hub(backend_id, fixtures)
    hub = await setup_hub()
    try:
        final = await executor.execute(
            run_id=run_id,
            org_id=org_id,
            input_payload={"name": "World"},
            claim_token=claim_token,
        )
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)

    assert final.status == "awaiting_human", f"expected awaiting_human interrupt, got {final.status}"


async def _gate_result(gate_config: dict, payload: dict[str, Any]) -> dict:
    """Invoke the HITL gate node directly with the committed decision and return
    its artifact — the exact routing a resume applies."""
    from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

    gate_fn = make_hitl_gate_fn(gate_config)
    result = await gate_fn({"_hitl_decision": payload, "run_context": {"input": {}}})
    return result["artifacts"][0]


async def _resume_and_complete(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    backend_id: uuid.UUID,
    fixtures: dict[str, str],
    resume_data: dict[str, Any],
) -> None:
    """Dispatch resume_run with the reconstructed payload; the run must complete."""
    setup_hub = _run_executor_hub(backend_id, fixtures)
    hub = await setup_hub()
    try:
        outcome = await pe.resume_run(
            async_engine=db_engine,
            run_id=str(run_id),
            org_id=str(org_id),
            resume_data=resume_data,
        )
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)

    assert outcome.get("status") == "complete", f"resume_run outcome: {outcome}"


async def test_hitl_resume_roundtrip_approve_with_modification(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """Approve-with-modification round-trip: the committed payload (including
    the human's modified output) is reconstructed verbatim and the resume runs
    the gate to completion as APPROVED with that output."""
    from modulo.core.cron_helpers import _committed_decision_resume_data

    org_id = await _seed_org(db_engine, "HitlApprove")
    account_id = await _seed_account(db_engine, org_id, "hitl-approve@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeHitlApprove", account_id)
    backend_id = uuid.uuid4()
    # FAR-541: the gate id is deterministic (hitl_gate_<source>_<target>); the
    # decision payload must carry the SAME stamp the per-gate consumer checks.
    gate_config: dict[str, Any] = {
        "gate_id": "hitl_gate_a_b",
        "human_only": True,
        "overdue_threshold_minutes": 60,
        "required_team_id": None,
    }
    node_a, node_b = "a", "b"
    snap = await _seed_snapshot(db_engine, org_id, pipe, _hitl_graph(node_a, node_b, str(backend_id), gate_config))
    run_id = await _seed_run(db_engine, org_id, pipe, snap)

    fixtures = {
        "Hello World": json.dumps({"greeting": "hi"}),
        "Bye World": json.dumps({"farewell": "bye"}),
    }
    await _interrupt_run(db_engine, migrated_db_url, org_id, run_id, backend_id, fixtures)

    status, _ = await _run_status(db_engine, run_id)
    assert status == "awaiting_human"

    gate_id = await _read_gate_id(db_engine, org_id, run_id)
    # FAR-541 (iteration 3): the real writer contract — approve-with-modification
    # (routes/hitl.py) submits action "approved" plus a "modified_output" member;
    # the retired "approved_with_modification" action was never produced by any
    # writer and the gate consumer fails closed on it.
    payload = {
        "action": "approved",
        "gate_id": gate_id,
        "modified_output": {"answer": "human-edited"},
    }
    await _commit_decision(db_engine, org_id, run_id, gate_id, decision="approved", decision_payload=payload)

    # B1-reconcile: the reconstructed resume payload is the EXACT committed
    # decision, never {} (which would silently drop the human's modification).
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, org_id)
        reconstructed = await _committed_decision_resume_data(session, org_id, run_id)
    assert reconstructed == payload, f"resume data must be the exact committed decision, got {reconstructed}"

    await _resume_and_complete(db_engine, migrated_db_url, org_id, run_id, backend_id, fixtures, reconstructed)

    status, completed_at = await _run_status(db_engine, run_id)
    assert status == "complete"
    assert completed_at is not None

    # The gate resumed as APPROVED carrying the human's modification.
    gate_result = await _gate_result(gate_config, payload)
    assert gate_result["result"] == "approved"
    assert gate_result["human_data"] == payload


async def test_hitl_resume_roundtrip_committed_rejection_resumes_as_rejected(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """The LIVE-BUG fix: a committed REJECTION must resume as rejected — the
    reconstructed payload is the exact ``{"action": "rejected", ...}`` decision,
    never an empty ``{}`` (which the gate would treat as an approval)."""
    from modulo.core.cron_helpers import (
        _awaiting_human_has_committed_decision,
        _committed_decision_resume_data,
    )

    org_id = await _seed_org(db_engine, "HitlReject")
    account_id = await _seed_account(db_engine, org_id, "hitl-reject@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeHitlReject", account_id)
    backend_id = uuid.uuid4()
    # FAR-541: the gate id is deterministic (hitl_gate_<source>_<target>); the
    # decision payload must carry the SAME stamp the per-gate consumer checks.
    gate_config: dict[str, Any] = {
        "gate_id": "hitl_gate_a_b",
        "human_only": True,
        "overdue_threshold_minutes": 60,
        "required_team_id": None,
    }
    node_a, node_b = "a", "b"
    snap = await _seed_snapshot(db_engine, org_id, pipe, _hitl_graph(node_a, node_b, str(backend_id), gate_config))
    run_id = await _seed_run(db_engine, org_id, pipe, snap)

    fixtures = {
        "Hello World": json.dumps({"greeting": "hi"}),
        "Bye World": json.dumps({"farewell": "bye"}),
    }
    await _interrupt_run(db_engine, migrated_db_url, org_id, run_id, backend_id, fixtures)

    gate_id = await _read_gate_id(db_engine, org_id, run_id)
    payload = {"action": "rejected", "gate_id": gate_id, "reason": "wrong answer"}
    await _commit_decision(db_engine, org_id, run_id, gate_id, decision="rejected", decision_payload=payload)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, org_id)
        assert await _awaiting_human_has_committed_decision(session, org_id, run_id) is True
        reconstructed = await _committed_decision_resume_data(session, org_id, run_id)
    assert reconstructed == payload, f"resume data must be the exact rejection, got {reconstructed}"

    await _resume_and_complete(db_engine, migrated_db_url, org_id, run_id, backend_id, fixtures, reconstructed)

    status, completed_at = await _run_status(db_engine, run_id)
    assert status == "complete"
    assert completed_at is not None

    # A committed rejection routes the gate to REJECTED — never an approval.
    gate_result = await _gate_result(gate_config, payload)
    assert gate_result["result"] == "rejected"
    assert gate_result["human_data"] == payload

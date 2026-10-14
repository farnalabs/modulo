"""FAR-1723 cap-router: container-validated Router + HITL execution (real Postgres).

The prior cap-router evidence (``test_far1129_cap_router_hitl_run``) exercises
Router and HITL nodes through ``build_graph_from_json`` + ``compiled.ainvoke``
with ``session_factory=None`` and an in-memory checkpointer — a real LangGraph
execution, but NO real database. Nothing in the container tier drove a Router
node through the executor against Postgres: the run-row terminalization
(``router_no_match``), the persisted ``hitl_claims`` gate, and the
``ModuloPostgresSaver`` checkpoint/resume seam were only ever exercised by
non-router or mock-level tests.

This module closes that gap. Every test seeds org/pipeline/snapshot/run rows
in the testcontainer Postgres (``db_engine``/``migrated_db_url`` fixtures) and
runs the real ``PipelineExecutor.execute`` (and ``pipeline_execution.resume_run``
for the HITL case) with the real Postgres checkpointer:

  * Router rules route a real DB-backed run to the CORRECT leaf branch
    (asserted from the stub backend's recorded invocations — the leaf that ran
    is the leaf whose rendered prompt was invoked).
  * The Router default rule catches an unmatched state.
  * A classifier Router with no matching rule and no default terminalizes the
    run ``router_no_match`` / ``router.no_match`` in the ``runs`` row.
  * A Router-routed branch whose edge carries a HITL gate pauses the run with
    a real langgraph interrupt, stamps a real ``hitl_claims`` row, and
    resumes through ``resume_run`` to completion after a committed human
    decision.

The model backend is a recording stub registered on a real ``ModelBackendHub``
(no LLM call, no network) — the container under test is Postgres, and the
execution path under test is the executor + checkpointer + HITL claim store.
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
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend, normalize_input

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Recording stub backend — proves WHICH leaf the Router routed to.
# ---------------------------------------------------------------------------


class _RecordingStubAdapter(ModelBackendBase):
    """Adapts StubModelBackend and records every invoked rendered prompt."""

    def __init__(self, fixture_map: dict[str, str], calls: list[str]) -> None:
        self._inner = StubModelBackend(fixture_map)
        self._calls = calls

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        self._calls.append(normalize_input(messages))
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        self._calls.append(normalize_input(messages))
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "stub"


# ---------------------------------------------------------------------------
# Seeds — real Postgres rows (mirrors test_hitl_resume_roundtrip.py).
# ---------------------------------------------------------------------------


async def _seed_org(engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name.lower()}-{org_id.hex[:8]}"},
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


async def _seed_snapshot(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    graph: dict[str, Any],
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


async def _seed_pipeline_run(
    engine: AsyncEngine,
    name: str,
    email: str,
    graph: dict[str, Any],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed org + account + pipeline + snapshot + pending run for one scenario."""
    org_id = await _seed_org(engine, name)
    account_id = await _seed_account(engine, org_id, email)
    pipeline_id = await _seed_pipeline(engine, org_id, f"Pipe{name}", account_id)
    snapshot_id = await _seed_snapshot(engine, org_id, pipeline_id, graph)
    run_id = await _seed_run(engine, org_id, pipeline_id, snapshot_id)
    return org_id, pipeline_id, run_id


async def _run_status(engine: AsyncEngine, run_id: uuid.UUID) -> tuple[str, str | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, error_code FROM runs WHERE id=:rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    return row[0], row[1]


async def _executed_node_ids(engine: AsyncEngine, run_id: uuid.UUID) -> set[str]:
    """Node ids with a non-empty persisted output row for the run."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT node_id FROM run_node_outputs "
                    "WHERE run_id = :rid AND outputs_json IS NOT NULL AND outputs_json::text <> '{}'"
                ),
                {"rid": str(run_id)},
            )
        ).fetchall()
    return {str(r[0]) for r in rows}


# ---------------------------------------------------------------------------
# Graph builders.
# ---------------------------------------------------------------------------


def _agent_leaf(node_id: str, backend_id: str, marker: str) -> dict[str, Any]:
    """An agent leaf whose rendered prompt is exactly *marker* (the fixture key)."""
    return {
        "id": node_id,
        "agent_id": str(uuid.uuid4()),
        "role": "agent",
        "prompt_template": marker,
        "model_backend_id": backend_id,
    }


def _router_config(*, default_target: str | None, leaves: dict[str, str]) -> dict[str, Any]:
    """Guard rules against ``run_context.input.route`` (first-match-wins).

    Compile-time validation requires every non-classifier Router to declare an
    explicit default rule target, so *default_target* must be supplied (or a
    leaf id is substituted) unless the caller builds a classifier config.
    """
    if default_target is None:
        default_target = leaves["other"]
    return {
        "rules": [
            {"guard": "run_context.input.route == `1`", "target": leaves["one"]},
            {"guard": "run_context.input.route == `2`", "target": leaves["two"]},
            {"default": True, "target": default_target},
        ],
    }


def _router_graph(
    *,
    router_id: str,
    router_config: dict[str, Any],
    leaves: list[dict[str, Any]],
    extra_edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [{"id": router_id, "node_type": "router", "router_config": router_config}]
    nodes.extend(leaves)
    edges: list[dict[str, Any]] = [{"source": router_id, "target": leaf["id"], "type": "normal"} for leaf in leaves]
    if extra_edges:
        edges.extend(extra_edges)
    return {"nodes": nodes, "edges": edges}


_FIXTURES: dict[str, str] = {
    "route-branch-one": json.dumps({"branch": "one"}),
    "route-branch-two": json.dumps({"branch": "two"}),
    "route-branch-other": json.dumps({"branch": "other"}),
    "route-tail": json.dumps({"tail": "done"}),
}

_LEAF_MARKERS: dict[str, str] = {
    "one": "route-branch-one",
    "two": "route-branch-two",
    "other": "route-branch-other",
}


# ---------------------------------------------------------------------------
# Real-executor driver.
# ---------------------------------------------------------------------------


async def _execute_with_hub(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    backend_ids: list[str],
    calls: list[str],
    input_payload: dict[str, Any],
) -> Any:
    """Claim the run and execute it through the real PipelineExecutor."""
    claim_token = await pe.claim_run_async(db_engine, str(run_id), str(org_id))
    assert claim_token is not None, "run was not claimable"

    # Build the checkpointer DSN from the migrated-testcontainer fixture rather
    # than get_settings().database_url: the fixture is the DB this test seeded.
    conn_string = migrated_db_url.replace("+asyncpg", "").replace("+psycopg", "")
    executor = PipelineExecutor(db_engine, checkpointer_conn_string=conn_string)

    hub = ModelBackendHub()
    await hub.__aenter__()
    adapter = _RecordingStubAdapter(_FIXTURES, calls)
    for backend_id in backend_ids:
        hub.register(uuid.UUID(backend_id), adapter)
    set_model_backend_hub(hub)
    try:
        return await executor.execute(
            run_id=run_id,
            org_id=org_id,
            input_payload=input_payload,
            claim_token=claim_token,
        )
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)


def _three_leaf_router_graph(backend_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Router -> {one, two, other} agent leaves; returns (graph, leaf_id_by_name)."""
    leaf_ids = {name: str(uuid.uuid4()) for name in _LEAF_MARKERS}
    leaves = [_agent_leaf(leaf_ids[name], backend_id, _LEAF_MARKERS[name]) for name in ("one", "two", "other")]
    router_id = str(uuid.uuid4())
    graph = _router_graph(
        router_id=router_id,
        router_config=_router_config(default_target=None, leaves=leaf_ids),
        leaves=leaves,
    )
    return graph, leaf_ids


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


async def test_router_rule_routes_real_executor_run_to_matching_branch(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """A Router rule matching run input routes the DB-backed run to that leaf."""
    backend_id = str(uuid.uuid4())
    graph, leaf_ids = _three_leaf_router_graph(backend_id)
    org_id, _pipeline_id, run_id = await _seed_pipeline_run(db_engine, "Far1723Rule", "far1723-rule@test.local", graph)

    calls: list[str] = []
    final = await _execute_with_hub(db_engine, migrated_db_url, org_id, run_id, [backend_id], calls, {"route": 1})

    assert final.status == "complete", f"expected complete, got {final.status} ({final.error_code})"
    assert calls == ["route-branch-one"]
    persisted = await _executed_node_ids(db_engine, run_id)
    assert leaf_ids["one"] in persisted
    assert leaf_ids["two"] not in persisted
    assert leaf_ids["other"] not in persisted


async def test_router_default_rule_catches_unmatched_state_real_executor(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """An input matching no guard falls through to the Router default target."""
    backend_id = str(uuid.uuid4())
    graph, leaf_ids = _three_leaf_router_graph(backend_id)
    org_id, _pipeline_id, run_id = await _seed_pipeline_run(
        db_engine, "Far1723Default", "far1723-default@test.local", graph
    )

    calls: list[str] = []
    final = await _execute_with_hub(db_engine, migrated_db_url, org_id, run_id, [backend_id], calls, {"route": 9})

    assert final.status == "complete", f"expected complete, got {final.status} ({final.error_code})"
    assert calls == ["route-branch-other"]
    persisted = await _executed_node_ids(db_engine, run_id)
    assert leaf_ids["other"] in persisted
    assert leaf_ids["one"] not in persisted
    assert leaf_ids["two"] not in persisted


async def test_router_classifier_no_match_terminalizes_run(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """A classifier Router with no match and no default terminalizes the run
    ``router_no_match`` / ``router.no_match`` — a real status write, not a raise
    swallowed at a mock boundary."""
    backend_id = str(uuid.uuid4())
    leaf_one = _agent_leaf(str(uuid.uuid4()), backend_id, _LEAF_MARKERS["one"])
    leaf_two = _agent_leaf(str(uuid.uuid4()), backend_id, _LEAF_MARKERS["two"])
    router_id = str(uuid.uuid4())
    # Classifier mode may omit a default rule (compile-time validation only
    # requires a default for non-classifier configs), so a genuine no-match is
    # reachable at runtime. The seeded state carries no ``_llm_next_node``.
    classifier_config: dict[str, Any] = {
        "mode": "classifier",
        "rules": [{"label": "b", "target": leaf_two["id"]}],
    }
    graph = _router_graph(router_id=router_id, router_config=classifier_config, leaves=[leaf_one, leaf_two])
    org_id, _pipeline_id, run_id = await _seed_pipeline_run(
        db_engine, "Far1723NoMatch", "far1723-nomatch@test.local", graph
    )

    calls: list[str] = []
    final = await _execute_with_hub(db_engine, migrated_db_url, org_id, run_id, [backend_id], calls, {"route": 1})

    assert final.status == "router_no_match", f"expected router_no_match, got {final.status} ({final.error_code})"
    assert final.error_code == "router.no_match"
    # No leaf was ever invoked — the Router refused before any agent ran.
    assert not calls
    status, error_code = await _run_status(db_engine, run_id)
    assert status == "router_no_match"
    assert error_code == "router.no_match"


async def test_router_branch_hitl_gate_pauses_and_resumes_real_executor(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """Router routes to a branch whose edge carries a HITL gate: the run pauses
    with a real langgraph interrupt, stamps a real ``hitl_claims`` row, and a
    committed human decision resumes it to completion through ``resume_run``."""
    backend_id = str(uuid.uuid4())
    leaf_one = _agent_leaf(str(uuid.uuid4()), backend_id, _LEAF_MARKERS["one"])
    leaf_two = _agent_leaf(str(uuid.uuid4()), backend_id, _LEAF_MARKERS["two"])
    tail = _agent_leaf(str(uuid.uuid4()), backend_id, "route-tail")
    router_id = str(uuid.uuid4())
    gate_config: dict[str, Any] = {
        # FAR-541: the compile path re-stamps the gate with the synthetic id
        # derived from the source/target pair; the decision must carry it.
        "review_id": f"hitl_review_{leaf_one['id']}_{tail['id']}",
        "human_only": True,
        "overdue_threshold_minutes": 60,
        "required_team_id": None,
    }
    leaf_targets = {"one": leaf_one["id"], "two": leaf_two["id"], "other": leaf_two["id"]}
    graph = _router_graph(
        router_id=router_id,
        router_config=_router_config(default_target=None, leaves=leaf_targets),
        leaves=[leaf_one, leaf_two, tail],
        extra_edges=[
            {
                "source": leaf_one["id"],
                "target": tail["id"],
                "type": "normal",
                "hitl_review_config": gate_config,
            },
            {
                "source": leaf_two["id"],
                "target": tail["id"],
                "type": "normal",
            },
        ],
    )
    org_id, _pipeline_id, run_id = await _seed_pipeline_run(db_engine, "Far1723Hitl", "far1723-hitl@test.local", graph)

    calls: list[str] = []
    paused = await _execute_with_hub(db_engine, migrated_db_url, org_id, run_id, [backend_id], calls, {"route": 1})
    assert paused.status == "awaiting_human", f"expected awaiting_human, got {paused.status} ({paused.error_code})"
    # The Router routed to leaf_one (not leaf_two) before the gate interrupted.
    assert calls == ["route-branch-one"]

    # A real HITL claim row was stamped in the testcontainer.
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT review_id FROM hitl_claims WHERE organisation_id=:oid AND run_id=:rid LIMIT 1"),
                {"oid": str(org_id), "rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None, "no HITL gate claim was created for the interrupted run"
    review_id = str(row[0])

    # Commit the human decision the way routes/hitl.py does, then reconstruct
    # the resume payload through the production seam and resume the run.
    decision_payload = {"action": "approved", "review_id": review_id}
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE hitl_claims SET decision=:d, decision_at=now(), "
                "decision_payload=CAST(:p AS json) "
                "WHERE organisation_id=:oid AND run_id=:rid AND review_id=:gid"
            ),
            {
                "d": "approved",
                "p": json.dumps(decision_payload),
                "oid": str(org_id),
                "rid": str(run_id),
                "gid": review_id,
            },
        )

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from modulo.core.cron_helpers import _committed_decision_resume_data
    from modulo.db.rls import set_rls_org

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        reconstructed = await _committed_decision_resume_data(session, org_id, run_id)
    assert reconstructed == decision_payload

    hub = ModelBackendHub()
    await hub.__aenter__()
    hub.register(uuid.UUID(backend_id), _RecordingStubAdapter(_FIXTURES, calls))
    set_model_backend_hub(hub)
    try:
        outcome = await pe.resume_run(
            async_engine=db_engine,
            run_id=str(run_id),
            org_id=str(org_id),
            resume_data=reconstructed,
        )
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)

    assert outcome.get("status") == "complete", f"resume outcome: {outcome}"
    status, _error_code = await _run_status(db_engine, run_id)
    assert status == "complete"
    # Router picked leaf_one, then the post-leaf_one gate interrupted; after
    # the committed decision the gate resumed and the tail ran — leaf_two
    # never ran.
    assert calls == ["route-branch-one", "route-tail"]

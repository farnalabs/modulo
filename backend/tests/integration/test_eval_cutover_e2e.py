"""Chunk-3 read-cutover behavioural tests (FAR-1100, spec §8 criteria 16-17).

Drives the PRODUCTION entry points against real Postgres (testcontainers):

* Criterion 16 — a post-cutover-authored Eval (a row in ``evals`` that never
  existed in ``eval_definitions``) runs end-to-end through the real
  ``PipelineExecutor``: the post-node eval engine reads it from the new
  tables, and the persisted ``eval_results`` row's ``eval_id`` resolves
  against ``evals`` and NOT against ``eval_definitions``.

* Criterion 17 — the suite completeness guard (``_check_eval_suites``) still
  reads ``eval_definitions`` at this chunk (the suite read switch is chunk
  5c, spec CO-3): all-pass results clear the guard, below-threshold results
  raise ``EvalSuiteBlockedError``.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import modulo.core.pipeline_execution as pe
from modulo.core.eval_engine import EvalSuiteBlockedError
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_model_backend_hub
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Seed helpers (raw SQL, minimal — mirrors test_runtime_conformance.py)
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
) -> uuid.UUID:
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


async def _seed_snapshot(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID, graph: dict) -> uuid.UUID:
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
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id), "graph": json.dumps(graph)},
        )
    return snapshot_id


async def _seed_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    status: str = "pending",
) -> uuid.UUID:
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                "run_number, status) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, :st)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": uuid.uuid4().hex,
                "thread": f"{org_id}:{run_id}",
                "rn": run_number,
                "st": status,
            },
        )
    return run_id


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


# ---------------------------------------------------------------------------
# Criterion 16 — post-node eval persists via the evals cutover
# ---------------------------------------------------------------------------


async def test_post_node_eval_persists_via_evals_cutover(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """An Eval authored ONLY in ``evals`` (+ PolicyGate) drives a real run's
    post-node eval: the run completes and the persisted eval_results row's
    eval_id resolves against ``evals`` and NOT against ``eval_definitions``.

    ``eval_definitions`` is deliberately left EMPTY — the only way a result
    row can exist is that the executor read the new tables.
    """
    from modulo.core.pipeline_engine.executor import PipelineExecutor
    from modulo.settings import get_settings

    org_id = await _seed_org(db_engine, "CutoverE2E")
    account_id = await _seed_account(db_engine, org_id, "cutover-e2e@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeCutoverE2E", account_id)
    node_id = str(uuid.uuid4())  # Eval.node_id is a Uuid column
    backend_id = str(uuid.uuid4())
    snap = await _seed_snapshot(db_engine, org_id, pipe, _one_agent_graph(node_id, backend_id))
    run_id = await _seed_run(db_engine, org_id, pipe, snap, status="pending")

    # Post-cutover authoring shape: Eval + PolicyGate rows in the NEW tables.
    eval_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO evals (id, organisation_id, pipeline_id, node_id, name, eval_type, "
                "config_json, account_id) "
                "VALUES (:id, :oid, :pid, CAST(:nid AS uuid), 'cutover-regex', 'regex', "
                "CAST(:cfg AS json), :aid)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "pid": str(pipe),
                "nid": node_id,
                "cfg": json.dumps({"pattern": "Hello", "field": "greeting"}),
                "aid": str(account_id),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, action) "
                "VALUES (:id, :oid, :eid, CAST(:nid AS uuid), 'warn')"
            ),
            {"id": str(uuid.uuid4()), "oid": str(org_id), "eid": str(eval_id), "nid": node_id},
        )

    claim_token = await pe.claim_run_async(app_engine, str(run_id), str(org_id))
    assert claim_token is not None, "claim must succeed under RLS"

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
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)

    assert final.status == "complete", f"run must complete, got {final.status}"

    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT er.eval_id, er.passed, "
                    "(EXISTS (SELECT 1 FROM evals e WHERE e.id = er.eval_id)) AS in_evals, "
                    "(EXISTS (SELECT 1 FROM eval_definitions d WHERE d.id = er.eval_id)) AS in_legacy "
                    "FROM eval_results er WHERE er.run_id = :rid"
                ),
                {"rid": str(run_id)},
            )
        ).fetchall()

    assert len(rows) == 1, f"expected exactly one post-node eval result, got {len(rows)}"
    stored_eval_id, _passed, in_evals, in_legacy = rows[0]
    assert stored_eval_id == eval_id, f"result must reference the evals-row eval {eval_id}, got {stored_eval_id}"
    assert in_evals is True, "persisted eval_id must resolve against evals (the cutover read path)"
    assert in_legacy is False, "persisted eval_id must NOT exist in eval_definitions (table is empty)"


# ---------------------------------------------------------------------------
# Criterion 17 — suite completeness guard still reads eval_definitions (CO-3)
# ---------------------------------------------------------------------------


async def test_eval_suite_guard_still_reads_eval_definitions(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """``_check_eval_suites`` still reads ``eval_definitions`` in this chunk
    (the suite read switch lands in chunk 5c): an all-pass run clears the
    guard; a below-threshold run raises ``EvalSuiteBlockedError``."""
    from modulo.core.pipeline_engine.executor import PipelineExecutor
    from modulo.settings import get_settings

    org_id = await _seed_org(db_engine, "CutoverSuite")
    account_id = await _seed_account(db_engine, org_id, "cutover-suite@test.local")
    pipe = await _seed_pipeline(db_engine, org_id, "PipeCutoverSuite", account_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, {"nodes": [], "edges": []})

    # Suite-scoped eval_definitions (node_id NULL, suite_id + threshold set).
    # The same UUIDs are mirrored into `evals` — the post-cutover data shape
    # 0254's backfill produces — because eval_results.eval_id now resolves
    # via evals (tenant trigger), while `_check_eval_suites` still reads
    # eval_definitions (chunk 5c switches that read).
    def_a = uuid.uuid4()
    def_b = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        for eval_id, name in ((def_a, "suite-a"), (def_b, "suite-b")):
            await conn.execute(
                text(
                    "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, name, eval_type, "
                    "config_json, failure_behaviour, pass_threshold, suite_id, account_id) "
                    "VALUES (:id, :oid, :pid, :name, 'regex', '{}'::json, 'warn', 0.5, 'suite-17', :aid)"
                ),
                {
                    "id": str(eval_id),
                    "oid": str(org_id),
                    "pid": str(pipe),
                    "name": name,
                    "aid": str(account_id),
                },
            )
            # evals row uses a DIFFERENT threshold (0.8) so the test can
            # distinguish which table _check_eval_suites reads: if it reads
            # eval_definitions (intended, CO-3), the 0.5 threshold governs;
            # if it reads evals (wrong), the 0.8 threshold governs.
            await conn.execute(
                text(
                    "INSERT INTO evals (id, organisation_id, pipeline_id, node_id, name, eval_type, "
                    "config_json, pass_threshold, suite_id, account_id) "
                    "VALUES (:id, :oid, :pid, NULL, :name, 'regex', '{}'::jsonb, 0.8, 'suite-17', :aid)"
                ),
                {
                    "id": str(eval_id),
                    "oid": str(org_id),
                    "pid": str(pipe),
                    "name": name,
                    "aid": str(account_id),
                },
            )

    settings = get_settings()
    conn_string = str(settings.database_url).replace("+asyncpg", "").replace("+psycopg", "")
    executor = PipelineExecutor(db_engine, checkpointer_conn_string=conn_string)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # All-pass run: full set coverage, aggregate 0.5 >= 0.5 (eval_definitions
    # threshold) -> no raise.  If _check_eval_suites incorrectly read evals
    # (threshold 0.8), the 0.5 aggregate would be below threshold and the
    # guard would raise — this is the falsifiability check (F4).
    run_pass = await _seed_run(db_engine, org_id, pipe, snap, status="running")
    async with db_engine.connect() as conn, conn.begin():
        for eval_id in (def_a, def_b):
            await conn.execute(
                text(
                    "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed, score) "
                    "VALUES (:id, :oid, :rid, :eid, true, 0.5)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_id),
                    "rid": str(run_pass),
                    "eid": str(eval_id),
                },
            )
    async with session_factory() as session:
        suite_results = await executor._check_eval_suites(session, run_pass, pipe)
    assert len(suite_results) == 1, f"expected one suite result, got {len(suite_results)}"

    # All-fail run: full coverage but aggregate 0.0 < 0.5 -> blocked.
    run_fail = await _seed_run(db_engine, org_id, pipe, snap, status="running")
    async with db_engine.connect() as conn, conn.begin():
        for eval_id in (def_a, def_b):
            await conn.execute(
                text(
                    "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed, score) "
                    "VALUES (:id, :oid, :rid, :eid, false, 0.0)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_id),
                    "rid": str(run_fail),
                    "eid": str(eval_id),
                },
            )
    async with session_factory() as session:
        with pytest.raises(EvalSuiteBlockedError):
            await executor._check_eval_suites(session, run_fail, pipe)

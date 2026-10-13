"""FAR-939 chunk 12 — human review as a native Policy Gate evaluation mode.

Integration coverage against real Postgres (testcontainers) for the chunk's
acceptance criteria that need the migrated schema, the executor's real
eval-def build, the compiled graph, and the resume seam:

* **C1** — ``policy_gates.evaluation_mode`` exists: TEXT NOT NULL DEFAULT
  ``'automatic'`` with CHECK ``('automatic', 'human')`` (``pg_constraint`` /
  ``information_schema`` queries).
* **C8** — graph build fails closed: a node carrying a human-mode-governed
  eval must be the source of EXACTLY ONE edge that compiles an HITL review
  gate, and that gate's config must not carry an explicit
  ``human_only: false`` (direct ``build_graph_from_json`` calls).
* **C9/C10/C14** — E2E approve: the run pauses at the existing interrupt
  (no EvalResult / PolicyGateDecision / EvalResult-block while pending), a
  committed approval resumes it to completion, and exactly one Evidence row
  ``gate_decision_<policy_gate_id>`` (subject run, ``value=true``) exists.
* **C15** — E2E reject: the run follows the existing reject path and the
  Evidence row carries ``value=false``.
* **C11** — after an end-to-end human decide, ``policy_gate_decisions`` has
  zero rows for the run and ``reconcile_decision_records`` reports zero
  anomalies for the org.
* **C12** — the grounding write is fail-open: an ``append_evidence`` failure
  during resume logs a structured warning and never fails the run.
* **C18** — cancelling an ``awaiting_human`` run whose human gate is pending
  terminalises cleanly with no undecided claim left on an awaiting run.

Seeding/interrupt/decision/resume helpers are shared with
``test_hitl_resume_roundtrip`` (the same harness the FAR-1533 reject BDD and
FAR-1723 router tests use): the human decision is committed the way
``routes/hitl.py`` does (decision columns on ``hitl_claims``) and the resume
payload is reconstructed through the production
``_committed_decision_resume_data`` seam before ``resume_run``.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import tests.integration.test_hitl_resume_roundtrip as rt

# ``asyncio_mode = "auto"`` (pyproject) collects the async tests; the module
# deliberately does NOT carry an explicit ``pytest.mark.asyncio`` because the
# C8 graph-build tests are synchronous.
pytestmark = pytest.mark.integration

_FIXTURES = {
    "Hello World": json.dumps({"greeting": "hi"}),
    "Bye World": json.dumps({"farewell": "bye"}),
}


# ---------------------------------------------------------------------------
# Seeds — Eval + PolicyGate (evaluation_mode) for the gate's SOURCE node
# ---------------------------------------------------------------------------


async def _seed_eval_with_gate(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    node_id: str,
    *,
    action: str = "block",
    evaluation_mode: str = "human",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a node-scoped Eval + its live PolicyGate; return ``(eval_id, gate_id)``."""
    eval_id = uuid.uuid4()
    gate_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO evals (id, organisation_id, pipeline_id, node_id, name, eval_type, "
                "config_json, pass_threshold, suite_id, version, account_id) "
                "VALUES (:id, :oid, :pid, CAST(:nid AS uuid), :name, 'regex', "
                "CAST(:cfg AS json), NULL, NULL, 1, :aid)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "nid": node_id,
                "name": "human-gate-eval",
                "cfg": json.dumps({"field": "greeting", "pattern": "hi"}),
                "aid": str(account_id),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, action, "
                "evaluation_mode, version, enabled, enabled_at) "
                "VALUES (:id, :oid, :eid, CAST(:nid AS uuid), :action, :mode, 1, true, now())"
            ),
            {
                "id": str(gate_id),
                "oid": str(org_id),
                "eid": str(eval_id),
                "nid": node_id,
                "action": action,
                "mode": evaluation_mode,
            },
        )
    return eval_id, gate_id


async def _run_query(engine: AsyncEngine, sql: str, params: dict[str, object] | None = None) -> list[tuple]:
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return [tuple(row) for row in result.all()]


async def _count(engine: AsyncEngine, sql: str, params: dict[str, object]) -> int:
    rows = await _run_query(engine, sql, params)
    return int(rows[0][0])


async def _seed_human_gate_run(
    db_engine: AsyncEngine,
    label: str,
    *,
    evaluation_mode: str = "human",
) -> dict[str, object]:
    """Seed org/account/pipeline/snapshot/run + human-mode Eval/PolicyGate.

    Node ids are UUIDs (``Eval.node_id`` is a UUID column) so the DTO build
    keys the eval onto the gate's SOURCE node.
    """
    org_id = await rt._seed_org(db_engine, label)
    account_id = await rt._seed_account(db_engine, org_id, f"{label.lower()}-{uuid.uuid4().hex[:8]}@test.local")
    pipeline_id = await rt._seed_pipeline(db_engine, org_id, f"Pipe{label}", account_id)
    node_a, node_b = str(uuid.uuid4()), str(uuid.uuid4())
    backend_id = uuid.uuid4()
    gate_config: dict[str, object] = {
        # The compile path stamps the deterministic review id
        # hitl_review_<source>_<target>; the decision must carry the same stamp.
        "review_id": f"hitl_review_{node_a}_{node_b}",
        "human_only": True,
        "overdue_threshold_minutes": 60,
        "required_team_id": None,
    }
    graph = rt._hitl_graph(node_a, node_b, str(backend_id), gate_config)
    snapshot_id = await rt._seed_snapshot(db_engine, org_id, pipeline_id, graph)
    run_id = await rt._seed_run(db_engine, org_id, pipeline_id, snapshot_id)
    eval_id, gate_id = await _seed_eval_with_gate(
        db_engine,
        org_id,
        pipeline_id,
        account_id,
        node_a,
        evaluation_mode=evaluation_mode,
    )
    return {
        "org_id": org_id,
        "account_id": account_id,
        "pipeline_id": pipeline_id,
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "backend_id": backend_id,
        "node_a": node_a,
        "node_b": node_b,
        "gate_config": gate_config,
        "eval_id": eval_id,
        "gate_id": gate_id,
    }


async def _interrupt(db_engine: AsyncEngine, migrated_db_url: str, ctx: dict[str, object]) -> str:
    """Execute to the gate interrupt; return the stamped review_id."""
    await rt._interrupt_run(
        db_engine,
        migrated_db_url,
        ctx["org_id"],  # type: ignore[arg-type]
        ctx["run_id"],  # type: ignore[arg-type]
        ctx["backend_id"],  # type: ignore[arg-type]
        _FIXTURES,
    )
    return await rt._read_review_id(db_engine, ctx["org_id"], ctx["run_id"])  # type: ignore[arg-type]


async def _decision_counts(db_engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, int]:
    """Row counts proving the human path writes no eval/decision rows."""
    return {
        "eval_results": await _count(
            db_engine,
            "SELECT count(*) FROM eval_results WHERE run_id = :rid",
            {"rid": str(run_id)},
        ),
        "policy_gate_decisions": await _count(
            db_engine,
            "SELECT count(*) FROM policy_gate_decisions WHERE run_id = :rid",
            {"rid": str(run_id)},
        ),
        "evidence": await _count(
            db_engine,
            "SELECT count(*) FROM evidence WHERE subject_type = 'run' AND subject_id = :sid",
            {"sid": str(run_id)},
        ),
    }


async def _resume_with_decision(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    ctx: dict[str, object],
    review_id: str,
    *,
    action: str,
    expected_status: str,
) -> None:
    """Commit the decision the way routes/hitl.py does, then resume via the
    production reconstruction seam."""
    org_id = ctx["org_id"]
    run_id = ctx["run_id"]
    payload: dict[str, object] = {"action": action, "review_id": review_id}
    if action == "rejected":
        payload["reason"] = "not good enough"
    await rt._commit_decision(
        db_engine,  # type: ignore[arg-type]
        org_id,  # type: ignore[arg-type]
        run_id,  # type: ignore[arg-type]
        review_id,
        decision=action,
        decision_payload=payload,
    )
    from modulo.core.cron_helpers import _committed_decision_resume_data
    from modulo.db.rls import set_rls_org

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)  # type: ignore[arg-type]
        reconstructed = await _committed_decision_resume_data(session, org_id, run_id)  # type: ignore[arg-type]
    assert reconstructed == payload

    await rt._resume_and_complete(
        db_engine,  # type: ignore[arg-type]
        migrated_db_url,
        org_id,  # type: ignore[arg-type]
        run_id,  # type: ignore[arg-type]
        ctx["backend_id"],  # type: ignore[arg-type]
        _FIXTURES,
        reconstructed,
        expected_status=expected_status,
    )


# ---------------------------------------------------------------------------
# C1 — the column, default, and CHECK exist on the migrated schema
# ---------------------------------------------------------------------------


async def test_c1_evaluation_mode_column_default_and_check(db_engine: AsyncEngine) -> None:
    """TEXT, NOT NULL, default 'automatic', CHECK ('automatic','human')."""
    column = await _run_query(
        db_engine,
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'policy_gates' AND column_name = 'evaluation_mode'",
    )
    assert column, "policy_gates.evaluation_mode column missing"
    data_type, is_nullable, column_default = column[0]
    assert data_type == "text"
    assert is_nullable == "NO"
    assert "automatic" in str(column_default)

    check = await _run_query(
        db_engine,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_policy_gates_evaluation_mode' "
        "AND conrelid = 'public.policy_gates'::regclass",
    )
    assert check, "ck_policy_gates_evaluation_mode missing"
    assert "'automatic'" in check[0][0]
    assert "'human'" in check[0][0]

    # The CHECK rejects an out-of-vocabulary value (fail-closed proof).
    org_id = await rt._seed_org(db_engine, "C1Check")
    account_id = await rt._seed_account(db_engine, org_id, f"c1-check-{uuid.uuid4().hex[:8]}@test.local")
    pipeline_id = await rt._seed_pipeline(db_engine, org_id, "PipeC1Check", account_id)
    eval_id, _gate_id = await _seed_eval_with_gate(db_engine, org_id, pipeline_id, account_id, str(uuid.uuid4()))
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        await _run_query(
            db_engine,
            "UPDATE policy_gates SET evaluation_mode = 'telepathic' WHERE eval_id = :eid",
            {"eid": str(eval_id)},
        )


# ---------------------------------------------------------------------------
# C8 — graph build fails closed for a pause-less / non-unique / non-human-only
# human gate
# ---------------------------------------------------------------------------


def _human_dto(node_id: str, *, mode: str = "human") -> object:
    from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO

    return EvalDefDTO(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="human-gate-eval",
        eval_type="regex",
        config={"field": "greeting", "pattern": "hi"},
        failure_behaviour="block",
        node_id=node_id,
        policy_gate_id=uuid.uuid4(),
        policy_gate_version=1,
        policy_gate_node_id=uuid.UUID(node_id),
        policy_gate_evaluation_mode=mode,
    )


def _two_node_graph(edge_extra: dict[str, object] | None = None) -> tuple[dict, str, str]:
    from modulo.core.pipeline_engine.graph_cache import _make_review_id

    node_a, node_b = str(uuid.uuid4()), str(uuid.uuid4())
    graph = {
        "nodes": [
            {
                "id": node_a,
                "agent_id": str(uuid.uuid4()),
                "node_type": "agent",
                "prompt_template": "Hello {{ state.run_context.input.name }}",
                "model_backend_id": str(uuid.uuid4()),
            },
            {
                "id": node_b,
                "agent_id": str(uuid.uuid4()),
                "node_type": "agent",
                "prompt_template": "Bye {{ state.run_context.input.name }}",
                "model_backend_id": str(uuid.uuid4()),
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": node_a,
                "target": node_b,
                "type": "normal",
                "hitl_review_config": {
                    "review_id": _make_review_id(node_a, node_b),
                    "human_only": True,
                    **(edge_extra or {}),
                },
            },
        ],
    }
    return graph, node_a, node_b


def _build(graph: dict, by_node: dict) -> object:
    from modulo.core.pipeline_engine.graph_cache import build_graph_from_json

    return build_graph_from_json(graph, eval_definitions_by_node=by_node)


def test_c8_human_gate_with_exactly_one_pause_edge_compiles() -> None:
    """The happy shape: one HITL edge from the eval's node — build succeeds."""
    graph, node_a, _node_b = _two_node_graph()
    compiled = _build(graph, {node_a: [_human_dto(node_a)]})
    assert compiled is not None


def test_c8_human_gate_without_a_pause_edge_raises() -> None:
    """No outgoing HITL edge from the human-governed node: the run must not
    start (the gate could silently decide nothing)."""
    graph, node_a, _node_b = _two_node_graph()
    graph["edges"][0].pop("hitl_review_config")
    with pytest.raises(ValueError, match="EXACTLY ONE"):
        _build(graph, {node_a: [_human_dto(node_a)]})


def test_c8_human_gate_with_two_pause_edges_raises() -> None:
    """Two HITL edges from one human-governed node would raise two interrupts
    for one gate — refused."""
    from modulo.core.pipeline_engine.graph_cache import _make_review_id

    graph, node_a, _node_b = _two_node_graph()
    node_c = str(uuid.uuid4())
    graph["nodes"].append(
        {
            "id": node_c,
            "agent_id": str(uuid.uuid4()),
            "node_type": "agent",
            "prompt_template": "Third",
            "model_backend_id": str(uuid.uuid4()),
        }
    )
    graph["edges"].append(
        {
            "id": "e2",
            "source": node_a,
            "target": node_c,
            "type": "normal",
            "hitl_review_config": {"review_id": _make_review_id(node_a, node_c), "human_only": True},
        }
    )
    with pytest.raises(ValueError, match="EXACTLY ONE"):
        _build(graph, {node_a: [_human_dto(node_a)]})


def test_c8_explicit_human_only_false_raises() -> None:
    """A human-decided gate is definitionally human-only: an explicit
    ``human_only: false`` on its pause edge is refused."""
    graph, node_a, _node_b = _two_node_graph({"human_only": False})
    with pytest.raises(ValueError, match="human_only"):
        _build(graph, {node_a: [_human_dto(node_a)]})


def test_c8_automatic_mode_gates_are_unaffected() -> None:
    """A node whose only governed eval is automatic-mode needs no pause edge
    (today's behaviour byte-identical)."""
    graph, node_a, _node_b = _two_node_graph()
    graph["edges"][0].pop("hitl_review_config")
    _build(graph, {node_a: [_human_dto(node_a, mode="automatic")]})


# ---------------------------------------------------------------------------
# C9 / C10 / C11 / C14 — E2E approve: skip while pending, evidence on resume
# ---------------------------------------------------------------------------


async def test_c14_e2e_approve_grounds_evidence_and_writes_no_decision_row(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """A human-mode gate pauses at the existing interrupt; an approval (the
    decision committed the way routes/hitl.py does) resumes the run to
    completion and grounds exactly one ``value=true`` Evidence row — with
    zero EvalResult / PolicyGateDecision rows at any point."""
    ctx = await _seed_human_gate_run(db_engine, "C14Approve")
    run_id = ctx["run_id"]
    gate_id = ctx["gate_id"]

    review_id = await _interrupt(db_engine, migrated_db_url, ctx)

    # C9 (pre): while pending, no evidence / eval result / decision row.
    counts = await _decision_counts(db_engine, run_id)  # type: ignore[arg-type]
    assert counts["evidence"] == 0
    assert counts["eval_results"] == 0
    assert counts["policy_gate_decisions"] == 0

    await _resume_with_decision(
        db_engine, migrated_db_url, ctx, review_id, action="approved", expected_status="complete"
    )

    # C9 (post): exactly one Evidence row, keyed on the gate, subject run.
    rows = await _run_query(
        db_engine,
        "SELECT key, subject_type, subject_id, value, producer_type, producer_id::text "
        "FROM evidence WHERE subject_type = 'run' AND subject_id = :sid AND key = :key",
        {"sid": str(run_id), "key": f"gate_decision_{gate_id}"},
    )
    assert len(rows) == 1
    key, subject_type, subject_id, value, producer_type, producer_id = rows[0]
    assert key == f"gate_decision_{gate_id}"
    assert subject_type == "run"
    assert subject_id == str(run_id)
    # C10: approved -> value=true; the configured enforcement is NOT in value.
    assert value is True
    assert producer_type == "policy_gate"
    assert producer_id == str(gate_id)

    # C11: zero PolicyGateDecision rows for the run.
    counts = await _decision_counts(db_engine, run_id)  # type: ignore[arg-type]
    assert counts["policy_gate_decisions"] == 0
    assert counts["eval_results"] == 0  # the human-governed eval never computed
    assert counts["evidence"] == 1

    # C11: the reconciler reports zero anomalies for the org.
    from modulo.core.eval_engine.decision_reconcile import reconcile_decision_records
    from modulo.db.rls import set_rls_org

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, ctx["org_id"])  # type: ignore[arg-type]
        report = await reconcile_decision_records(session, org_id=ctx["org_id"])  # type: ignore[arg-type]
    assert report.counts_by_kind().get("missing_eval_result_id", 0) == 0
    assert not report.anomalies


async def test_c15_e2e_reject_grounds_false_evidence(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """A rejection follows the existing reject path (no reject route → the
    run ends ``rejected``) and grounds Evidence ``value=false``."""
    ctx = await _seed_human_gate_run(db_engine, "C15Reject")
    run_id = ctx["run_id"]
    gate_id = ctx["gate_id"]

    review_id = await _interrupt(db_engine, migrated_db_url, ctx)
    await _resume_with_decision(
        db_engine, migrated_db_url, ctx, review_id, action="rejected", expected_status="rejected"
    )

    rows = await _run_query(
        db_engine,
        "SELECT value FROM evidence WHERE subject_type = 'run' AND subject_id = :sid AND key = :key",
        {"sid": str(run_id), "key": f"gate_decision_{gate_id}"},
    )
    assert len(rows) == 1
    assert rows[0][0] is False

    counts = await _decision_counts(db_engine, run_id)  # type: ignore[arg-type]
    assert counts["policy_gate_decisions"] == 0
    assert counts["eval_results"] == 0


# ---------------------------------------------------------------------------
# C12 — the grounding write is fail-open with a structured log
# ---------------------------------------------------------------------------


async def test_c12_grounding_failure_is_fail_open(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``append_evidence`` failure during resume must NOT fail the run or
    the already-committed decision: the run still completes and a structured
    warning is logged."""
    import modulo.core.eval_engine.evidence_layer as evidence_layer

    ctx = await _seed_human_gate_run(db_engine, "C12FailOpen")
    run_id = ctx["run_id"]

    review_id = await _interrupt(db_engine, migrated_db_url, ctx)

    original = evidence_layer.append_evidence

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("grounding store unavailable")

    evidence_layer.append_evidence = _boom  # type: ignore[assignment]
    try:
        with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
            await _resume_with_decision(
                db_engine, migrated_db_url, ctx, review_id, action="approved", expected_status="complete"
            )
    finally:
        evidence_layer.append_evidence = original  # type: ignore[assignment]

    failure_logs = [r for r in caplog.records if r.getMessage() == "hitl_review.human_gate_evidence_grounding_failed"]
    assert failure_logs, "the grounding failure must be logged with its structured event name"

    # No evidence row landed, but the run's decision stood.
    status, _ = await rt._run_status(db_engine, run_id)  # type: ignore[arg-type]
    assert status == "complete"
    counts = await _decision_counts(db_engine, run_id)  # type: ignore[arg-type]
    assert counts["evidence"] == 0
    assert counts["policy_gate_decisions"] == 0


# ---------------------------------------------------------------------------
# C18 — cancellation while the human gate is pending terminalises cleanly
# ---------------------------------------------------------------------------


async def test_c18_cancellation_while_pending_is_clean(
    db_engine: AsyncEngine,
    migrated_db_url: str,
) -> None:
    """Cancelling an ``awaiting_human`` run whose human-mode gate is pending
    reaches ``cancelled`` with no undecided claim left on an awaiting run
    (the pending list's status filter excludes it)."""
    from modulo.db.crud.run import request_cancellation
    from modulo.db.rls import set_rls_org

    ctx = await _seed_human_gate_run(db_engine, "C18Cancel")
    run_id = ctx["run_id"]
    await _interrupt(db_engine, migrated_db_url, ctx)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, ctx["org_id"])  # type: ignore[arg-type]
        cancelled = await request_cancellation(session, run_id)  # type: ignore[arg-type]
    assert cancelled is not None

    status, _ = await rt._run_status(db_engine, run_id)  # type: ignore[arg-type]
    assert status == "cancelled"

    # No undecided claim for this run remains on an awaiting_human run — the
    # pending-list status filter has nothing to surface.
    pending = await _count(
        db_engine,
        "SELECT count(*) FROM hitl_claims c "
        "WHERE c.run_id = :rid AND c.decision IS NULL "
        "AND EXISTS (SELECT 1 FROM runs r WHERE r.id = c.run_id AND r.status = 'awaiting_human')",
        {"rid": str(run_id)},
    )
    assert pending == 0

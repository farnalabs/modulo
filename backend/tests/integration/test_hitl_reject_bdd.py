"""FAR-1533: BDD steps for ``features/hitl/reject.feature`` driving a REAL run.

Lives under ``tests/integration`` (not ``tests/bdd/steps``) because it needs the
real-Postgres fixtures of ``tests/integration/conftest.py``. Each scenario seeds
a pipeline with a HITL gate, runs it with the real ``PipelineExecutor`` to the
gate interrupt, commits a human rejection, resumes through
``pipeline_execution.resume_run`` and reads the terminal run state back from the
database. The seeding/interrupt/resume helpers are shared with
``test_hitl_resume_roundtrip.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import tests.integration.test_hitl_resume_roundtrip as rt
from modulo.core.hitl_manager.gate_coalescing import SUPERSEDE_REASON

pytestmark = [pytest.mark.integration]

scenarios("../bdd/features/hitl/reject.feature")

_FIXTURES = {
    "Hello World": json.dumps({"greeting": "hi"}),
    "Bye World": json.dumps({"farewell": "bye"}),
    "Fix World": json.dumps({"fixed": True}),
}
_NODE_PROMPTS = {"a": "Hello World", "b": "Bye World", "fixer": "Fix World"}


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive an async helper from a sync pytest-bdd step.

    The integration ``db_engine`` uses ``NullPool`` precisely so it can be used
    from any event loop, so a fresh loop per step is safe.
    """
    return asyncio.run(coro)


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {"gate_extra": {}, "reject_edge_to": None}


@pytest.fixture
def executed_prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every prompt the stub backend serves, across interrupt AND resume."""
    seen: list[str] = []

    class _Recording(rt._StubAdapter):
        async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
            seen.append(str(messages[-1].content))
            return await super().invoke(messages, **kwargs)

    monkeypatch.setattr(rt, "_StubAdapter", _Recording)
    return seen


def _graph(ctx: dict[str, Any]) -> dict:
    graph = rt._hitl_graph("a", "b", str(ctx["backend_id"]), ctx["gate_config"])
    fixer = ctx["reject_edge_to"]
    if fixer:
        graph["nodes"].append(
            {
                "id": fixer,
                "agent_id": str(uuid.uuid4()),
                "role": "agent",
                "prompt_template": "Fix {{ state.run_context.input.name }}",
                "model_backend_id": str(ctx["backend_id"]),
            }
        )
        graph["edges"].append({"id": "e-reject", "source": "a", "target": fixer, "type": "reject"})
    return graph


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("a pipeline whose HITL gate has no reject route")
def _gate_no_route(ctx: dict[str, Any]) -> None:
    ctx["gate_extra"] = {}


@given(parsers.parse('a pipeline whose HITL gate has a reject route to node "{node}"'))
def _gate_with_route(ctx: dict[str, Any], node: str) -> None:
    ctx["reject_edge_to"] = node


@given(parsers.parse('a pipeline whose HITL gate has on_reject set to "{mode}"'))
def _gate_on_reject(ctx: dict[str, Any], mode: str) -> None:
    ctx["gate_extra"] = {"on_reject": mode}


@given("a run is waiting at the gate")
def _run_waiting(
    ctx: dict[str, Any], db_engine: AsyncEngine, migrated_db_url: str, executed_prompts: list[str]
) -> None:
    async def _go() -> None:
        org_id = await rt._seed_org(db_engine, "HitlRejectBdd")
        account_id = await rt._seed_account(db_engine, org_id, f"hitl-reject-bdd-{uuid.uuid4().hex[:8]}@test.local")
        pipe = await rt._seed_pipeline(db_engine, org_id, "PipeHitlRejectBdd", account_id)
        ctx["org_id"] = org_id
        ctx["backend_id"] = uuid.uuid4()
        ctx["gate_config"] = {
            "review_id": "hitl_review_a_b",
            "human_only": True,
            "overdue_threshold_minutes": 60,
            "required_team_id": None,
            **ctx["gate_extra"],
        }
        snap = await rt._seed_snapshot(db_engine, org_id, pipe, _graph(ctx))
        ctx["run_id"] = await rt._seed_run(db_engine, org_id, pipe, snap)
        await rt._interrupt_run(db_engine, migrated_db_url, org_id, ctx["run_id"], ctx["backend_id"], _FIXTURES)
        status, _ = await rt._run_status(db_engine, ctx["run_id"])
        assert status == "awaiting_human"

    _run(_go())
    # Only the gate's upstream node has run so far.
    assert _NODE_PROMPTS["b"] not in executed_prompts


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


def _reject_and_resume(ctx: dict[str, Any], db_engine: AsyncEngine, reason: str) -> None:
    from modulo.core.cron_helpers import _committed_decision_resume_data
    from modulo.db.rls import set_rls_org

    async def _go() -> None:
        org_id, run_id = ctx["org_id"], ctx["run_id"]
        review_id = await rt._read_review_id(db_engine, org_id, run_id)
        payload = {"action": "rejected", "review_id": review_id, "reason": reason}
        await rt._commit_decision(db_engine, org_id, run_id, review_id, decision="rejected", decision_payload=payload)
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            resume_data = await _committed_decision_resume_data(session, org_id, run_id)
        assert resume_data == payload
        # ``resume_run`` reports the outcome; the Then steps read the
        # authoritative run state back from the database.
        hub = await rt._run_executor_hub(ctx["backend_id"], _FIXTURES)()
        try:
            await rt.pe.resume_run(
                async_engine=db_engine, run_id=str(run_id), org_id=str(org_id), resume_data=resume_data
            )
        finally:
            rt.set_model_backend_hub(None)
            await hub.__aexit__(None, None, None)

    _run(_go())


@when(parsers.parse('the approver rejects the gate with reason "{reason}"'))
def _approver_rejects(ctx: dict[str, Any], db_engine: AsyncEngine, reason: str) -> None:
    _reject_and_resume(ctx, db_engine, reason)


@when("the gate is superseded by a newer gate")
def _gate_superseded(ctx: dict[str, Any], db_engine: AsyncEngine) -> None:
    _reject_and_resume(ctx, db_engine, SUPERSEDE_REASON)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


def _read(db_engine: AsyncEngine, ctx: dict[str, Any], reader: Callable[..., Coroutine[Any, Any, Any]]) -> Any:
    return _run(reader(db_engine, ctx["run_id"]))


@then(parsers.parse('the run status is "{status}"'))
def _status_is(ctx: dict[str, Any], db_engine: AsyncEngine, status: str) -> None:
    actual, completed_at = _read(db_engine, ctx, rt._run_status)
    assert actual == status
    assert completed_at is not None


@then(parsers.parse('the run error code is "{code}"'))
def _error_code_is(ctx: dict[str, Any], db_engine: AsyncEngine, code: str) -> None:
    assert _read(db_engine, ctx, rt._run_error_code) == code


@then("the run has no error code")
def _no_error_code(ctx: dict[str, Any], db_engine: AsyncEngine) -> None:
    assert _read(db_engine, ctx, rt._run_error_code) is None


@then(parsers.parse('the run executed node "{node}"'))
def _executed(executed_prompts: list[str], node: str) -> None:
    assert _NODE_PROMPTS[node] in executed_prompts


@then(parsers.parse('the run did not execute node "{node}"'))
def _not_executed(executed_prompts: list[str], node: str) -> None:
    assert _NODE_PROMPTS[node] not in executed_prompts

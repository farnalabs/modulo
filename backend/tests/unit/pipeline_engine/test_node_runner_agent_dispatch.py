"""Regression pins for the structural S3776 split of ``make_node_fn``.

The refactor extracted ``_dispatch_node`` (node-start gates + stub branches +
run overrides), ``_run_agent_node`` (render/invoke/validate/finalize),
``_build_repair_invoke`` and ``_stamp_schema_enforcement`` out of the
``make_node_fn`` closure without changing behaviour. These tests pin the two
invariants called out for the split:

1. ``result["_llm_next_node"]`` remains the SOLE LLM-routing signal: an agent
   node run in ``routing_mode="llm"`` stamps it from the model output's
   ``_next_node`` key (popped out of the output), and the compiled graph
   routes on it INSTEAD OF the static edge from the same source node.
2. The FAR-902 schema-enforcement record survives an agent node: it is
   carried off the result and persisted to ``run_node_outputs`` UNCHANGED,
   and never leaks into the returned node result (D3 ordering).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import modulo.core.pipeline_engine.node_runner as nr
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json


async def test_llm_next_node_overrides_static_edge() -> None:
    """The agent node's ``_llm_next_node`` signal beats the static edge.

    The ``decider`` node has BOTH a static (normal) edge to ``static-target``
    and a conditional routing edge labelled ``route_b`` -> ``path-b``. The
    mocked model emits ``_next_node: "route_b"``; the node must stamp it as
    ``result["_llm_next_node"]`` (removing it from the node output) and the
    compiled router must follow the labelled target, NOT the static edge.
    """
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "decider",
                "role": None,
                "routing_mode": "llm",
                "routing_prompt": "Choose route_b.",
                "prompt_template": "pick a path",
                "model_backend_id": str(uuid.uuid4()),
            },
            {"id": "path-b", "role": None},
            {"id": "static-target", "role": None},
        ],
        "edges": [
            {"source": "decider", "target": "path-b", "type": "conditional", "routing_label": "route_b"},
            {"source": "decider", "target": "static-target", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None

    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    with patch.object(
        nr,
        "_invoke_node_model",
        new=AsyncMock(return_value={"verdict": "b", "_next_node": "route_b"}),
    ):
        result = await compiled.ainvoke(initial_state, config)

    # The node stamped the routing signal and stripped it from its own output.
    assert result.get("_llm_next_node") == "route_b"
    decider_artifact = next(a for a in result["artifacts"] if a["node_id"] == "decider")
    assert "_next_node" not in decider_artifact["output"]

    # The signal routed the graph over the static edge.
    executed = [a["node_id"] for a in result["artifacts"]]
    assert "path-b" in executed
    assert "static-target" not in executed


async def test_enforcement_record_survives_agent_node() -> None:
    """An agent node's FAR-902 record reaches run_node_outputs unchanged.

    The node runs the full agent path (schema validation in lenient mode),
    carries the enforcement record OFF the result (it must not leak into the
    returned node result / node telemetry), and persists it to the
    attempt-keyed ``run_node_outputs`` row via the conformance-context
    session factory — with the payload byte-for-byte identical to what
    ``_finalize_node_result`` builds for the same inputs.
    """
    from modulo.db.models.run_node_outputs import RunNodeOutput

    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
    }
    node_def = {
        "id": "n-schema",
        "model_backend_id": str(uuid.uuid4()),
        "prompt_template": "summarise",
        "output_schema_json": schema,
    }
    fn = nr.make_node_fn(node_def)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(RunNodeOutput.__table__.create)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    run_id = uuid.uuid4()
    org_id = uuid.uuid4()

    # The exact record the node MUST persist, built from identical inputs.
    expected = nr._finalize_node_result(
        "n-schema",
        {"summary": "hello"},
        schema,
        None,
        mode="lenient",
        schema_id="unknown",
        schema_version=0,
        resolved_profile="verbatim",
        native_output=False,
    )["_schema_enforcement_record"]
    assert expected is not None

    prev_ctx = nr.get_conformance_ctx()
    nr.set_conformance_ctx(sf, org_id, None, None)
    try:
        with (
            patch.object(nr, "_run_conformance_gate", new=AsyncMock(return_value=False)),
            patch.object(nr, "_invoke_node_model", new=AsyncMock(return_value={"summary": "hello"})),
        ):
            result = await fn(
                {
                    "run_context": {"input": {}},
                    "_run_id": str(run_id),
                }
            )
    finally:
        nr._conformance_ctx_cv.set(prev_ctx)

    try:
        # Carried OFF the result — the record never leaks into node output.
        assert "_schema_enforcement_record" not in result
        assert result["artifacts"][0]["status"] == "completed"

        async with sf() as session:
            row = (await session.execute(select(RunNodeOutput).where(RunNodeOutput.node_id == "n-schema"))).scalar_one()
        assert row.run_id == run_id
        assert row.attempt_key == f"run:{run_id}:node:n-schema:agent"
        assert row.schema_enforcement_json == expected
    finally:
        await engine.dispose()


def test_build_repair_invoke_raises_when_hub_unavailable() -> None:
    """``_build_repair_invoke`` fails loudly when no ModelBackendHub exists.

    ``_build_repair_invoke`` captures the hub once and hands the returned
    callable to the strict-mode repair loop. When the hub is absent at call
    time the repair re-invoke MUST raise a clear ``RuntimeError`` rather than
    silently no-op or hand ``json.loads`` an un-awaited coroutine - a missing
    hub is a wiring error, and the node takes its documented hard-fail path.
    """
    with patch(
        "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
        return_value=None,
    ):
        repair_fn = nr._build_repair_invoke(str(uuid.uuid4()))
        assert repair_fn is not None
        with pytest.raises(RuntimeError, match="ModelBackendHub not available"):
            repair_fn("repair prompt")

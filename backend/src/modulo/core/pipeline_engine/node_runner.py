"""Factory that builds a cancellable LangGraph node function from a node definition.

Node types:
  - standard (agent):  agent/connector node; runs the node body, then checks for
                       outgoing HITL gate edges (handled externally via
                       intermediate gate nodes).
  - hitl_gate:         intermediate node inserted by build_graph_from_json for
                       every edge that carries a hitl_gate_config.  Calls
                       interrupt(gate_payload) and blocks until a human reviews
                       it, unless the effective autonomy level (from run_context
                       or pipeline default) bypasses the gate.
  - manual:            placeholder node for SDLC modeling.  No AI agent, no
                       connector binding, no model backend required.  The human
                       provides output directly via the HITL review UI.  Output
                       is validated against output_schema_id before the run
                       continues.  A log entry is recorded on completion.
                       Fields required: id, output_schema_id (optional).
                       Fields NOT required: agent_id, connector_binding,
                       model_backend_id.

Autonomy integration:
  - ``manual_approval`` (default):  gate interrupts for human review.
  - ``notify_on_complete``:         gate auto-approves and records an artifact;
                                    no interrupt is raised.
  - ``fully_autonomous``:           gate is silently skipped.
  - ``human_only`` on gate config:  overrides autonomy — always interrupts.
"""

import logging
from typing import Any

from langgraph.errors import NodeInterrupt

from modulo.core.pipeline_engine.decorator import cancellable_node
from modulo.core.run_context.autonomy import (
    effective_autonomy_level,
    should_notify_on_complete,
    should_skip_hitl_gate,
)

_log = logging.getLogger(__name__)


def make_node_fn(
    node_def: dict[str, Any],
    *,
    role: str | None = None,
    timeout: float | None = None,
) -> Any:
    """Return a decorated async node function for use in a StateGraph.

    At this phase the node body is a stub: it records node entry in state["artifacts"]
    and returns.  Real agent invocation is wired in once ModelBackendHub and
    ConnectorHub are plumbed into the run context (phase2-10 execution path).
    """
    node_id: str = str(node_def["id"])

    @cancellable_node(timeout=timeout, role=role)
    async def _node(state: dict[str, Any]) -> dict[str, Any]:
        artifacts: list[dict[str, Any]] = list(state.get("artifacts") or [])
        artifacts.append({"node_id": node_id, "status": "executed"})
        return {"artifacts": artifacts}

    _node.__name__ = f"node_{node_id}"
    return _node


def make_hitl_gate_fn(
    hitl_gate_config: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """Return a node function that raises a HITL interrupt.

    The node checks the effective autonomy level from ``run_context`` at
    runtime.  If the gate should be bypassed (autonomous mode) or
    auto-approved (notify mode), no interrupt is raised.

    On resume (via ``aupdate_state`` + ``astream_events(None, config)``),
    the node is re-invoked with ``state["_hitl_decision"]`` populated.
    It then returns artifacts reflecting the human's decision.
    """
    gate_id: str = hitl_gate_config.get("gate_id", "gate")
    human_only: bool = hitl_gate_config.get("human_only", False)

    async def _hitl_gate(state: dict[str, Any]) -> dict[str, Any]:
        # Determine effective autonomy level from run_context.
        run_context: dict[str, Any] = state.get("run_context") or {}
        pipeline_default: str | None = run_context.get("_pipeline_default_autonomy")
        autonomy = effective_autonomy_level(pipeline_default, run_context)
        human_only_effective: bool = human_only

        # human_only overrides everything — always interrupt.
        if human_only_effective:
            pass
        elif should_skip_hitl_gate(autonomy):
            # fully_autonomous: silently skip the gate.
            artifacts: list[dict[str, Any]] = list(state.get("artifacts") or [])
            artifacts.append(
                {
                    "node_id": gate_id,
                    "status": "skipped",
                    "autonomy": autonomy.value,
                }
            )
            return {"artifacts": artifacts}
        elif should_notify_on_complete(autonomy):
            # notify_on_complete: auto-approve, record notification artifact.
            artifacts = list(state.get("artifacts") or [])
            artifacts.append(
                {
                    "node_id": gate_id,
                    "status": "auto_approved",
                    "autonomy": autonomy.value,
                }
            )
            return {"artifacts": artifacts}

        # Check if this is a resume after human review.
        decision = state.get("_hitl_decision")
        if decision is not None:
            is_rejected = isinstance(decision, dict) and decision.get("action") == "rejected"
            result = "rejected" if is_rejected else "approved"
            out_artifacts: list[dict[str, Any]] = list(state.get("artifacts") or [])
            out_artifacts.append(
                {
                    "node_id": gate_id,
                    "status": "interrupted",
                    "result": result,
                    "human_data": decision,
                }
            )
            return {"artifacts": out_artifacts}

        # First invocation — store config and interrupt.
        hitl_gates: list[dict[str, Any]] = list(state.get("_hitl_gates") or [])
        hitl_gates.append(hitl_gate_config)
        state["_hitl_gates"] = hitl_gates

        # State mutations before the raise are persisted by the checkpointer.
        raise NodeInterrupt(
            {
                "gate_id": gate_id,
                "autonomy_level": autonomy.value,
                "human_only": human_only_effective,
                "overdue_threshold_minutes": hitl_gate_config.get("overdue_threshold_minutes"),
            }
        )

    _hitl_gate.__name__ = f"hitl_gate_{gate_id}"
    return _hitl_gate


def make_manual_node_fn(
    node_def: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """Return a node function for a manual-input node.

    The node immediately interrupts and waits for human output. On resume the
    output is validated against output_schema_id (if defined) before continuing.
    """
    node_id: str = str(node_def["id"])
    output_schema_json: dict[str, Any] | None = node_def.get("output_schema_json")
    manual_prompt: str = node_def.get("manual_prompt", "")

    async def _manual_node(state: dict[str, Any]) -> dict[str, Any]:
        # Check if this is a resume with human output.
        decision = state.get("_hitl_decision")
        if decision is not None and isinstance(decision, dict):
            resume_data = decision.get("output", decision)
            manual_output: dict[str, Any] | None = resume_data if isinstance(resume_data, dict) else None
            if output_schema_json and manual_output is not None:
                _validate_against_schema(manual_output, output_schema_json)

            out_artifacts: list[dict[str, Any]] = list(state.get("artifacts") or [])
            out_artifacts.append(
                {
                    "node_id": node_id,
                    "status": "completed",
                    "human_output": manual_output,
                }
            )

            _log.info(
                "manual_node.completed",
                extra={
                    "node_id": node_id,
                    "has_output_schema": output_schema_json is not None,
                },
            )

            return {
                "artifacts": out_artifacts,
                "manual_output": manual_output,
            }

        # First invocation — record pending artifact and interrupt.
        artifacts: list[dict[str, Any]] = list(state.get("artifacts") or [])
        artifacts.append({"node_id": node_id, "status": "awaiting_human"})
        state["artifacts"] = artifacts

        _log.info(
            "manual_node.awaiting_human",
            extra={
                "node_id": node_id,
                "prompt": manual_prompt or "",
            },
        )

        raise NodeInterrupt(
            {
                "manual": True,
                "node_id": node_id,
                "prompt": manual_prompt,
                "output_schema_id": node_def.get("output_schema_id"),
            }
        )

    _manual_node.__name__ = f"manual_{node_id}"
    return _manual_node


def _validate_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Lightweight field-presence validation against a JSON schema.

    Raises ValueError on first missing required field.  Full JSON Schema
    validation (via a library like `jsonschema`) is deferred to v1.
    """
    required: list[str] = schema.get("required", [])
    for field in required:
        if field not in data:
            raise ValueError(f"Manual output missing required field {field!r} (required: {required})")

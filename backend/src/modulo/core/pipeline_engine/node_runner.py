"""Factory that builds a cancellable LangGraph node function from a node definition.

Node types:
  - standard (agent):  agent/connector node; runs the node body, then checks for
                        outgoing HITL gate edges (handled externally via
                        intermediate gate nodes).
  - hitl_gate:         intermediate node inserted by build_graph_from_json for
                        every edge that carries a hitl_gate_config.  Calls
                        interrupt(gate_payload) and blocks until a human reviews
                        it, unless the effective autonomy level (from run_context
                        or pipeline default) bypasses the gate.  Also supports
                        conditional gating via a JMESPath ``condition`` on the
                        gate config, and eval-before-interrupt for node-scoped
                        eval definitions.
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
  - ``human_only`` on gate config:  overrides autonomy —  always interrupts.

Conditional gating ((Section 8.17):
  - ``condition`` on ``hitl_gate_config``:  JMESPath expression evaluated
    against the current state (upstream node output).  If falsy the gate is
    skipped.  If truthy or absent the gate proceeds to autonomy checks.

Eval-before-interrupt ((Section 8.17):
  - ``eval_definitions``:  list of ``EvalDefinition`` DTOs scoped to the
    upstream node.  Evaluated *after* the condition check but *before* the
    interrupt.  If any eval with ``failure_behaviour='block'`` fails, an
    ``EvalBlockedError`` is raised instead of a ``GraphInterrupt``.
"""

import asyncio
import base64
import json
import logging
import os
import re as _re
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any

import jinja2
import jmespath
from jinja2.sandbox import SandboxedEnvironment
from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalResult
from modulo.core.pipeline_engine.decorator import cancellable_node
from modulo.core.pipeline_engine.input_truncation import truncate_input
from modulo.core.run_context.autonomy import (
    effective_autonomy_level,
    should_notify_on_complete,
    should_skip_hitl_gate,
)
from modulo.db.models.eval_result import EvalResult as EvalResultModel
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)


_is_truthy = bool

_MAX_ARTIFACT_LOG = 102400
_MAX_OTEL_LOG_ATTR = 32768
_MAX_ERROR_MSG = 500

_OUTPUT_READ_TIMEOUT = 30.0  # max seconds to wait for sandbox output after command times out
_DECORATOR_GRACE = 5.0  # scheduling + finally-block margin for decorator safety net
_SANDBOX_IO_TIMEOUT = 30.0  # max seconds for a single sandbox file read/write


def _evaluate_eval_condition(score: float, threshold: float, operator: str) -> bool:
    """Evaluate an eval-reference condition using the given operator.

    Returns True when the condition is satisfied (meaning the gate should fire/interrupt).
    Returns False when the condition is not satisfied (gate should be skipped).
    """
    match operator:
        case "lt":
            return score < threshold
        case "gt":
            return score > threshold
        case "lte":
            return score <= threshold
        case "gte":
            return score >= threshold
        case "eq":
            return score == threshold
        case "neq":
            return score != threshold
        case _:
            _log.warning(
                "hitl_gate.unknown_operator", extra={"operator": operator, "score": score, "threshold": threshold}
            )
            return False


def make_node_fn(
    node_def: dict[str, Any],
    *,
    role: str | None = None,
    timeout: float | None = None,
    max_input_length: int | None = None,
    token_budget: int | None = None,
) -> Any:
    """Return a decorated async node function for use in a StateGraph.

    Renders the agent's prompt template against state via SandboxedEnvironment,
    invokes the configured model backend via ModelBackendHub, validates the
    output against the output schema (if defined), and returns the result
    in state["artifacts"] and state["output"].

    Nodes without a ``model_backend_id`` (connector-bindings, etc.) return a
    stub artifact without invoking a model.

    When *max_input_length* is set, input text from ``run_context["input"]`` is
    truncated before being passed to the LLM.

    When *token_budget* is set, per-node token budget is enforced at the
    executor level via ``node_token_budgets`` during ``_stream_graph()``.
    """

    node_id: str = str(node_def["id"])

    @cancellable_node(timeout=timeout, role=role)
    async def _node(state: dict[str, Any]) -> dict[str, Any]:
        run_context: dict[str, Any] = state.get("run_context") or {}
        raw_input = run_context.get("input", {})

        # Truncate input if max_input_length is configured for this agent.
        if max_input_length is not None and isinstance(raw_input, str):
            run_context["input"] = truncate_input(raw_input, max_input_length)

        # Get agent data from node_def (embedded at snapshot creation).
        prompt_template = node_def.get("prompt_template") or ""
        model_backend_id_str = node_def.get("model_backend_id")
        output_schema_json = node_def.get("output_schema_json")

        # If no model_backend_id, fall back to stub behavior
        # (connector_binding nodes, manual nodes routed through wrong path, etc.).
        if not model_backend_id_str:
            return {"artifacts": [{"node_id": node_id, "status": "executed"}]}

        env = SandboxedEnvironment()
        template = env.from_string(prompt_template)
        template_vars: dict[str, Any] = {
            "state": state,
            "run_context": run_context,
            "input": raw_input,
        }
        # Inject resolved parameters as {{ parameter.<key> }}.
        resolved = node_def.get("_resolved_parameters")
        if isinstance(resolved, dict):
            template_vars["parameter"] = resolved
        rendered_prompt = template.render(**template_vars)

        # Append routing prompt for LLM routing nodes.
        routing_mode: str | None = node_def.get("routing_mode")
        if routing_mode == "llm":
            routing_prompt: str = node_def.get("routing_prompt", "")
            if routing_prompt:
                rendered_prompt = rendered_prompt + "\n\n" + routing_prompt

        # Get ModelBackendHub from ContextVar.
        from modulo.core.pipeline_engine.decorator import get_model_backend_hub

        hub = get_model_backend_hub()
        if hub is None:
            raise RuntimeError(f"ModelBackendHub not available for node {node_id!r}")

        # Resolve backend ID and invoke the model.
        backend_id = uuid.UUID(model_backend_id_str)
        backend = await hub.get(backend_id)

        messages = [HumanMessage(content=rendered_prompt)]
        response = await backend.invoke(messages)

        content = response.content if hasattr(response, "content") else str(response)
        output_data: Any = content
        if isinstance(content, str):
            with suppress(json.JSONDecodeError, ValueError):
                output_data = json.loads(content)

        # Validate against output schema if defined.
        if isinstance(output_schema_json, dict) and isinstance(output_data, dict):
            _validate_against_schema(output_data, output_schema_json)

        result: dict[str, Any] = {
            "artifacts": [{"node_id": node_id, "status": "completed", "output": output_data}],
            "output": output_data,
        }

        # Extract _next_node from LLM routing output for the router.
        if routing_mode == "llm" and isinstance(output_data, dict):
            next_node = output_data.pop("_next_node", None)
            if next_node is not None:
                result["_llm_next_node"] = next_node

        return result

    _node.__name__ = f"node_{node_id}"
    return _node


def make_hitl_gate_fn(
    hitl_gate_config: dict[str, Any],
    *,
    timeout: float | None = None,
    eval_definitions: Sequence[EvalDefinition] | None = None,
    session_factory: Callable[..., Any] | None = None,
    org_id: uuid.UUID | None = None,
) -> Any:
    """Return a node function that raises a HITL interrupt.

    The node checks the effective autonomy level from ``run_context`` at
    runtime.  If the gate should be bypassed (autonomous mode) or
    auto-approved (notify mode), no interrupt is raised.

    Conditional gating:
      If ``hitl_gate_config`` contains a ``condition`` JMESPath expression,
      it is evaluated against the current state.  If the result is falsy
      the gate is skipped entirely (no autonomy or decision checks).

    Eval-before-interrupt:
      If ``eval_definitions`` is provided, each definition is evaluated
      against the current state *after* the condition check but *before*
      the interrupt.  Any eval with ``failure_behaviour='block'`` that
      fails raises ``EvalBlockedError``, preventing the interrupt.

      If ``session_factory`` is provided, eval results are persisted to
      the ``eval_results`` table so that post-run suite-level threshold
      checks (``_check_eval_suites``) can read them.

    On resume (via ``aupdate_state`` + ``astream_events(None, config)``),
    the node is re-invoked with ``state["_hitl_decision"]`` populated.
    It then returns artifacts reflecting the human's decision.
    """
    gate_id: str = hitl_gate_config.get("gate_id", "gate")
    human_only: bool = hitl_gate_config.get("human_only", False)
    condition_expr: str | None = hitl_gate_config.get("condition")
    eval_condition_raw: dict[str, Any] | None = hitl_gate_config.get("eval_condition")
    required_team_id: str | None = hitl_gate_config.get("required_team_id")

    async def _hitl_gate(state: dict[str, Any]) -> dict[str, Any]:
        # --- Resume check —  always first so condition/evals aren't re-evaluated. ---
        decision = state.get("_hitl_decision")
        if decision is not None:
            action = decision.get("action") if isinstance(decision, dict) else None
            if action == "deliver_manual":
                manual_output = decision.get("output", {})
                return {
                    "artifacts": [
                        {
                            "node_id": gate_id,
                            "status": "interrupted",
                            "result": "delivered_manual",
                            "human_data": decision,
                            "manual_output": manual_output,
                        }
                    ],
                    "output": manual_output,
                }
            is_rejected = action == "rejected"
            result_status = "rejected" if is_rejected else "approved"
            gate_result: dict[str, Any] = {
                "artifacts": [
                    {
                        "node_id": gate_id,
                        "status": "interrupted",
                        "result": result_status,
                        "human_data": decision,
                    }
                ],
            }
            # If the human provided modified output, write it into state so
            # downstream nodes receive the human's version instead of the
            # original agent output.
            if isinstance(decision, dict) and "modified_output" in decision:
                gate_result["output"] = decision["modified_output"]
            return gate_result

        # --- Conditional gate ((Section 8.17) —  evaluate condition against state. ---
        if condition_expr:
            try:
                compiled = jmespath.compile(condition_expr)
            except jmespath.exceptions.JMESPathError:
                _log.exception("hitl_gate.invalid_condition", extra={"condition": condition_expr})
                raise ValueError(f"Invalid HITL gate condition expression: {condition_expr}") from None
            result = compiled.search(state)
            if not _is_truthy(result):
                # Condition falsy —  skip the gate entirely.
                return {
                    "artifacts": [
                        {
                            "node_id": gate_id,
                            "status": "condition_skipped",
                            "condition": condition_expr,
                            "condition_result": result,
                        }
                    ],
                }

        # --- Eval-before-interrupt ((Section 8.17) —  run node-scoped evals. ---
        eval_results_by_name: dict[str, EvalResult] = {}
        if eval_definitions:
            engine = EvalEngine()
            for eval_def in eval_definitions:
                eval_result = engine.evaluate(state, eval_def)
                eval_results_by_name[eval_def.name] = eval_result
                _log.info(
                    "hitl_gate.eval_result",
                    extra={
                        "gate_id": gate_id,
                        "eval_name": eval_def.name,
                        "eval_id": str(eval_def.id),
                        "passed": eval_result.passed,
                        "score": eval_result.score,
                        "detail": eval_result.detail,
                    },
                )
            # If any block eval failed, EvalBlockedError was raised above.

            # Persist eval results to the eval_results table so post-run
            # suite-level threshold checks can read them.
            if session_factory is not None and org_id is not None:
                try:
                    _run_id: uuid.UUID | None = state.get("_run_id")
                    if _run_id is not None:
                        async with session_factory() as session, session.begin():
                            await set_rls_org(session, org_id)
                            for eval_def in eval_definitions:
                                eval_result = eval_results_by_name[eval_def.name]
                                node_uuid: uuid.UUID | None = uuid.UUID(eval_def.node_id) if eval_def.node_id else None
                                db_result = EvalResultModel(
                                    organisation_id=org_id,
                                    run_id=_run_id,
                                    node_id=node_uuid,
                                    eval_id=eval_def.id,
                                    passed=eval_result.passed,
                                    score=eval_result.score,
                                    detail=eval_result.detail,
                                )
                                session.add(db_result)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("hitl_gate.persist_eval_failed")

        # --- Eval-reference condition check ((Section 8.17 v1) —  evaluate condition
        # against captured eval results. ---
        if eval_condition_raw is not None and eval_results_by_name:
            eval_name: str = eval_condition_raw.get("eval_name", "")
            threshold: float = eval_condition_raw.get("threshold", 0.0)
            operator: str = eval_condition_raw.get("operator", "lt")
            matched_result = eval_results_by_name.get(eval_name)
            if matched_result is not None:
                score: float = matched_result.score or 0.0
                condition_true: bool = _evaluate_eval_condition(score, threshold, operator)
                _log.info(
                    "hitl_gate.eval_condition",
                    extra={
                        "gate_id": gate_id,
                        "eval_name": eval_name,
                        "score": score,
                        "threshold": threshold,
                        "operator": operator,
                        "condition_true": condition_true,
                    },
                )
                if not condition_true:
                    return {
                        "artifacts": [
                            {
                                "node_id": gate_id,
                                "status": "condition_skipped",
                                "condition": eval_condition_raw,
                                "condition_result": False,
                            }
                        ],
                    }

        # Determine effective autonomy level from run_context.
        run_context: dict[str, Any] = state.get("run_context") or {}
        pipeline_default: str | None = run_context.get("_pipeline_default_autonomy")
        autonomy = effective_autonomy_level(pipeline_default, run_context)
        human_only_effective: bool = human_only

        # human_only overrides everything — always interrupt.
        if not human_only_effective and should_skip_hitl_gate(autonomy):
            # fully_autonomous: silently skip the gate.
            return {
                "artifacts": [
                    {
                        "node_id": gate_id,
                        "status": "skipped",
                        "autonomy": autonomy.value,
                    }
                ],
            }
        if not human_only_effective and should_notify_on_complete(autonomy):
            # notify_on_complete: auto-approve, record notification artifact.
            return {
                "artifacts": [
                    {
                        "node_id": gate_id,
                        "status": "auto_approved",
                        "autonomy": autonomy.value,
                    }
                ],
            }

        # First invocation —  store config and interrupt.
        hitl_gates: list[dict[str, Any]] = list(state.get("_hitl_gates") or [])
        hitl_gates.append(hitl_gate_config)
        state["_hitl_gates"] = hitl_gates

        # State mutations before the interrupt are persisted by the checkpointer.
        decision = interrupt(
            {
                "gate_id": gate_id,
                "autonomy_level": autonomy.value,
                "human_only": human_only_effective,
                "overdue_threshold_minutes": hitl_gate_config.get("overdue_threshold_minutes"),
                "required_team_id": required_team_id,
            }
        )
        return await _hitl_gate({**state, "_hitl_decision": decision})

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

            _log.info(
                "manual_node.completed",
                extra={
                    "node_id": node_id,
                    "has_output_schema": output_schema_json is not None,
                },
            )

            return {
                "artifacts": [
                    {
                        "node_id": node_id,
                        "status": "completed",
                        "human_output": manual_output,
                    }
                ],
                "manual_output": manual_output,
            }

        # First invocation —  record pending artifact and interrupt.
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

        decision = interrupt(
            {
                "manual": True,
                "node_id": node_id,
                "prompt": manual_prompt,
                "output_schema_id": node_def.get("output_schema_id"),
            }
        )
        return await _manual_node({**state, "_hitl_decision": decision})

    _manual_node.__name__ = f"manual_{node_id}"
    return _manual_node


def make_connector_fn(
    node_def: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """
    Return a decorated async node function that resolves a connector
    from the ConnectorHub and executes a connector action (query/write).

    The node_def must have a 'connector_binding' dict with:
      - instance_id: uuid of the ConnectorInstance
      - type: connector type (e.g. 'shell')
      - operation: 'query' or 'write' (optional, default 'query')
      - input: dict of input parameters (optional)
    """

    node_id: str = str(node_def["id"])
    binding = node_def.get("connector_binding") or {}
    op: str = binding.get("operation", "query")

    @cancellable_node(timeout=timeout)
    async def _connector_node(state: dict[str, Any]) -> dict[str, Any]:
        from modulo.connectors.base import ConnectorPayload, ConnectorQuery
        from modulo.core.pipeline_engine.decorator import get_connector_hub

        hub = get_connector_hub()
        if hub is None:
            return {"artifacts": [{"node_id": node_id, "status": "executed", "output": {"note": "no connector hub"}}]}

        instance_id_str = binding.get("instance_id")
        if not instance_id_str:
            return {"artifacts": [{"node_id": node_id, "status": "failed", "error": "no connector instance_id"}]}

        import uuid as _uuid

        try:
            connector = hub.get(_uuid.UUID(str(instance_id_str)))
        except Exception as _conn_exc:
            return {"artifacts": [{"node_id": node_id, "status": "failed", "error": f"connector error: {_conn_exc}"}]}

        run_context = state.get("run_context") or {}
        raw_input = run_context.get("input", {})
        resource: str = binding.get("resource", "command")
        filters = dict(binding.get("filters", {}))
        data = dict(binding.get("data", {}))
        if isinstance(raw_input, dict):
            filters.update({k: v for k, v in raw_input.items() if k not in data})
            data.update({k: v for k, v in raw_input.items() if k not in filters})

        # Ensure provider_ref for shell connectors
        if "provider_ref" not in filters and "provider_ref" not in data:
            filters["provider_ref"] = "/"

        try:
            if op == "write":
                payload = ConnectorPayload(resource=resource, data=data)
                result = await connector.write(payload)
            else:
                query = ConnectorQuery(resource=resource, filters=filters)
                result = await connector.query(query)
        except Exception as exc:
            return {"artifacts": [{"node_id": node_id, "status": "failed", "error": str(exc)}]}

        return {
            "artifacts": [{"node_id": node_id, "status": "completed", "output": result}],
            "output": result,
        }

    _connector_node.__name__ = f"connector_{node_id}"
    return _connector_node


def make_sandbox_agent_fn(
    node_def: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """Return a decorated async node function that dispatches work to an external
    agent runtime in an E2B sandbox.

    The node_def must have:
      - agent_prompt: str —  Jinja2 template rendered against state
      - template_id: str —  E2B sandbox template ID (default "base")
      - agent_command: str —  command to run inside the sandbox
        (default: "claude --output-json /home/user/prompt.md")
      - output_schema_json: dict | None —  optional output schema validation
      - timeout_seconds: int —  max wall-clock time (default 600)
      - context_files: dict[str, str] —  optional files to write into the sandbox
        keyed by path

    The node creates an E2B sandbox, writes the rendered prompt + context files,
    runs the external agent, reads structured output from /home/user/output.json,
    and tears down the sandbox. Wall-clock time and exit code are captured
    natively —  even on failure.
    """

    _secret_ref_re = _re.compile(r"^\{\{\s*secrets\.(\w+)\s*\}\}$")

    def _resolve_env_vars(env_vars: dict[str, str]) -> dict[str, str]:
        """Resolve {{ secrets.KEY }} references in env var values."""
        resolved: dict[str, str] = {}
        for key, value in env_vars.items():
            m = _secret_ref_re.match(str(value))
            if m:
                secret_key = m.group(1)
                import os as _os

                resolved_value = _os.environ.get(secret_key)
                if resolved_value is None:
                    import logging as _logging

                    _logging.getLogger(__name__).warning(
                        "env_var.secret_ref_not_found", extra={"key": key, "secret_key": secret_key}
                    )
                    resolved[key] = ""
                else:
                    resolved[key] = resolved_value
            else:
                resolved[key] = value
        return resolved

    node_id: str = str(node_def["id"])
    agent_prompt_template: str = node_def.get("agent_prompt") or ""
    template_id: str = node_def.get("template_id", "base")
    env_vars_extra: dict[str, str] = _resolve_env_vars(node_def.get("env_vars") or {})
    commands_concatenation_string: str = node_def.get("commands_concatenation_string", " && ")
    agent_commands_raw: list[str] | None = node_def.get("agent_commands")
    agent_command_raw: str | None = node_def.get("agent_command")
    if agent_commands_raw and len(agent_commands_raw) > 0:
        agent_command = commands_concatenation_string.join(agent_commands_raw)
    elif agent_command_raw:
        agent_command = agent_command_raw
    else:
        agent_command = "claude --output-json /home/user/prompt.md"
    output_schema_json: dict[str, Any] | None = node_def.get("output_schema_json")
    sandbox_timeout: int = node_def.get("timeout_seconds", 600)
    context_files: dict[str, str] = node_def.get("context_files") or {}

    from e2b import AsyncSandbox  # type: ignore[import-untyped]
    from opentelemetry import trace as _otel_trace

    @cancellable_node(
        timeout=(timeout or sandbox_timeout) + _OUTPUT_READ_TIMEOUT + _DECORATOR_GRACE,
        role="sandbox_agent",
    )
    async def _sandbox_agent(state: dict[str, Any]) -> dict[str, Any]:

        run_context: dict[str, Any] = state.get("run_context") or {}
        raw_input: Any = run_context.get("input", {})

        env = SandboxedEnvironment()
        template = env.from_string(agent_prompt_template)
        template_vars: dict[str, Any] = {
            "state": state,
            "run_context": run_context,
            "input": raw_input,
        }
        resolved = node_def.get("_resolved_parameters")
        if isinstance(resolved, dict):
            template_vars["parameter"] = resolved

        run_id: str = str(state.get("_run_id", ""))
        pipeline_id: str = str(state.get("_pipeline_id", ""))
        org_id: str = str(state.get("_org_id", ""))

        try:
            rendered_prompt = template.render(**template_vars)
        except jinja2.UndefinedError as e:
            _log.warning("Prompt template UndefinedError for run %s: %s", run_id, e)
            return {
                "status": "skipped",
                "summary": f"Skipped: prompt template references missing input fields ({e})",
                "agent_stdout": "",
                "agent_stderr": "",
                "exit_code": 0,
            }

        start_time = time.monotonic()
        sandbox: AsyncSandbox | None = None

        _stdout_len = 0
        _stderr_len = 0

        try:
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(template=template_id, timeout=sandbox_timeout),
                timeout=min(sandbox_timeout, 120),
            )
            assert sandbox is not None, "Sandbox was not created before use"
            for path, content in context_files.items():
                if path.endswith(".b64"):
                    content = base64.b64decode(content).decode()
                    path = path[:-4]
                await asyncio.wait_for(sandbox.files.write(path, content), timeout=_SANDBOX_IO_TIMEOUT)

            await asyncio.wait_for(
                sandbox.files.write("/home/user/prompt.md", rendered_prompt),
                timeout=_SANDBOX_IO_TIMEOUT,
            )

            _input_json = json.dumps(raw_input)
            if len(_input_json) > 10240:
                _input_json = json.dumps(
                    {"_truncated": True, "_key_count": len(raw_input) if isinstance(raw_input, dict) else 0}
                )

            try:
                cmd_result = await asyncio.wait_for(
                    sandbox.commands.run(
                        agent_command,
                        timeout=sandbox_timeout,
                        envs={
                            # System env vars first -- provide defaults from the host.
                            # DO NOT move env_vars_extra before these. Pipelines need
                            # to override GITHUB_TOKEN for identity separation (e.g.
                            # PR Reviewer uses modulo-reviewbot PAT, not the system
                            # default farnalabs bot). The reserved prefix validator
                            # already prevents overriding MODULO_* vars.
                            "MODULO_RUN_ID": run_id,
                            "MODULO_PIPELINE_ID": pipeline_id,
                            "MODULO_ORG_ID": org_id,
                            "MODULO_INPUT_PAYLOAD": _input_json,
                            "APP_MODULO_OPENCODE_API_KEY": os.environ.get("APP_MODULO_OPENCODE_API_KEY", ""),
                            "GITHUB_TOKEN": os.environ.get("GITHUB_DOGFOOD_PAT_ALL", "")
                            or os.environ.get("GITHUB_DOGFOOD_PAT_WR", "")
                            or os.environ.get("GITHUB_TOKEN", ""),
                            # Pipeline env vars last -- override system defaults.
                            # See comment above for why ordering is critical.
                            **env_vars_extra,
                        },
                    ),
                    timeout=sandbox_timeout,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                _log.warning(
                    "sandbox_agent.command_timed_out",
                    extra={
                        "node_id": node_id,
                        "timeout": sandbox_timeout,
                    },
                )
                cmd_result = None
            except Exception as _cee:
                _log.exception(
                    "sandbox_agent.command_failed",
                    extra={
                        "node_id": node_id,
                        "exc_type": type(_cee).__name__,
                        "exc_msg": str(_cee)[:_MAX_ERROR_MSG],
                    },
                )
                cmd_result = getattr(_cee, "result", None) or _cee

            elapsed = time.monotonic() - start_time
            exit_code: int = getattr(cmd_result, "exit_code", -1)
            agent_stdout_raw: str = getattr(cmd_result, "stdout", "") or ""
            agent_stderr_raw: str = getattr(cmd_result, "stderr", "") or ""
            _stdout_len = len(agent_stdout_raw)
            _stderr_len = len(agent_stderr_raw)
            agent_stdout = agent_stdout_raw[:_MAX_ARTIFACT_LOG]
            agent_stderr = agent_stderr_raw[:_MAX_ARTIFACT_LOG]

            raw_output: str = ""
            output_json: Any = None
            try:
                _remaining_after_cmd = max(_OUTPUT_READ_TIMEOUT, sandbox_timeout - (time.monotonic() - start_time))
                raw_output = await asyncio.wait_for(
                    sandbox.files.read(
                        "/home/user/output.json",
                        request_timeout=_remaining_after_cmd,
                    ),
                    timeout=_remaining_after_cmd,
                )
                output_json = json.loads(raw_output)
            except Exception:
                _log.info(
                    "sandbox_agent.no_output_json",
                    extra={"node_id": node_id, "exit_code": exit_code},
                )

            _span = _otel_trace.get_current_span()
            if _span.is_recording():
                _span.add_event(
                    "sandbox.agent.output",
                    {
                        "stdout": agent_stdout[:_MAX_OTEL_LOG_ATTR],
                        "stderr": agent_stderr[:_MAX_OTEL_LOG_ATTR],
                        "stdout_length": _stdout_len,
                        "stderr_length": _stderr_len,
                    },
                )

            if isinstance(output_schema_json, dict) and isinstance(output_json, dict):
                try:
                    _validate_against_schema(output_json, output_schema_json)
                except ValueError:
                    _log.exception(
                        "sandbox_agent.schema_validation_failed",
                        extra={"node_id": node_id},
                    )
                    elapsed = time.monotonic() - start_time
                    return {
                        "artifacts": [
                            {
                                "node_id": node_id,
                                "status": "failed",
                                "output": {
                                    "status": "failed",
                                    "summary": "Output failed schema validation",
                                    "exit_code": exit_code,
                                    "wall_clock_time_ms": int(elapsed * 1000),
                                    "output_json": output_json,
                                    "agent_stdout": agent_stdout,
                                    "agent_stderr": agent_stderr,
                                },
                            }
                        ],
                        "output": {
                            "status": "failed",
                            "summary": "Output failed schema validation",
                            "wall_clock_time_ms": int(elapsed * 1000),
                            "agent_stdout": agent_stdout,
                            "agent_stderr": agent_stderr,
                        },
                    }

            status: str = "completed" if exit_code == 0 else "failed"
            result_summary: str = ""
            changed_files: list[str] = []
            pr_url: str = ""

            if isinstance(output_json, dict):
                result_summary = output_json.get("summary", "")
                changed_files = output_json.get("changed_files", [])
                pr_url = output_json.get("pr_url", "")

            return {
                "artifacts": [
                    {
                        "node_id": node_id,
                        "status": status,
                        "output": {
                            "status": status,
                            "summary": result_summary,
                            "changed_files": changed_files,
                            "pr_url": pr_url,
                            "exit_code": exit_code,
                            "wall_clock_time_ms": int(elapsed * 1000),
                            "output_json": output_json,
                            "agent_stdout": agent_stdout,
                            "agent_stderr": agent_stderr,
                        },
                    }
                ],
                "output": {
                    "status": status,
                    "summary": result_summary,
                    "wall_clock_time_ms": int(elapsed * 1000),
                    "agent_stdout": agent_stdout,
                    "agent_stderr": agent_stderr,
                },
            }

        except asyncio.CancelledError:
            raise
        except Exception as _exc:
            elapsed = time.monotonic() - start_time
            import traceback as _tb

            _exc_type = type(_exc).__name__
            _exc_msg = str(_exc)[:_MAX_ERROR_MSG]
            _exc_tb = _tb.format_exc()
            _log.exception(
                "sandbox_agent.execution_failed",
                extra={
                    "node_id": node_id,
                    "elapsed_ms": int(elapsed * 1000),
                    "exc_type": _exc_type,
                    "exc_msg": _exc_msg,
                },
            )
            _span = _otel_trace.get_current_span()
            if _span.is_recording():
                _span.add_event(
                    "sandbox.agent.output",
                    {
                        "stdout": locals().get("agent_stdout", "")[:_MAX_OTEL_LOG_ATTR],
                        "stderr": locals().get("agent_stderr", "")[:_MAX_OTEL_LOG_ATTR],
                        "stdout_length": _stdout_len,
                        "stderr_length": _stderr_len,
                    },
                )
            _exc_stdout = locals().get("agent_stdout", "")
            _exc_stderr = locals().get("agent_stderr", "")
            return {
                "artifacts": [
                    {
                        "node_id": node_id,
                        "status": "failed",
                        "output": {
                            "status": "failed",
                            "summary": "Sandbox agent execution failed",
                            "error_type": _exc_type,
                            "error_message": _exc_msg,
                            "exit_code": -1,
                            "wall_clock_time_ms": int(elapsed * 1000),
                            "agent_stdout": _exc_stdout,
                            "agent_stderr": _exc_stderr,
                        },
                    }
                ],
                "output": {
                    "status": "failed",
                    "summary": "Sandbox agent execution failed",
                    "wall_clock_time_ms": int(elapsed * 1000),
                    "agent_stdout": _exc_stdout,
                    "agent_stderr": _exc_stderr,
                },
            }
        finally:
            if sandbox is not None:
                try:
                    await asyncio.wait_for(
                        sandbox.kill(request_timeout=_OUTPUT_READ_TIMEOUT),
                        timeout=_OUTPUT_READ_TIMEOUT,
                    )
                except Exception:
                    _log.exception(
                        "sandbox_agent.kill_failed",
                        extra={"node_id": node_id},
                    )

    _sandbox_agent.__name__ = f"sandbox_agent_{node_id}"
    return _sandbox_agent


def _validate_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Lightweight field-presence validation against a JSON schema.

    Raises ValueError on first missing required field.  Full JSON Schema
    validation (via a library like `jsonschema`) is deferred to v1.
    """
    required: list[str] = schema.get("required", [])
    for field in required:
        if field not in data:
            raise ValueError(f"Manual output missing required field {field!r} (required: {required})")

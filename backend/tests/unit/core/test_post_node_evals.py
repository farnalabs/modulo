"""FAR-311 regression tests: post-node evals validate the node's CONTRACT output.

A sandbox_agent node's stored output is ``artifacts[0].output.output_json``
(see ``node_output_split._split_sandbox_agent``); the outer ``output``
envelope key is telemetry-style (status/summary/cost fields) and does NOT
carry the agent's return fields. Before the fix ``_run_post_node_evals``
validated ``envelope["output"]`` — so an eval whose ``then`` branch required
``pr_url`` + ``changed_files`` failed for EVERY completed PR review
(``eval_failed`` / ``error_code: eval.blocked``).

These tests pin: an envelope whose artifact output carries pr_url/changed_files
PASSES the schema eval, and the engine validates the artifact-level contract
output (not the outer envelope ``output``).
"""

import uuid
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from modulo.core.eval_engine import EvalBlockedError, EvalDefinition, EvalEngine, EvalType
from modulo.core.node_output_split import resolve_node_contract_output, split_node_output
from modulo.core.pipeline_engine.executor import PipelineExecutor, _resolve_post_node_eval_target

# Mirrors the FAR-301 PR-review eval shape: a completed node must expose
# ``pr_url`` AND ``changed_files``. Applied to the telemetry-style outer
# ``output`` envelope (which carries only status/summary/cost) it MUST fail;
# applied to the agent's real contract output it MUST pass.
PR_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string"}},
    "if": {"properties": {"status": {"const": "completed"}}},
    "then": {"required": ["pr_url", "changed_files"]},
    "required": ["status"],
}


def _key_eval_def(name: str = "pr-review") -> EvalDefinition:
    return EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        pipeline_id=uuid4(),
        node_id="reviewer",
        name=name,
        eval_type=EvalType.JSON_SCHEMA,
        config={"schema": PR_REVIEW_SCHEMA},
        failure_behaviour="block",
    )


def _sandbox_agent_envelope(contract_output: dict[str, Any] | None) -> dict[str, Any]:
    """A realistic completed sandbox_agent envelope (FAR-311' failing shape).

    The outer ``output`` key is the telemetry-style summary — it carries
    status/summary/cost but never ``pr_url`` / ``changed_files``. The agent's
    contract output lives at ``artifacts[0].output.output_json``.
    """
    artifact_output: dict[str, Any] = {
        "status": "completed",
        "summary": "reviewed the PR",
        "exit_code": 0,
        "wall_clock_time_ms": 1234,
        "cost_estimate_usd": 0.01,
    }
    if contract_output is not None:
        artifact_output["output_json"] = contract_output
    return {
        "artifacts": [
            {
                "node_id": "reviewer",
                "status": "completed",
                "output": artifact_output,
            }
        ],
        "output": {
            "status": "completed",
            "summary": "reviewed the PR",
            "wall_clock_time_ms": 1234,
            "cost_estimate_usd": 0.01,
            "agent_stdout": "",
            "agent_stderr": "",
        },
    }


def _executor() -> PipelineExecutor:
    return PipelineExecutor(MagicMock())


class TestResolvePostNodeEvalTarget:
    def test_sandbox_agent_returns_artifact_output_json(self) -> None:
        envelope = _sandbox_agent_envelope(
            {"status": "completed", "pr_url": "https://x/pull/1", "changed_files": ["a"]}
        )
        target = _resolve_post_node_eval_target("reviewer", envelope, {"reviewer": "sandbox_agent"})
        assert target is not envelope["output"]
        assert target == {"status": "completed", "pr_url": "https://x/pull/1", "changed_files": ["a"]}

    def test_agent_returns_outer_output(self) -> None:
        envelope = {"output": {"summary": "done"}, "summary": "top"}
        target = _resolve_post_node_eval_target("a", envelope, {"a": "agent"})
        assert target == {"summary": "done"}

    def test_connector_returns_artifact_output(self) -> None:
        envelope = {
            "artifacts": [{"node_id": "c", "status": "completed", "output": {"result": "ok"}}],
            "output": {"status": "completed"},
        }
        target = _resolve_post_node_eval_target("c", envelope, {"c": "connector"})
        assert target == {"result": "ok"}

    def test_unknown_type_keeps_legacy_inner_output(self) -> None:
        envelope = {"output": {"summary": "done"}, "summary": "top"}
        target = _resolve_post_node_eval_target("x", envelope, {"x": "weird"})
        assert target == {"summary": "done"}

    def test_missing_type_map_keeps_legacy_inner_output(self) -> None:
        envelope = {"output": {"summary": "done"}}
        target = _resolve_post_node_eval_target("x", envelope, None)
        assert target == {"summary": "done"}

    def test_non_dict_contract_output_falls_back_to_envelope(self) -> None:
        envelope = _sandbox_agent_envelope(None)
        target = _resolve_post_node_eval_target("reviewer", envelope, {"reviewer": "sandbox_agent"})
        assert target is envelope


class TestResolveNodeContractOutput:
    def test_sandbox_agent_returns_output_json(self) -> None:
        envelope = _sandbox_agent_envelope(
            {"status": "completed", "pr_url": "https://x/pull/1", "changed_files": ["a"]}
        )
        found, contract_output = resolve_node_contract_output(envelope, "sandbox_agent")
        assert found is True
        assert contract_output == {"status": "completed", "pr_url": "https://x/pull/1", "changed_files": ["a"]}

    def test_unknown_type_reports_not_found(self) -> None:
        found, contract_output = resolve_node_contract_output({"output": {"summary": "done"}}, "weird")
        assert found is False
        assert contract_output is None

    def test_missing_output_json_reports_not_found(self) -> None:
        envelope = _sandbox_agent_envelope(None)
        found, contract_output = resolve_node_contract_output(envelope, "sandbox_agent")
        assert found is False
        assert contract_output is None

    def test_empty_type_defaults_to_agent(self) -> None:
        envelope = {"output": {"summary": "done"}}
        found, contract_output = resolve_node_contract_output(envelope, None)
        assert found is True
        assert contract_output == {"summary": "done"}


class TestPostNodeEvalsValidateContractOutput:
    async def test_sandbox_envelope_with_valid_artifact_output_passes(self) -> None:
        """A sandbox_agent envelope whose artifact output carries pr_url +
        changed_files PASSES the pr_url-requiring schema eval."""
        envelope = _sandbox_agent_envelope(
            {"status": "completed", "pr_url": "https://github.com/farnalabs/modulo/pull/123", "changed_files": ["a.py"]}
        )
        executor = _executor()
        # failure_behaviour='block' — a failure would raise EvalBlockedError,
        # so reaching the end of the call without an exception proves a pass.
        await executor._run_post_node_evals(
            "reviewer",
            envelope,
            {"reviewer": [_key_eval_def()]},
            uuid.uuid4(),
            None,
            node_type_map={"reviewer": "sandbox_agent"},
        )

    def test_outer_envelope_output_is_not_what_the_engine_validates(self) -> None:
        """The same schema applied to the outer envelope ``output`` MUST fail —
        proving the engine validates the artifact contract output, not the
        telemetry envelope."""
        envelope = _sandbox_agent_envelope(
            {"status": "completed", "pr_url": "https://github.com/farnalabs/modulo/pull/123", "changed_files": ["a.py"]}
        )
        eval_def = _key_eval_def()
        # The contract output (what the engine now validates) passes...
        contract_output, _ = split_node_output(envelope, "sandbox_agent", None)
        assert EvalEngine().evaluate(contract_output, eval_def, run_id=uuid.uuid4()).passed is True
        # ...while the outer envelope ``output`` (the OLD target) fails.
        with pytest.raises(EvalBlockedError):
            EvalEngine().evaluate(envelope["output"], eval_def, run_id=uuid.uuid4())

    async def test_without_type_map_legacy_target_fails(self) -> None:
        """Without a node_type_map the legacy ``envelope["output"]`` read
        remains — validating telemetry fails the pr_url schema (documenting
        why the production path always supplies the map)."""
        envelope = _sandbox_agent_envelope(
            {"status": "completed", "pr_url": "https://github.com/farnalabs/modulo/pull/123", "changed_files": ["a.py"]}
        )
        executor = _executor()
        with pytest.raises(EvalBlockedError, match="pr-review"):
            await executor._run_post_node_evals(
                "reviewer",
                envelope,
                {"reviewer": [_key_eval_def()]},
                uuid.uuid4(),
                None,
            )

    def test_agent_contract_output_still_validated(self) -> None:
        """Non-sandbox node types keep their contract output — an agent node's
        ``envelope["output"]`` is validated and passes when compliant."""
        envelope = {"output": {"status": "completed", "pr_url": "https://x/pull/1", "changed_files": ["a"]}}
        target = _resolve_post_node_eval_target("a", envelope, {"a": "agent"})
        assert EvalEngine().evaluate(target, _key_eval_def(), run_id=uuid.uuid4()).passed is True

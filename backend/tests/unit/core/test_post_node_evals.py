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

FAR-315: also pins that an ``llm_judge`` eval in the standalone post-node path
receives an ``llm_judge_callable`` resolved from
``eval_def.config["model_backend_id"]`` via the ModelBackendHub — before the
fix the post-node loop passed no callable, so every llm_judge eval returned
score=0.0 with detail "LLM judge callable not provided".
"""

import json
import logging
import uuid
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

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
        # failure_behaviour='block' — a failure raises EvalBlockedError (the
        # contract output carries pr_url + changed_files, so it must pass the
        # schema). Any EvalBlockedError here means the pass is broken.
        try:
            await executor._run_post_node_evals(
                "reviewer",
                envelope,
                {"reviewer": [_key_eval_def()]},
                uuid.uuid4(),
                None,
                node_type_map={"reviewer": "sandbox_agent"},
            )
        except EvalBlockedError as exc:
            pytest.fail(f"expected the pr_url-requiring eval to pass against the contract output, got: {exc}")

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


class _FakeBackend:
    """Fake model backend whose invoke() returns the configured JSON content."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content=self._content)


class _FakeHub:
    """Fake ModelBackendHub returning a single fake backend."""

    def __init__(self, content: str) -> None:
        self._backend = _FakeBackend(content)

    async def get(self, backend_id: uuid.UUID, **kwargs: Any) -> _FakeBackend:
        return self._backend


def _llm_judge_eval_def(
    *,
    with_backend_id: bool = True,
    failure_behaviour: str = "block",
    backend_id: str | None = None,
) -> EvalDefinition:
    config: dict[str, Any] = {"field": "content"}
    if backend_id is not None:
        config["model_backend_id"] = backend_id
    elif with_backend_id:
        config["model_backend_id"] = str(uuid.uuid4())
    return EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        pipeline_id=uuid4(),
        node_id="reviewer",
        name="llm-judge",
        eval_type=EvalType.LLM_JUDGE,
        config=config,
        failure_behaviour=failure_behaviour,
    )


def _agent_envelope(content: str) -> dict[str, Any]:
    return {"output": {"content": content}}


class TestPostNodeLlmJudgeCallable:
    """FAR-315: the standalone post-node eval path must resolve the LLM judge.

    Before the fix ``_run_post_node_evals`` called ``run_evals_persist_before_decide``
    WITHOUT ``resolve_llm_judge``, so every llm_judge eval scored 0.0 with
    detail "LLM judge callable not provided" — and a ``block`` gate failed
    every run regardless of the judge's verdict. These tests pin the wiring:
    the judge callable is resolved from ``eval_def.config["model_backend_id"]``
    via the ModelBackendHub (same resolver the HITL-gate path uses, FAR-307)
    and the engine scores against the judge's response.
    """

    async def test_llm_judge_scores_via_hub(self, caplog: pytest.LogCaptureFixture) -> None:
        """A judge returning 0.9 must produce score 0.9 (not the fail-closed 0.0).

        ``failure_behaviour="block"`` + a passing high score means: without the
        fix this raises ``EvalBlockedError`` (score 0.0 fails the block), with
        the fix it completes and the structured log carries the judge's score.
        """
        eval_def = _llm_judge_eval_def()
        hub = _FakeHub(json.dumps({"passed": True, "score": 0.9, "detail": "judged"}))
        executor = _executor()

        with (
            caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.executor"),
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
        ):
            await executor._run_post_node_evals(
                "reviewer",
                _agent_envelope("some agent output"),
                {"reviewer": [eval_def]},
                uuid.uuid4(),
                None,
                node_type_map={"reviewer": "agent"},
            )

        judge_records = [r for r in caplog.records if r.getMessage() == "post_node_eval.result"]
        assert len(judge_records) == 1
        assert judge_records[0].score == pytest.approx(0.9)
        assert judge_records[0].passed is True
        assert judge_records[0].detail != "LLM judge callable not provided"

    async def test_llm_judge_without_backend_id_still_fails_closed(self) -> None:
        """No model_backend_id ⇒ no judge callable ⇒ score 0.0 fails the block.

        Documents the intended fail-closed fallback is preserved: the resolver
        only builds a callable when the eval config names a backend.
        """
        eval_def = _llm_judge_eval_def(with_backend_id=False)
        executor = _executor()
        with pytest.raises(EvalBlockedError, match="llm-judge"):
            await executor._run_post_node_evals(
                "reviewer",
                _agent_envelope("some agent output"),
                {"reviewer": [eval_def]},
                uuid.uuid4(),
                None,
                node_type_map={"reviewer": "agent"},
            )

    async def test_malformed_backend_id_fails_closed_without_aborting_siblings(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A malformed (non-UUID) model_backend_id fails THAT eval closed with a
        clear configuration message — sibling evals still run.

        ``eval_def.config["model_backend_id"]`` is unvalidated free-form JSON;
        before the defensive parse in ``_build_llm_judge_callable``, a typo or
        stale manual edit (e.g. ``"not-a-uuid"``) raised a raw ``ValueError``
        inside ``resolve_llm_judge()`` in ``run_evals_persist_before_decide``'s
        per-eval loop — aborting every sibling eval and terminalising the run
        via the generic error path.

        Discriminating: without the fix this test fails with ``ValueError``
        instead of completing; with the fix the malformed eval fails closed
        with a clear detail (not a silent 0.0, not the MISSING-id message)
        and the sibling regex eval still scores.
        """
        judge_def = _llm_judge_eval_def(failure_behaviour="warn", backend_id="not-a-uuid")
        sibling = EvalDefinition(
            id=uuid4(),
            org_id=uuid4(),
            pipeline_id=uuid4(),
            node_id="reviewer",
            name="sibling-regex",
            eval_type=EvalType.REGEX,
            config={"field": "content", "pattern": "agent"},
            failure_behaviour="warn",
        )
        hub = _FakeHub(json.dumps({"passed": True, "score": 0.9, "detail": "judged"}))
        executor = _executor()
        with (
            caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.executor"),
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
        ):
            await executor._run_post_node_evals(
                "reviewer",
                _agent_envelope("some agent output"),
                {"reviewer": [judge_def, sibling]},
                uuid.uuid4(),
                None,
                node_type_map={"reviewer": "agent"},
            )

        records = {r.eval_name: r for r in caplog.records if r.getMessage() == "post_node_eval.result"}
        judge_record = records["llm-judge"]
        assert judge_record.passed is False
        assert judge_record.score == 0.0
        # A clear, fail-closed configuration error — not the MISSING-id
        # message and not a silent 0.0 with no explanation.
        assert "malformed" in judge_record.detail
        assert "not-a-uuid" in judge_record.detail
        assert judge_record.detail != "LLM judge callable not provided"
        # The sibling eval still ran (pre-fix the ValueError aborted the loop).
        assert records["sibling-regex"].passed is True

    async def test_regex_post_node_eval_still_works(self, caplog: pytest.LogCaptureFixture) -> None:
        """Non-judge eval types keep working through the same loop (FAR-315 scope)."""
        eval_def = EvalDefinition(
            id=uuid4(),
            org_id=uuid4(),
            pipeline_id=uuid4(),
            node_id="reviewer",
            name="regex-check",
            eval_type=EvalType.REGEX,
            config={"field": "content", "pattern": "agent"},
            failure_behaviour="block",
        )
        executor = _executor()
        with caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.executor"):
            # "some agent output" matches /agent/ — no EvalBlockedError.
            await executor._run_post_node_evals(
                "reviewer",
                _agent_envelope("some agent output"),
                {"reviewer": [eval_def]},
                uuid.uuid4(),
                None,
                node_type_map={"reviewer": "agent"},
            )
        regex_records = [r for r in caplog.records if r.getMessage() == "post_node_eval.result"]
        assert len(regex_records) == 1
        assert regex_records[0].passed is True

"""Typed model-judge adapter for internal System Maps discovery."""

from __future__ import annotations

import uuid
from typing import Any

from modulo.core.eval_engine import EvalDefinition
from modulo.core.eval_engine.typed_judge import JudgeConfidenceKind, JudgeIdentity
from modulo.core.eval_engine.typed_judge.config import timeout_seconds, typed_config_questions
from modulo.core.eval_engine.typed_judge.transport import JudgeBackendHandle, judge_questions


class _DiscoveryJudge:
    def __init__(self, hub: Any, backend_id: uuid.UUID) -> None:
        self._hub = hub
        self._backend_id = backend_id

    async def ajudge(self, output: dict[str, Any], eval_def: EvalDefinition) -> dict[str, Any]:
        backend = await self._hub.get(self._backend_id)

        async def invoke(messages: list[Any], output_schema: dict[str, Any] | None) -> Any:
            if output_schema is None:
                return await backend.invoke(messages)
            return await backend.invoke(messages, output_schema=output_schema)

        handle = JudgeBackendHandle(
            key=str(self._backend_id),
            invoke=invoke,
            supports_native_structured_output=bool(getattr(backend, "supports_native_structured_output", False)),
        )
        per_question, per_definition = timeout_seconds(eval_def.config)
        verdict = await judge_questions(
            handle,
            typed_config_questions(eval_def.config),
            str(eval_def.config.get("_judge_content") or output),
            pass_rule=eval_def.config.get("pass_rule") if isinstance(eval_def.config.get("pass_rule"), dict) else None,
            identity=JudgeIdentity(mechanism="model_backend", judge_backend_id=str(self._backend_id)),
            per_question_seconds=per_question,
            per_definition_seconds=per_definition,
            confidence_kind=JudgeConfidenceKind.MODEL_SELF_REPORTED,
        )
        return verdict.to_json()


def build_discovery_model_judge_callable(hub: Any, backend_id: uuid.UUID) -> _DiscoveryJudge:
    """Build the existing typed-judge transport adapter for discovery."""
    return _DiscoveryJudge(hub, backend_id)


__all__ = ["build_discovery_model_judge_callable"]

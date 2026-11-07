"""Typed model-judge adapter for internal System Maps discovery."""

from __future__ import annotations

import uuid
from typing import Any

from modulo.core.eval_engine import ModelJudgeCallable


def build_discovery_model_judge_callable(hub: Any, backend_id: uuid.UUID) -> ModelJudgeCallable:
    """Build the shared typed-judge callable for System Maps discovery.

    Discovery eval definitions are validated to use the typed ``answer_contract``
    (see ``_resolve_discovery_eval``), so this delegates to
    :func:`build_model_judge_callable` — the same adapter the pipeline and
    suite-run paths use. That helper owns the bounded ``hub.get`` resolve
    (:data:`BACKEND_INVOKE_TIMEOUT_SECONDS`, FAR-1652), the ``TimeoutError`` ->
    ``JUDGE_TIMEOUT`` and backend-failure mapping, and the ``_judge_content``
    fallback, so discovery cannot drift from the pipeline judge contract.
    """
    from modulo.core.pipeline_engine.llm_judge import build_model_judge_callable

    return build_model_judge_callable(hub, str(backend_id))


__all__ = ["build_discovery_model_judge_callable"]

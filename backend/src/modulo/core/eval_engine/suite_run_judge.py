"""Suite-run model-judge wiring (FAR-1712).

The production suite-run path (the SAQ ``execute_suite_run`` job) previously
never supplied an ``model_judge_callable``, so the runner
(:mod:`modulo.core.eval_engine.execute_suite_run`) hard-refused every
``EvalSuite`` that contained an ``model_judge`` definition. This module closes the
gap: it builds an ORG-SCOPED :class:`~modulo.core.model_backend_hub.ModelBackendHub`
from the org's ACTIVE ``ModelBackend`` rows and a single dispatch callable that
resolves each judge definition's OWN ``config["model_backend_id"]`` through that
hub.

Because the hub supports self-hosted / local providers (ollama, vllm, llamacpp,
localai, lm_studio, tgi, ...), a locally-configured backend is used with no
outbound dependency - the air-gap path.

This module deliberately does NOT reimplement the judge itself. It owns only
hub construction + per-definition dispatch; the sync->async invoke bridge, the
timeout bounds and the malformed-id fail-closed behaviour all stay in
:mod:`modulo.core.pipeline_engine.llm_judge` (the in-pipeline judge path). That
keeps one source of truth for judge behaviour across both call sites.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine import EvalDefinition, ModelJudgeCallable
from modulo.core.eval_engine.typed_judge import TYPED_ANSWER_CONTRACT, JudgeErrorCode, JudgeExecutionError
from modulo.core.model_backend_hub import BackendNotFoundError, ModelBackendHub
from modulo.db.models.model_backend import ModelBackend

_log = logging.getLogger(__name__)

__all__ = [
    "build_org_model_backend_hub",
    "build_suite_run_model_judge_callable",
]


async def build_org_model_backend_hub(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> ModelBackendHub | None:
    """Build and initialise an org-scoped :class:`ModelBackendHub`, or ``None``.

    Loads the org's ACTIVE ``ModelBackend`` rows with an UNCONDITIONAL
    ``organisation_id == org_id`` predicate (the primary isolation control; RLS
    is never relied on alone) and returns an entered + initialised hub. Returns
    ``None`` when the org has no active backend - the caller then passes no
    judge callable, so the runner's existing loud refusal is preserved.

    The caller OWNS the returned hub and MUST tear it down
    (``await hub.__aexit__(None, None, None)``) in a ``finally``. No
    module-global hub is used or set: the SAQ path has no run context (no
    ContextVar), so a hub local to the job is the only correct scope.

    An ``initialise`` failure AFTER ``__aenter__`` succeeded must not leak the
    entered hub's transport resources, so the pair is wrapped and the hub torn
    down (cancellation-shielded, via the executor's ``_teardown_hub``) before
    the error is re-raised - mirroring
    :meth:`PipelineExecutor._init_model_backend_hub`'s init-failure path.
    """
    rows = list(
        (
            await session.execute(
                select(ModelBackend).where(
                    ModelBackend.organisation_id == org_id,
                    ModelBackend.status == "active",
                )
            )
        ).scalars()
    )
    if not rows:
        return None

    from modulo.core.secrets_backend import create_secrets_backend
    from modulo.settings import get_settings

    secrets_backend = create_secrets_backend(
        fernet_key=get_settings().fernet_key,
        session=session,
    )
    hub = ModelBackendHub()
    await hub.__aenter__()
    try:
        await hub.initialise(rows, secrets_backend=secrets_backend)
    except BaseException:
        # A failure after ``__aenter__`` succeeded (bad credentials, unknown
        # provider, cancellation mid-init) would otherwise leak the entered
        # hub. Tear it down (cancellation-shielded) before re-raising, exactly
        # as the executor does on its init-failure path.
        from modulo.core.pipeline_engine.executor import _teardown_hub

        await _teardown_hub(hub)
        raise
    return hub


def build_suite_run_model_judge_callable(hub: ModelBackendHub) -> ModelJudgeCallable:
    """Build ONE synchronous judge callable that dispatches per definition.

    The :class:`~modulo.core.eval_engine.ModelJudgeCallable` protocol is
    synchronous and receives ``(output, eval_def)`` per call, and the suite
    runner hands the SAME callable to every definition in the suite. This
    dispatcher therefore reads ``eval_def.config["model_backend_id"]`` per call
    so each judge definition uses its OWN configured backend, and caches the
    built judge per backend id so a suite with N cases x M definitions does not
    rebuild the sync->async bridge on every call.

    A MISSING or malformed ``model_backend_id``, and an id the hub does not
    know (a VALID UUID for a deleted, deactivated, or another org's backend),
    raise :class:`~modulo.core.eval_engine.typed_judge.JudgeExecutionError` with
    ``judge.configuration``. The engine turns that into
    ``outcome=error``/NULL score for THAT definition — no verdict, never a
    ``passed=False, score=0.0`` verdict — and the remaining definitions and
    cases still run. ADR 066 D6/D13.
    """
    from modulo.core.pipeline_engine.llm_judge import build_model_judge_callable

    cache: dict[str, ModelJudgeCallable] = {}

    def _judge(output: dict[str, Any], eval_def: EvalDefinition) -> dict[str, Any]:
        config = eval_def.config or {}
        backend_id_raw = config.get("model_backend_id")
        if not backend_id_raw and config.get("answer_contract") == TYPED_ANSWER_CONTRACT:
            backing = config.get("backing")
            if isinstance(backing, dict):
                backend_id_raw = backing.get("model_backend_id")
        if not backend_id_raw:
            detail = (
                "model_judge eval has no model_backend_id configured; set the eval "
                "definition's config['model_backend_id'] (or the typed "
                "config['backing']['model_backend_id']) to an active model backend id"
            )
            _log.error(
                "suite_run_judge.missing_model_backend_id",
                extra={"eval_id": str(eval_def.id), "detail": detail},
            )
            raise JudgeExecutionError(JudgeErrorCode.JUDGE_CONFIGURATION, detail)
        key = str(backend_id_raw)
        judge = cache.get(key)
        if judge is None:
            judge = build_model_judge_callable(hub, key)
            cache[key] = judge
        try:
            return judge(output, eval_def)
        except BackendNotFoundError as exc:
            # A VALID-UUID id that the hub does not hold (deleted, deactivated,
            # or another org's backend) means the judge cannot run. It is a
            # configuration error (no verdict), never a failing verdict.
            detail = (
                f"model_judge eval's model_backend_id {key!r} is not an active model "
                "backend for this organisation; set the eval definition's "
                "config['model_backend_id'] to an active model backend id"
            )
            _log.error(
                "suite_run_judge.unknown_model_backend_id",
                extra={"eval_id": str(eval_def.id), "detail": detail},
            )
            raise JudgeExecutionError(JudgeErrorCode.JUDGE_CONFIGURATION, detail) from exc

    return _judge

"""CRUD for EvalResult records."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult


def non_guardrail_eval_results_clause() -> Any:
    """The eval_results consumer contract (FAR-223 item 11 §4d).

    Guardrail results are stored in eval_results (rows whose ``eval_id`` points
    at an ``eval_type='guardrail'`` definition). EVERY consumer must either
    explicitly include or exclude them. This clause EXCLUDES guardrail rows —
    the default for normal-eval surfaces, whose pass-rate / failure counting
    break on guardrail semantics (a regex guardrail's ``passed=True`` means the
    pattern MATCHED = a violation, inverted from a normal eval).
    """
    return EvalResult.eval_id.not_in(select(EvalDefinition.id).where(EvalDefinition.eval_type == "guardrail"))


async def get_run_evals(
    session: AsyncSession,
    run_id: Any,
) -> list[EvalResult]:
    """Eval results for a run, EXCLUDING guardrail rows (consumer contract).

    The MCP ``get_run_evals`` surface shows normal eval outcomes; guardrail
    interception results are surfaced via the run-detail ``guardrail_summary``
    instead (item 11).
    """
    result = await session.execute(
        select(EvalResult)
        .where(EvalResult.run_id == run_id, non_guardrail_eval_results_clause())
        .order_by(EvalResult.evaluated_at.desc())
    )
    return list(result.scalars().all())

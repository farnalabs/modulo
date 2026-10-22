"""CRUD for EvalResult records."""

from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult


def non_guardrail_eval_results_clause() -> Any:
    """The eval_results quality-consumer contract (FAR-223 item 11 §4d; ADR 066 D18).

    Guardrail results are stored in eval_results (rows whose ``eval_id`` points
    at an ``eval_type='guardrail'`` definition). EVERY consumer must either
    explicitly include or exclude them. This clause EXCLUDES guardrail rows —
    the default for normal-eval surfaces, whose pass-rate / failure counting
    break on guardrail semantics (a regex guardrail's ``passed=True`` means the
    pattern MATCHED = a violation, inverted from a normal eval).

    The guardrail set is read from ``evals`` — the storage source since the
    FAR-1100/1101 eval-definition cutover (migration ``0254``) and the table
    ``eval_results.eval_id`` actually references through the composite FK
    ``fk_eval_results_eval_org`` (migration ``0294``). The legacy
    ``eval_definitions`` table is a frozen pre-cutover snapshot that no write
    path touches, so sourcing the exclusion from it silently no-ops for every
    post-cutover guardrail and lets inverted-semantics rows into pass rates
    (FAR-1725).

    The sub-select is deliberately Core-level (``Eval.__table__``): an ORM
    ``select(Eval.id)`` would pick up the global soft-delete loader criteria
    (``deleted_at IS NULL``) and drop soft-deleted guardrails from the
    exclusion set, re-leaking their stored results. A guardrail's ``passed``
    stays inverted after the definition is retired, so soft-deleted
    definitions must stay excluded too.

    ADR 066 D18: an ``outcome='error'`` row records that the judge produced NO
    verdict — an instrument failure, not a failed check — so it is excluded
    from every quality aggregate's numerator AND denominator (a ``passed``
    projection of ``False`` would otherwise count it as a failure). One shared
    clause keeps every consumer consistent instead of per-endpoint patches.

    ``is_distinct_from`` (SQL: ``IS DISTINCT FROM``) rather than ``!=`` is
    deliberate: ``outcome`` is nullable during the FAR-2040 expand rollout, and
    a plain inequality evaluates to NULL for legacy rows, silently dropping
    every pre-``outcome`` pass/fail result from every pass rate.
    """
    evals_table = Eval.__table__
    return and_(
        EvalResult.eval_id.not_in(select(evals_table.c.id).where(evals_table.c.eval_type == "guardrail")),
        EvalResult.outcome.is_distinct_from("error"),
    )


async def get_run_evals(
    session: AsyncSession,
    run_id: Any,
) -> list[EvalResult]:
    """Eval results for a run, EXCLUDING guardrail AND error rows (consumer contract).

    The MCP ``get_run_evals`` surface shows normal eval outcomes; guardrail
    interception results are surfaced via the run-detail ``guardrail_summary``
    instead (item 11). ``outcome='error'`` rows are the failed instrument, not
    a verdict, so they are excluded here too (ADR 066 D18).
    """
    result = await session.execute(
        select(EvalResult)
        .where(EvalResult.run_id == run_id, non_guardrail_eval_results_clause())
        .order_by(EvalResult.evaluated_at.desc())
    )
    return list(result.scalars().all())


async def get_evals_for_runs(
    session: AsyncSession,
    *,
    org_id: Any,
    run_ids: list[Any],
) -> list[EvalResult]:
    """Org-scoped eval results (excl. guardrails) across a set of runs.

    The coverage-gap read-model groups by ``run_id`` so the same eval's score
    spread can be measured across the variants in one batch. The explicit
    ``organisation_id`` predicate is the BYPASSRLS isolation control (plus
    ``set_rls_org`` as defense-in-depth), so a cross-org run can never be
    pulled into the spread. Guardrail rows and judge error rows are excluded
    per the consumer contract — guardrail ``passed`` semantics are inverted (a
    match = a violation) and an error row produced no verdict (ADR 066 D18).
    """
    if not run_ids:
        return []
    result = await session.execute(
        select(EvalResult)
        .where(
            EvalResult.run_id.in_(run_ids),
            EvalResult.organisation_id == org_id,
            non_guardrail_eval_results_clause(),
        )
        .order_by(EvalResult.evaluated_at)
    )
    return list(result.scalars().all())

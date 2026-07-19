"""CRUD for EvalResult records."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval_result import EvalResult


async def get_run_evals(
    session: AsyncSession,
    run_id: Any,
) -> list[EvalResult]:
    result = await session.execute(
        select(EvalResult).where(EvalResult.run_id == run_id).order_by(EvalResult.evaluated_at.desc())
    )
    return list(result.scalars().all())

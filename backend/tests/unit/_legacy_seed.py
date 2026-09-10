"""Shared seed helper for the legacy ``runs`` blob columns (FAR-583 B1).

The legacy blob columns left the ORM mapping at B1 but still EXIST IN THE
DATABASE until B2b, and every test that seeds them does it through the repo
module's raw Core legacy table via one copy-pasted
``update(RUNS_LEGACY_TABLE).where(id==run_id).values(**...)`` statement. This
module removes the duplication: each caller passes only the keys it wants to
write (an explicit ``None`` value writes SQL NULL — a meaningful state, the
"side absent" representation — so it is passed through, never filtered).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run_node_outputs import RUNS_LEGACY_TABLE


async def seed_legacy_blobs(session: AsyncSession, run_id: uuid.UUID, /, **values: Any) -> None:
    """Rewrite the run's legacy blob columns via the raw Core legacy table.

    Writes EXACTLY the given keyword keys (skipping the whole UPDATE when the
    mapping is empty): ``outputs_json`` / ``node_telemetry_json`` /
    ``raw_output_markers``. A ``None`` value is written as SQL NULL ("side
    absent"), never skipped.
    """
    if not values:
        return
    await session.execute(update(RUNS_LEGACY_TABLE).where(RUNS_LEGACY_TABLE.c.id == run_id).values(**values))

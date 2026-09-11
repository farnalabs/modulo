"""B2c seeding helper (FAR-583): stores blobs on the new table.

The B1-era ``seed_legacy_blobs`` helper is retired with the legacy read
fallback (B2c removes it; migration 0212 — the follow-up drop PR — drops
the columns themselves) - tests seed the ``run_node_outputs``
store through the repo writers directly.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run_node_outputs import replace_run_node_outputs, write_run_markers
from modulo.db.rls import set_rls_org


async def seed_run_blobs(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    outputs: dict[str, Any] | None,
    telemetry: dict[str, Any] | None = None,
    markers: dict[str, Any] | None = None,
    organisation_id: uuid.UUID | None = None,
) -> None:
    """Seed the run_node_outputs store for *run_id* through the repo writers.

    This is the new-table twin of the retired legacy-column seeder.
    """
    org = organisation_id
    async with session.begin_nested():
        await set_rls_org(session, org)
        if outputs is not None or telemetry is not None:
            await replace_run_node_outputs(
                session,
                run_id=run_id,
                organisation_id=org,
                outputs=outputs,
                telemetry=telemetry,
            )
        if markers:
            await write_run_markers(session, run_id=run_id, organisation_id=org, markers=markers)
    await set_rls_org(session, None)

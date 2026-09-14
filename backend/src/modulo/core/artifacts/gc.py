"""Retention-driven garbage collection for the artifact store (FAR-811).

Runs from ``saq_worker.retention_cleanup`` under the system role
(``modulo_system``, BYPASSRLS) so the run-existence lookups are not RLS-filtered
per organisation.  It scans the on-disk artifact store for run directories
whose run no longer exists in ``runs`` and deletes their files, so retained
FAR-811 full-transcript artifacts (and stale FAR-582 side-cars) are removed by
the same retention policy that purges the run rows — never left behind to
accumulate disk forever.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

# ``runs.id`` membership is checked in chunks so a huge store never sends one
# unbounded ``IN (...)`` list.
_GC_BATCH_SIZE = 500


async def delete_orphaned_run_artifacts(
    session: AsyncSession,
    *,
    store: Any | None = None,
) -> dict[str, Any]:
    """Delete artifact-store files whose runs no longer exist in the DB.

    Enumerates every ``(org_id, run_id)`` directory in the store and checks
    ``runs.id`` membership in batches via *session* (expected to be a system /
    BYPASSRLS session so RLS does not filter org-scoped ids away).  Returns
    ``{"orphan_runs": [(org_id, run_id), ...], "files_deleted": n}``.

    Best-effort: an exhausted store / failed enumeration returns an empty
    report, and an individual delete failure is logged and skipped so one bad
    run never halts the sweep.
    """
    if store is None:
        from modulo.core.artifacts.store import get_store

        store = get_store()
    try:
        run_ids_on_disk = store.iter_run_ids()
    except Exception:
        _log.warning("artifact_gc.enumeration_failed", exc_info=True)
        return {"orphan_runs": [], "files_deleted": 0}
    if not run_ids_on_disk:
        return {"orphan_runs": [], "files_deleted": 0}

    orphan_runs: list[tuple[str, str]] = []
    files_deleted = 0
    for offset in range(0, len(run_ids_on_disk), _GC_BATCH_SIZE):
        batch = run_ids_on_disk[offset : offset + _GC_BATCH_SIZE]
        run_ids = [uuid.UUID(run_id) for _, run_id in batch]
        existing: set[str] = set()
        try:
            result = await session.execute(select(Run.id).where(Run.id.in_(run_ids)))
            existing = {str(row[0]) for row in result.all()}
        except Exception:
            _log.warning("artifact_gc.lookup_failed", exc_info=True)
            continue
        for org_id, run_id in batch:
            if run_id in existing:
                continue
            try:
                files_deleted += store.delete_run(org_id, run_id)
            except Exception:
                _log.warning("artifact_gc.delete_failed", exc_info=True)
                continue
            orphan_runs.append((org_id, run_id))
            _log.info("artifact_gc.deleted_orphan_run", extra={"run_id": run_id, "org_id": org_id})

    return {"orphan_runs": orphan_runs, "files_deleted": files_deleted}

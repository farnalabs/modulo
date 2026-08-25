"""CRUD for organisation deletion workflow.

Soft-delete flow:
  1. Admin requests deletion → org gets deleted_at + status='deleted',
     export bundle captured, audit event recorded, confirmation email token generated.
  2. Confirmation within 24h → token verified, org hard-deleted (cascades via FK).
  3. Immediate admin DELETE → skips token, hard-deletes directly.

Run retention:
  - Terminal runs older than 30 days are hard-deleted before org drop.
  - LangGraph checkpoint rows of TERMINAL runs are batched 500 at a time in an
    hourly job (see ``batch_delete_langgraph_checkpoints``).
"""

import asyncio
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import batch_delete_old_terminal_runs
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import TERMINAL_STATUSES, Run

_log = logging.getLogger(__name__)

# Error raised when the target org no longer exists mid-deletion flow.
_ERR_ORG_NOT_FOUND = "Organisation not found"

DELETION_TOKEN_BYTES = 48
CONFIRMATION_WINDOW_HOURS = 24
RUN_RETENTION_DAYS = 30
# LangGraph checkpoint rows are the bulk of the DB volume (~7.9GB observed) but
# carry no feature beyond the terminal run's audit trail. Post-terminal nothing
# reads them, so they are aged out on a MUCH shorter window than the ``runs``
# rows themselves (FAR-432). A terminal run's checkpoints become reclaimable a
# few days after it finishes rather than at the 30-day run window.
CHECKPOINT_RETENTION_DAYS = 3
CHECKPOINT_BATCH_SIZE = 500
# Best-effort E2B sandbox kill timeout (B7) — teardown-grade, per the SDK
# wait_for guidance in backend AGENTS.md.
_SANDBOX_KILL_TIMEOUT = 30


# ── Helpers ──────────────────────────────────────────────────────────


def _generate_deletion_token() -> str:
    return secrets.token_urlsafe(DELETION_TOKEN_BYTES)


async def _count_non_terminal_runs(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Count runs for the org that are NOT in a terminal state.

    Terminal statuses are the single source of truth ``TERMINAL_STATUSES``
    (complete/failed/cancelled/eval_failed). A non-terminal run is still
    live — the org cannot be hard-deleted out from under it.
    """
    result = await session.execute(
        select(func.count()).select_from(Run).where(Run.organisation_id == org_id, Run.status.not_in(TERMINAL_STATUSES))
    )
    return int(result.scalar() or 0)


async def _abort_org_live_sandboxes(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Best-effort kill of the org's live E2B sandboxes before hard-delete.

    Reads ``runs.sandbox_id`` via raw SQL (the column ships in a parallel
    migration; the runs model is owned by another worker). Kills each
    sandbox by id through the E2B SDK. NEVER blocks the delete: any failure
    (import, missing column pre-migration, SDK error, network) is logged and
    swallowed. Returns the number of kill requests sent (not necessarily
    succeeded).
    """
    try:
        from e2b import AsyncSandbox

        # Guard: runs.sandbox_id ships in a parallel migration owned by the
        # runs-model worker. Skip the whole abort when the column is absent —
        # an UndefinedColumnError would abort the surrounding transaction and
        # break the delete that follows.
        col_check = await session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'runs' AND column_name = 'sandbox_id' "
                "LIMIT 1"
            )
        )
        if col_check.first() is None:
            _log.info("org_deletion: runs.sandbox_id column not present; skipping sandbox abort for org %s", org_id)
            return 0

        result = await session.execute(
            text(
                "SELECT DISTINCT sandbox_id FROM runs "
                "WHERE organisation_id = :org_id AND sandbox_id IS NOT NULL "
                "AND status NOT IN :terminal"
            ).bindparams(bindparam("terminal", expanding=True)),
            {"org_id": org_id, "terminal": tuple(TERMINAL_STATUSES)},
        )
        sandbox_ids = [row[0] for row in result.all()]
    except SQLAlchemyError:
        # Column not present yet (parallel migration not applied) — best-effort.
        _log.exception("org_deletion: could not read runs.sandbox_id for org %s", org_id)
        return 0
    except Exception:
        _log.exception("org_deletion: E2B SDK unavailable; skipping sandbox abort for org %s", org_id)
        return 0

    killed = 0
    for sandbox_id in sandbox_ids:
        try:
            await asyncio.wait_for(AsyncSandbox.kill(sandbox_id), timeout=_SANDBOX_KILL_TIMEOUT)
            killed += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("org_deletion: failed to kill E2B sandbox %s for org %s", sandbox_id, org_id)
    return killed


async def _collect_org_export(session: AsyncSession, org: Organisation) -> dict[str, Any]:
    """Bundle all org-owned data into a JSON-serialisable dict."""
    org_id = org.id

    stmt = select(OrgMembership).where(OrgMembership.organisation_id == org_id)
    memberships = (await session.execute(stmt)).scalars().all()
    pipelines = (await session.execute(select(Pipeline).where(Pipeline.organisation_id == org_id))).scalars().all()
    runs = (await session.execute(select(Run).where(Run.organisation_id == org_id).limit(5000))).scalars().all()
    audit = (
        (await session.execute(select(AuditEvent).where(AuditEvent.organisation_id == org_id).limit(10000)))
        .scalars()
        .all()
    )
    library = (
        (await session.execute(select(LibraryPrimitive).where(LibraryPrimitive.organisation_id == org_id)))
        .scalars()
        .all()
    )
    connectors = (
        (await session.execute(select(ConnectorInstance).where(ConnectorInstance.organisation_id == org_id)))
        .scalars()
        .all()
    )
    backends = (
        (await session.execute(select(ModelBackend).where(ModelBackend.organisation_id == org_id))).scalars().all()
    )

    def _serialise(records: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for r in records:
            row: dict[str, Any] = {}
            for c in r.__table__.columns:
                val = getattr(r, c.name)
                if isinstance(val, uuid.UUID):
                    val = str(val)
                elif isinstance(val, datetime):
                    val = val.isoformat()
                elif isinstance(val, Decimal):
                    val = str(val)
                row[c.name] = val
            result.append(row)
        return result

    return {
        "organisation": _serialise([org]),
        "memberships": _serialise(memberships),
        "pipelines": _serialise(pipelines),
        "runs": _serialise(runs),
        "audit_events": _serialise(audit),
        "library_primitives": _serialise(library),
        "connector_instances": _serialise(connectors),
        "model_backends": _serialise(backends),
        "exported_at": datetime.now(UTC).isoformat(),
    }


# ── Public API ───────────────────────────────────────────────────────


async def request_org_deletion(
    session: AsyncSession,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Initiate the soft-delete workflow for an organisation.

    1. Validates org is active.
    2. Generates confirmation token (24 h TTL).
    3. Captures export bundle.
    4. Sets org deleted_at + status='deleted'.
    5. Soft-marks child rows.
    6. Records ``org_deletion_requested`` audit event.

    Returns a dict with ``token``, ``token_expires_at``, and ``export`` keys.
    """
    result = await session.execute(select(Organisation).where(Organisation.id == org_id).with_for_update())
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError(_ERR_ORG_NOT_FOUND)
    if org.status == "deleted":
        raise ValueError("Organisation is already deleted")

    token = _generate_deletion_token()
    expires_at = datetime.now(UTC) + timedelta(hours=CONFIRMATION_WINDOW_HOURS)
    export = await _collect_org_export(session, org)

    # Soft-delete org
    org.status = "deleted"
    org.deleted_at = datetime.now(UTC)
    org.deletion_token = token
    org.deletion_token_expires_at = expires_at
    org.export_bundle_json = export

    await session.flush()

    return {
        "token": token,
        "token_expires_at": expires_at.isoformat(),
        "export": export,
    }


async def confirm_org_deletion(
    session: AsyncSession,
    org_id: uuid.UUID,
    token: str,
    *,
    immediate: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Confirm and execute org deletion.

    When *immediate* is True (admin DELETE endpoint), the token check is
    skipped. Otherwise the token must match and not be expired.

    Guard (B7): hard-delete is REFUSED while ANY non-terminal run exists
    (status not in ``TERMINAL_STATUSES``) — the org cannot be deleted out from
    under live runs. Passing *force*=True proceeds anyway; it is destructive
    and is only wired to admin break-glass endpoints.

    Before hard-deleting, the org's live E2B sandboxes are aborted best-effort
    (never blocks on E2B), then terminal runs older than 30 days are
    batch-deleted. The remaining cascade is handled by Postgres FK constraints.
    """
    result = await session.execute(select(Organisation).where(Organisation.id == org_id).with_for_update())
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError(_ERR_ORG_NOT_FOUND)

    if not immediate:
        if org.deletion_token is None or org.deletion_token != token:
            raise ValueError("Invalid deletion token")
        expires_at = org.deletion_token_expires_at
        if expires_at is None or datetime.now(UTC) > expires_at:
            raise ValueError("Deletion token has expired")

    # B7 guard — refuse while live runs exist unless force proceeds.
    non_terminal = await _count_non_terminal_runs(session, org_id)
    if non_terminal and not force:
        raise ValueError(
            f"Cannot delete organisation: {non_terminal} run(s) still in progress. "
            "Wait for them to finish, or force the deletion (destructive)."
        )

    # Abort the org's live sandboxes before hard-delete (best-effort).
    await _abort_org_live_sandboxes(session, org_id)

    # Hard-delete terminal runs past retention window
    deleted_runs = await batch_delete_old_terminal_runs(session, max_age_days=RUN_RETENTION_DAYS, batch_size=500)

    # Hard-delete the organisation — FK cascade removes all remaining scoped rows
    await session.delete(org)
    await session.flush()

    return {
        "deleted_organisation_id": str(org_id),
        "hard_deleted_runs": deleted_runs,
    }


async def cancel_org_deletion(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Cancel a pending org deletion — restores status to active.

    The org must be in 'deleted' status with a valid deletion_token set.
    Clears the soft-delete fields and restores the organisation to active state.
    """
    result = await session.execute(select(Organisation).where(Organisation.id == org_id).with_for_update())
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError(_ERR_ORG_NOT_FOUND)
    if org.status != "deleted" or org.deletion_token is None:
        raise ValueError("No pending deletion found")

    org.status = "active"
    org.deleted_at = None
    org.deletion_token = None
    org.deletion_token_expires_at = None

    await session.flush()

    return {"status": "active"}


async def export_org_data(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Return the export bundle for an org (captures live data if none exists)."""
    result = await session.execute(select(Organisation).where(Organisation.id == org_id).with_for_update())
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError(_ERR_ORG_NOT_FOUND)

    if org.export_bundle_json is not None:
        return org.export_bundle_json

    return await _collect_org_export(session, org)


async def _checkpoint_created_at_present(session: AsyncSession) -> bool:
    """True iff the saver checkpoint tables expose a ``created_at`` column.

    ``ModuloPostgresSaver._MIGRATION_SQL`` adds ``created_at`` (idempotent) to
    all three checkpoint tables at app boot. Several deployed schemas never
    applied that migration, so the column is absent — the retention sweep then
    keys age off ``runs.*`` instead of referencing a column that does not exist
    (FAR-432). Note that inspecting ``checkpoints`` is sufficient: the saver
    adds the column to every checkpoint table in the same migration set.
    """
    result = await session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'checkpoints' AND column_name = 'created_at' LIMIT 1"
        )
    )
    return result.first() is not None


async def batch_delete_langgraph_checkpoints(
    session: AsyncSession,
    *,
    batch_size: int = CHECKPOINT_BATCH_SIZE,
    max_age_days: int = CHECKPOINT_RETENTION_DAYS,
) -> int:
    """Hourly retention: purge old LangGraph checkpoint rows of TERMINAL runs.

    Ages out checkpoint rows for threads whose owning ``runs`` row is TERMINAL
    and older than ``max_age_days`` (default ``CHECKPOINT_RETENTION_DAYS`` = 3,
    deliberately shorter than the 30-day ``runs`` window — checkpoints are the
    bulk volume and are unread post-terminal). A live run
    (``pending``/``running``/``awaiting_human``/``claimed``) is never purged,
    because its ``runs`` row status is not in ``TERMINAL_STATUSES``. This is
    the load-bearing guard: an ``awaiting_human`` run paused at a HITL gate
    keeps its interrupt checkpoint, so ``resume_run`` on later approval resumes
    the graph instead of re-running side-effectful nodes from scratch.

    AGE IS MEASURED FROM ``runs`` (FAR-432). The historical implementation
    referenced ``created_at`` on the checkpoint tables, which is missing in
    some deployed schemas — the query raised and every call silently returned
    ``checkpoints_deleted=0`` while ~7.9GB accumulated. The age predicate is
    now ``COALESCE(runs.completed_at, runs.created_at) < cutoff`` so the sweep
    works regardless of whether the checkpoint ``created_at`` column exists.

    Schema-aware behaviour:
      * If the checkpoint tables DO have ``created_at`` (the test DB and any
        boot that ran the saver migrations), the predicate is
        ``(run-age < cutoff OR checkpoint.created_at < cutoff)`` with the
        historical orphan branch (``r.id IS NULL``) retained. This preserves
        the granular within-thread semantics the retention tests assert.
      * If the column is ABSENT (the deployed-schema bug), the predicate is
        ``run-age < cutoff AND r.status = ANY(terminal)`` only — no reference
        to the missing column, so the sweep actually deletes instead of
        raising. Orphaned threads (run row already purged) are handled by the
        FAR-427 ``run_retention.purge_terminal_runs`` cascade; they cannot be
        age-filtered without the checkpoint column.

    Operates directly on the unqualified table names the
    ``ModuloPostgresSaver`` migrations create (``checkpoints``,
    ``checkpoint_blobs``, ``checkpoint_writes``) — they land in the
    connection's default search_path schema, not a ``langgraph`` schema.
    Blob rows are not FK-cascaded from checkpoints (the saver migrations
    define no foreign keys), so all three tables are purged.

    ``checkpoint_writes`` rows are keyed by their owning checkpoint
    (``checkpoint_id``) and written together with it, so the thread + age +
    terminal-owner predicate above only ever targets writes whose checkpoint is
    being purged. ``checkpoint_blobs`` are keyed by
    (``thread_id``, ``checkpoint_ns``, ``channel``, ``version``) and shared
    across checkpoints in a thread; their reference lives inside the
    Fernet-encrypted ``checkpoint`` JSONB (``channel_versions``), so it cannot
    be probed in SQL. A blob is therefore only deleted when NO checkpoint
    remains in its thread — checked AFTER the checkpoints pass of the same
    batch, so an old idle-channel blob still referenced by a fresh checkpoint
    is retained until that checkpoint itself ages out.
    """
    from modulo.db.models.run import TERMINAL_STATUSES

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    terminal_statuses = list(TERMINAL_STATUSES)
    deleted_total = 0

    # ``table_col`` is the per-table ``created_at`` reference used ONLY when the
    # column is present. When absent (deployed-schema bug), the SQL must not
    # mention it at all — hence two predicate templates.
    has_created_at = await _checkpoint_created_at_present(session)

    if has_created_at:
        # Precise path: purge by checkpoint-age OR run-age, keeping the orphan
        # branch. ``r`` is LEFT JOINed (orphans have no ``runs`` row).
        _table_sql = {
            "checkpoint_writes": (
                "DELETE FROM checkpoint_writes w "
                "WHERE ctid IN ("
                "  SELECT w.ctid FROM checkpoint_writes w "
                "  LEFT JOIN runs r ON r.langgraph_thread_id = w.thread_id "
                "  WHERE (COALESCE(r.completed_at, r.created_at) < :cutoff "
                "      OR w.created_at < :cutoff) "
                "    AND (r.id IS NULL OR r.status = ANY(:terminal_statuses)) "
                "  LIMIT :limit"
                ")"
            ),
            "checkpoints": (
                "DELETE FROM checkpoints c "
                "WHERE ctid IN ("
                "  SELECT c.ctid FROM checkpoints c "
                "  LEFT JOIN runs r ON r.langgraph_thread_id = c.thread_id "
                "  WHERE (COALESCE(r.completed_at, r.created_at) < :cutoff "
                "      OR c.created_at < :cutoff) "
                "    AND (r.id IS NULL OR r.status = ANY(:terminal_statuses)) "
                "  LIMIT :limit"
                ")"
            ),
            "checkpoint_blobs": (
                "DELETE FROM checkpoint_blobs b "
                "WHERE ctid IN ("
                "  SELECT b.ctid FROM checkpoint_blobs b "
                "  LEFT JOIN runs r ON r.langgraph_thread_id = b.thread_id "
                "  WHERE (COALESCE(r.completed_at, r.created_at) < :cutoff "
                "      OR b.created_at < :cutoff) "
                "    AND (r.id IS NULL OR r.status = ANY(:terminal_statuses)) "
                "    AND NOT EXISTS ("
                "      SELECT 1 FROM checkpoints c "
                "      WHERE c.thread_id = b.thread_id "
                "        AND c.checkpoint_ns = b.checkpoint_ns"
                "    ) "
                "  LIMIT :limit"
                ")"
            ),
        }
    else:
        # Deployed-schema path (no checkpoint.created_at): key age on runs.*
        # only. INNER JOIN guarantees a terminal ``runs`` row; the terminal
        # status filter is what protects a live/HITL run from purge.
        _table_sql = {
            "checkpoint_writes": (
                "DELETE FROM checkpoint_writes w "
                "WHERE ctid IN ("
                "  SELECT w.ctid FROM checkpoint_writes w "
                "  INNER JOIN runs r ON r.langgraph_thread_id = w.thread_id "
                "  WHERE COALESCE(r.completed_at, r.created_at) < :cutoff "
                "    AND r.status = ANY(:terminal_statuses) "
                "  LIMIT :limit"
                ")"
            ),
            "checkpoints": (
                "DELETE FROM checkpoints c "
                "WHERE ctid IN ("
                "  SELECT c.ctid FROM checkpoints c "
                "  INNER JOIN runs r ON r.langgraph_thread_id = c.thread_id "
                "  WHERE COALESCE(r.completed_at, r.created_at) < :cutoff "
                "    AND r.status = ANY(:terminal_statuses) "
                "  LIMIT :limit"
                ")"
            ),
            "checkpoint_blobs": (
                "DELETE FROM checkpoint_blobs b "
                "WHERE ctid IN ("
                "  SELECT b.ctid FROM checkpoint_blobs b "
                "  INNER JOIN runs r ON r.langgraph_thread_id = b.thread_id "
                "  WHERE COALESCE(r.completed_at, r.created_at) < :cutoff "
                "    AND r.status = ANY(:terminal_statuses) "
                "    AND NOT EXISTS ("
                "      SELECT 1 FROM checkpoints c "
                "      WHERE c.thread_id = b.thread_id "
                "        AND c.checkpoint_ns = b.checkpoint_ns"
                "    ) "
                "  LIMIT :limit"
                ")"
            ),
        }
    for stmt_text in _table_sql.values():
        while True:
            stmt = text(stmt_text)
            result = await session.execute(
                stmt,
                {"cutoff": cutoff, "limit": batch_size, "terminal_statuses": terminal_statuses},
            )
            count = result.rowcount if hasattr(result, "rowcount") else 0
            deleted_total += count
            if count < batch_size:
                break

    return deleted_total

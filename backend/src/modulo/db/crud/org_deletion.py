"""CRUD for organisation deletion workflow.

Soft-delete flow:
  1. Admin requests deletion → org gets deleted_at + status='deleted',
     export bundle captured, audit event recorded, confirmation email token generated.
  2. Confirmation within 24h → token verified, org hard-deleted (cascades via FK).
  3. Immediate admin DELETE → skips token, hard-deletes directly.

Run retention:
  - Terminal runs older than 30 days are hard-deleted before org drop.
  - LangGraph checkpoint rows of TERMINAL runs are purged by an hourly job in
    (organisation, thread) batches, committed per batch and bounded by a
    per-tick row budget (see ``batch_delete_langgraph_checkpoints``).
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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from modulo.db.crud.run import batch_delete_old_terminal_runs
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.collection_install import CollectionInstall
from modulo.db.models.composite_template import CompositeTemplate
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.primitive_abuse_report import PrimitiveAbuseReport
from modulo.db.models.primitive_rating import PrimitiveRating
from modulo.db.models.publisher import Publisher
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.rls import set_rls_org
from modulo.db.session import get_shared_engine
from modulo.db.soft_delete import include_soft_deleted

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
#: Candidate *threads* fetched and deleted per committed batch (FAR-1971a).
CHECKPOINT_BATCH_SIZE = 500
#: Per-tick ceiling on checkpoint rows deleted (FAR-1971c). The sweep runs
#: inside the SAQ retention job whose timeout is 300s; an unbounded tick runs
#: until it is CANCELLED mid-``DELETE`` and the cancellation discards the
#: transaction. Production observed a ~2.8GB WAL burst (177 un-archived 16MB
#: segments) from exactly that. A bounded budget makes every tick finish well
#: inside the timeout; the next tick resumes from the remaining rows.
CHECKPOINT_MAX_ROWS_PER_TICK = 10_000
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
    """Bundle the org-owned data this export covers into a JSON-serialisable dict.

    This archive is a curated subset, not an exhaustive dump: it covers the
    FAR-1758 library cluster plus the pre-existing org-owned tables selected
    below, and a table missing from it is data the org never gets back — so
    library-cluster tables must be added here when introduced. Soft-deletable
    tables are read through :func:`include_soft_deleted`: a soft-deleted row
    is still org-owned data awaiting purge, and dropping it silently would
    make the archive incomplete exactly when an org retires entities before
    deleting itself (FAR-1758).
    """
    org_id = org.id

    stmt = select(OrgMembership).where(OrgMembership.organisation_id == org_id)
    memberships = (await session.execute(stmt)).scalars().all()
    pipelines = (
        (await session.execute(include_soft_deleted(select(Pipeline).where(Pipeline.organisation_id == org_id))))
        .scalars()
        .all()
    )
    runs = (await session.execute(select(Run).where(Run.organisation_id == org_id).limit(5000))).scalars().all()
    audit = (
        (await session.execute(select(AuditEvent).where(AuditEvent.organisation_id == org_id).limit(10000)))
        .scalars()
        .all()
    )
    library = (
        (
            await session.execute(
                include_soft_deleted(select(LibraryPrimitive).where(LibraryPrimitive.organisation_id == org_id))
            )
        )
        .scalars()
        .all()
    )
    connectors = (
        (
            await session.execute(
                include_soft_deleted(select(ConnectorInstance).where(ConnectorInstance.organisation_id == org_id))
            )
        )
        .scalars()
        .all()
    )
    backends = (
        (await session.execute(select(ModelBackend).where(ModelBackend.organisation_id == org_id))).scalars().all()
    )
    # Library cluster tables the archive previously omitted (FAR-1758 item 6).
    composites = (
        (
            await session.execute(
                include_soft_deleted(select(CompositeTemplate).where(CompositeTemplate.organisation_id == org_id))
            )
        )
        .scalars()
        .all()
    )
    ratings = (
        (await session.execute(select(PrimitiveRating).where(PrimitiveRating.organisation_id == org_id)))
        .scalars()
        .all()
    )
    abuse_reports = (
        (await session.execute(select(PrimitiveAbuseReport).where(PrimitiveAbuseReport.organisation_id == org_id)))
        .scalars()
        .all()
    )
    publishers = (await session.execute(select(Publisher).where(Publisher.organisation_id == org_id))).scalars().all()
    installs = (
        (
            await session.execute(
                include_soft_deleted(select(CollectionInstall).where(CollectionInstall.organisation_id == org_id))
            )
        )
        .scalars()
        .all()
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
        "composite_templates": _serialise(composites),
        "primitive_ratings": _serialise(ratings),
        "primitive_abuse_reports": _serialise(abuse_reports),
        "publishers": _serialise(publishers),
        "collection_installs": _serialise(installs),
        "connector_instances": _serialise(connectors),
        "model_backends": _serialise(backends),
        "exported_at": datetime.now(UTC).isoformat(),
    }


# ── Public API ───────────────────────────────────────────────────────


async def request_org_deletion(
    session: AsyncSession,
    org_id: uuid.UUID,
    _actor_user_id: uuid.UUID,
    _settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Initiate the soft-delete workflow for an organisation.

    1. Pre-lock read (no FOR UPDATE, include_soft_deleted) to validate the org
       exists and status != "deleted".
    2. Collect the export via a read-only REPEATABLE READ snapshot on a SEPARATE
       connection (BEFORE any lock).
    3. Take the FSM FOR UPDATE re-read of the org row and RE-VALIDATE the
       premises under the lock (org still exists, still not deleted) — a
       concurrent confirm/cancel may have landed between snapshot and lock.
    4. Transition (status/deleted_at/token/expiry), persist export_bundle_json,
       flush — all under the short lock.

    The snapshot is collected BEFORE the FSM lock, so a write landing between
    the snapshot and the lock acquisition is not included in the export bundle.
    Deletion is soft-first with a 24 h cancel path, so the window is bounded
    and the user can cancel if the export missed a late change.

    Returns a dict with `token`, `token_expires_at`, and `export` keys.
    """
    # Step 1: Pre-lock validation read (no lock, include_soft_deleted).
    result = await session.execute(include_soft_deleted(select(Organisation).where(Organisation.id == org_id)))
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError(_ERR_ORG_NOT_FOUND)
    if org.status == "deleted":
        raise ValueError("Organisation is already deleted")

    # Step 2: Collect the export in a read-only REPEATABLE READ snapshot
    # on a SEPARATE connection — BEFORE taking the FSM lock.
    export = await _collect_org_export_in_snapshot(org_id)

    # Step 3: FSM FOR UPDATE re-read and premise re-check under the lock.
    # FAR-1624 org-lock audit (updated FAR-1823): intentionally `FOR UPDATE`,
    # NOT `FOR NO KEY UPDATE`. This site acquires the org lock for the SHORT
    # FSM transition only (snapshot was collected BEFORE the lock). The lock
    # is kept so the deletion transition blocks concurrent child inserts
    # (FOR KEY SHARE) before the hard delete. Premises are RE-VALIDATED under
    # the lock because a concurrent confirm/cancel may have changed the org
    # state between the snapshot and this lock acquisition.
    result = await session.execute(
        include_soft_deleted(select(Organisation).where(Organisation.id == org_id).with_for_update())
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError(_ERR_ORG_NOT_FOUND)
    if org.status == "deleted":
        raise ValueError("Organisation is already deleted")

    # Step 4: Transition under the short lock.
    token = _generate_deletion_token()
    expires_at = datetime.now(UTC) + timedelta(hours=CONFIRMATION_WINDOW_HOURS)

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
    # FAR-1624 org-lock audit: intentionally `FOR UPDATE`, NOT `FOR NO KEY
    # UPDATE`. This deletion path acquires the org lock FIRST (no pre-held FK
    # `KEY SHARE` on the org), so it cannot hit the KEY-SHARE->FOR-UPDATE
    # upgrade cycle; the strongest lock is kept so the hard delete blocks
    # concurrent child inserts.
    result = await session.execute(
        include_soft_deleted(select(Organisation).where(Organisation.id == org_id).with_for_update())
    )
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

    # FAR-1102: purge governance decision records BEFORE the org hard-delete.
    # The RESTRICT FK chain (pipelines -> evals -> policy_gates ->
    # policy_gate_decisions) means hard-deleting the org with decision rows
    # present raises IntegrityError → raw 500.  Child-first purge of the
    # decision rows prevents this.  This is the same shared helper used by
    # Path 6 (organisation.py delete_organisation).
    from modulo.db.crud.policy_gate_decision import purge_org_decision_records

    await purge_org_decision_records(session, org_id)

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
    # FAR-1624 org-lock audit: intentionally `FOR UPDATE`, NOT `FOR NO KEY
    # UPDATE` — this read-modify-write acquires the org lock FIRST (no pre-held
    # FK `KEY SHARE` on the org), so it cannot hit the KEY-SHARE->FOR-UPDATE
    # upgrade cycle.
    result = await session.execute(
        include_soft_deleted(select(Organisation).where(Organisation.id == org_id).with_for_update())
    )
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


async def _collect_org_export_in_snapshot(org_id: uuid.UUID, engine: AsyncEngine | None = None) -> dict[str, Any]:
    """Collect the org export bundle in a read-only REPEATABLE READ snapshot.

    This runs on a SEPARATE connection/session from the caller's transaction.
    Postgres rejects SET TRANSACTION ISOLATION LEVEL mid-transaction, so the
    isolation must be established on a fresh connection before its first
    statement. We use the shared engine (or injected engine for tests) to create
    a connection with REPEATABLE READ isolation via execution_options, set it to
    READ ONLY on Postgres, then bind a session to it.

    The snapshot session only reads by org_id; it never attaches the caller's
    ORM object. This ensures the export reflects a consistent point-in-time
    view without blocking concurrent writers.

    NOTE: this function holds a SECOND pooled connection for the export
    duration. The endpoint is admin-only and bounded by pool_timeout.
    """
    if engine is None:
        engine = get_shared_engine()

    # Configure REPEATABLE READ before starting the transaction on PostgreSQL.
    # execution_options returns the same AsyncConnection after configuring it.
    async with engine.connect() as base_conn:
        dialect = base_conn.dialect.name
        conn: AsyncConnection = base_conn
        if dialect == "postgresql":
            conn = await base_conn.execution_options(isolation_level="REPEATABLE READ")
            await conn.execute(text("SET TRANSACTION READ ONLY"))

        # Bind a session to this connection for ORM queries.
        snapshot_factory = async_sessionmaker(
            bind=conn,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

        async with snapshot_factory() as session, session.begin():
            # The transaction starts on the session (joins the connection's transaction).
            # Set RLS org context so tenant-scoped reads (pipelines, runs, etc.)
            # return data on Postgres (RLS) and generic backends (session.info filter).
            await set_rls_org(session, org_id)

            # Read the org with include_soft_deleted visibility (same as caller).
            result = await session.execute(include_soft_deleted(select(Organisation).where(Organisation.id == org_id)))
            org = result.scalar_one_or_none()
            if org is None:
                raise ValueError(_ERR_ORG_NOT_FOUND)

            # Collect the export using the snapshot session.
            export = await _collect_org_export(session, org)

        # Transaction committed by the session context manager.
        # Connection closed by the outer context manager.

    return export


async def export_org_data(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Return the export bundle for an org (captures live data if none exists).

    FAR-1624 org-lock audit (updated FAR-1823): the org-row `FOR UPDATE` lock
    was REMOVED. This read path now uses a read-only REPEATABLE READ snapshot
    on a separate connection for live captures. The stored
    `export_bundle_json` is returned directly when present (no snapshot needed).
    The org-row lock never gave cross-table consistency because child-row
    UPDATEs were never blocked by it — the export reads multiple child tables
    that the org lock does not protect.
    """
    # Read the org WITHOUT a lock, but with include_soft_deleted visibility.
    result = await session.execute(include_soft_deleted(select(Organisation).where(Organisation.id == org_id)))
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError(_ERR_ORG_NOT_FOUND)

    if org.export_bundle_json is not None:
        return org.export_bundle_json

    # Live capture: use a read-only REPEATABLE READ snapshot on a separate connection.
    return await _collect_org_export_in_snapshot(org_id)


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


# FAR-1971a: sweep candidates are (organisation_id, thread_id) pairs, deleted
# with an equality predicate on BOTH leading columns so the checkpoint tables'
# existing indexes serve the delete. ``ix_checkpoints_org_thread``,
# ``ix_checkpoint_blobs_org`` and ``ix_checkpoint_writes_org`` all LEAD with
# ``organisation_id``; the historical form batched by ``ctid`` through a join
# on the bare ``thread_id`` column, for which no checkpoint index leads, so the
# plan was a seq scan + hash join over the multi-GB checkpoint tables.
#
# Class 1 — a terminal ``runs`` row aged past the cutoff (the deployed-schema
# production path). The ``EXISTS`` probes are index-served on
# (organisation_id, thread_id), so a fully-purged thread drops out of the
# candidate set and the sweep resumes from the remainder on the next tick.
#
# The ``NOT EXISTS`` guard is the SAME one Class 2 carries, and it is the
# function's stated invariant: a thread is a candidate only when NO non-terminal
# run shares it. A resume child (``run_group_kind='resume'``) legitimately
# shares its parent's ``langgraph_thread_id``, so an aged terminal parent must
# not drag a live child's resume state out with it. The anti-join is bounded:
# the outer ``LIMIT`` caps the candidate fetch, and the probe is served by the
# ``langgraph_thread_id`` index.
_RUN_EXPIRED_CANDIDATES_SQL = """
SELECT r.organisation_id, r.langgraph_thread_id
FROM runs r
WHERE r.status = ANY(:terminal_statuses)
  AND COALESCE(r.completed_at, r.created_at) < :cutoff
  AND NOT EXISTS (
    SELECT 1 FROM runs live
    WHERE live.langgraph_thread_id = r.langgraph_thread_id
      AND live.status <> ALL(:terminal_statuses)
  )
  AND (
    EXISTS (
      SELECT 1 FROM checkpoints c
      WHERE c.organisation_id = r.organisation_id AND c.thread_id = r.langgraph_thread_id
    )
    OR EXISTS (
      SELECT 1 FROM checkpoint_blobs b
      WHERE b.organisation_id = r.organisation_id AND b.thread_id = r.langgraph_thread_id
    )
    OR EXISTS (
      SELECT 1 FROM checkpoint_writes w
      WHERE w.organisation_id = r.organisation_id AND w.thread_id = r.langgraph_thread_id
    )
  )
ORDER BY r.created_at, r.id
LIMIT :limit
"""

# Class 2 (only when the checkpoint tables carry ``created_at`` — the test DB
# and any boot that ran the saver migrations) — a checkpoint row older than the
# cutoff whose owning run is terminal or absent. Preserves the historical
# ``created_at < cutoff OR run-age < cutoff`` semantics. The owner guard admits
# orphan threads (no ``runs`` row) and excludes a live run, so an
# ``awaiting_human`` run paused at a HITL gate keeps its interrupt checkpoint.
# Each query repeats the guard verbatim rather than interpolating it: the SQL is
# a module constant and text() must receive a fully-literal statement (no
# string-built SQL, ruff S608).
_STALE_CHECKPOINT_CANDIDATES_SQL = """
SELECT DISTINCT t.organisation_id, t.thread_id
FROM checkpoints t
WHERE t.created_at < :cutoff
  AND NOT EXISTS (
    SELECT 1 FROM runs r
    WHERE r.langgraph_thread_id = t.thread_id AND r.status <> ALL(:terminal_statuses)
  )
LIMIT :limit
"""

_STALE_WRITE_CANDIDATES_SQL = """
SELECT DISTINCT t.organisation_id, t.thread_id
FROM checkpoint_writes t
WHERE t.created_at < :cutoff
  AND NOT EXISTS (
    SELECT 1 FROM runs r
    WHERE r.langgraph_thread_id = t.thread_id AND r.status <> ALL(:terminal_statuses)
  )
LIMIT :limit
"""

# A blob is only reclaimed when no checkpoint remains in its thread; the guard
# is index-served and keeps a fresh checkpoint's idle-channel blob alive.
_STALE_BLOB_CANDIDATES_SQL = """
SELECT DISTINCT t.organisation_id, t.thread_id
FROM checkpoint_blobs t
WHERE t.created_at < :cutoff
  AND NOT EXISTS (
    SELECT 1 FROM runs r
    WHERE r.langgraph_thread_id = t.thread_id AND r.status <> ALL(:terminal_statuses)
  )
  AND NOT EXISTS (
    SELECT 1 FROM checkpoints c
    WHERE c.organisation_id = t.organisation_id
      AND c.thread_id = t.thread_id
      AND c.checkpoint_ns = t.checkpoint_ns
  )
LIMIT :limit
"""

# Row deletes, grouped by (organisation_id, run_expired). ``IN :threads`` is an
# expanding bindparam, so the predicate is ``organisation_id = :org AND
# thread_id IN (...)`` — a two-column prefix of every checkpoint index.
# ``checkpoint_blobs`` is deleted LAST so its "no checkpoint remains" guard sees
# the checkpoints already removed in the same transaction.
_DELETE_ALL_THREADS_SQL: dict[str, str] = {
    "checkpoints": "DELETE FROM checkpoints WHERE organisation_id = :org AND thread_id IN :threads",
    "checkpoint_writes": "DELETE FROM checkpoint_writes WHERE organisation_id = :org AND thread_id IN :threads",
    "checkpoint_blobs": (
        "DELETE FROM checkpoint_blobs b WHERE b.organisation_id = :org AND b.thread_id IN :threads "
        "AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.organisation_id = :org "
        "AND c.thread_id = b.thread_id AND c.checkpoint_ns = b.checkpoint_ns)"
    ),
}
_DELETE_STALE_ROWS_SQL: dict[str, str] = {
    "checkpoints": (
        "DELETE FROM checkpoints WHERE organisation_id = :org AND thread_id IN :threads AND created_at < :cutoff"
    ),
    "checkpoint_writes": (
        "DELETE FROM checkpoint_writes WHERE organisation_id = :org AND thread_id IN :threads AND created_at < :cutoff"
    ),
    "checkpoint_blobs": (
        "DELETE FROM checkpoint_blobs b WHERE b.organisation_id = :org AND b.thread_id IN :threads "
        "AND b.created_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.organisation_id = :org "
        "AND c.thread_id = b.thread_id AND c.checkpoint_ns = b.checkpoint_ns)"
    ),
}
#: Deletion order — blobs must be last (see above).
_CHECKPOINT_DELETE_ORDER = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")


async def _purge_candidates(
    session: AsyncSession,
    *,
    cutoff: datetime,
    terminal_statuses: list[str],
    has_created_at: bool,
    limit: int,
) -> list[tuple[uuid.UUID, str, bool]]:
    """Return up to ``limit`` ``(org_id, thread_id, run_expired)`` sweep candidates.

    ``run_expired`` is ``True`` for a thread whose owning run is terminal and
    aged past the cutoff (purge the whole thread), and ``False`` for a thread
    that merely contains stale rows (purge only those rows). A thread found by
    both sources keeps the stronger ``True``.
    """
    seen: dict[tuple[uuid.UUID, str], bool] = {}
    run_rows = await session.execute(
        text(_RUN_EXPIRED_CANDIDATES_SQL),
        {"cutoff": cutoff, "terminal_statuses": terminal_statuses, "limit": limit},
    )
    for row in run_rows.all():
        seen[(row[0], row[1])] = True
    if has_created_at:
        remaining = limit - len(seen)
        for stale_sql in (_STALE_CHECKPOINT_CANDIDATES_SQL, _STALE_WRITE_CANDIDATES_SQL, _STALE_BLOB_CANDIDATES_SQL):
            if remaining <= 0:
                break
            stale_rows = await session.execute(
                text(stale_sql),
                {"cutoff": cutoff, "terminal_statuses": terminal_statuses, "limit": remaining},
            )
            for row in stale_rows.all():
                key = (row[0], row[1])
                if key not in seen:
                    seen[key] = False
                    remaining -= 1
    return [(org_id, thread_id, run_expired) for (org_id, thread_id), run_expired in seen.items()]


async def _delete_thread_batch(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    thread_ids: list[str],
    run_expired: bool,
    cutoff: datetime,
) -> int:
    """Delete one batch's checkpoint rows for a single org; return the rows deleted.

    ``run_expired`` selects the predicate: an old terminal run's whole thread is
    purged, while a thread that only *contains* stale rows keeps its fresh ones.
    """
    templates = _DELETE_ALL_THREADS_SQL if run_expired else _DELETE_STALE_ROWS_SQL
    params: dict[str, Any] = {"org": str(org_id), "threads": thread_ids}
    if not run_expired:
        params["cutoff"] = cutoff
    deleted = 0
    for table in _CHECKPOINT_DELETE_ORDER:
        stmt = text(templates[table]).bindparams(bindparam("threads", expanding=True))
        result = await session.execute(stmt, params)
        deleted += getattr(result, "rowcount", 0) or 0
    return deleted


async def batch_delete_langgraph_checkpoints(
    session: AsyncSession,
    *,
    batch_size: int = CHECKPOINT_BATCH_SIZE,
    max_age_days: int = CHECKPOINT_RETENTION_DAYS,
    max_rows_per_tick: int = CHECKPOINT_MAX_ROWS_PER_TICK,
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

    AGE IS MEASURED FROM ``runs`` (FAR-432). The age predicate is
    ``COALESCE(runs.completed_at, runs.created_at) < cutoff`` so the sweep works
    regardless of whether the checkpoint ``created_at`` column exists. When the
    column is present (the test DB and any boot that ran the saver migrations)
    a checkpoint older than the cutoff is also reclaimed for a terminal run —
    the historical ``created_at < cutoff OR run-age < cutoff`` semantics.

    BATCHING (FAR-1971). ``batch_size`` bounds the number of candidate
    *threads* fetched and deleted per committed batch (not the historical
    raw-row ctid batch). Each batch runs in its own transaction and is
    committed before the next is fetched, so a mid-tick cancellation can never
    discard work that already succeeded — the previous single-transaction loop
    made no observable progress across five minutes of retries. The whole tick
    is bounded by ``max_rows_per_tick`` so it finishes well inside the job's
    300s timeout instead of running to the wall-clock limit; the next tick
    resumes from the rows that remain.

    Operates directly on the unqualified table names the
    ``ModuloPostgresSaver`` migrations create (``checkpoints``,
    ``checkpoint_blobs``, ``checkpoint_writes``) — they land in the
    connection's default search_path schema, not a ``langgraph`` schema.
    Blob rows are not FK-cascaded from checkpoints (the saver migrations
    define no foreign keys), so all three tables are purged.

    ``checkpoint_writes`` rows are keyed by their owning checkpoint
    (``checkpoint_id``) and written together with it. ``checkpoint_blobs`` are
    keyed by (``thread_id``, ``checkpoint_ns``, ``channel``, ``version``) and
    shared across checkpoints in a thread; their reference lives inside the
    Fernet-encrypted ``checkpoint`` JSONB (``channel_versions``), so it cannot
    be probed in SQL. A blob is therefore only deleted when NO checkpoint
    remains in its thread — checked after the checkpoints pass of the same
    batch, so an old idle-channel blob still referenced by a fresh checkpoint
    is retained until that checkpoint itself ages out.

    The session factory used by the hourly system cron is ``autobegin=False``,
    so every statement here sits inside an explicit ``session.begin()``.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    terminal_statuses = list(TERMINAL_STATUSES)
    deleted_total = 0

    async with session.begin():
        has_created_at = await _checkpoint_created_at_present(session)

    while deleted_total < max_rows_per_tick:
        batch_deleted = 0
        async with session.begin():
            candidates = await _purge_candidates(
                session,
                cutoff=cutoff,
                terminal_statuses=terminal_statuses,
                has_created_at=has_created_at,
                limit=batch_size,
            )
            if not candidates:
                break
            grouped: dict[tuple[uuid.UUID, bool], list[str]] = {}
            for org_id, thread_id, run_expired in candidates:
                grouped.setdefault((org_id, run_expired), []).append(thread_id)
            for (org_id, run_expired), thread_ids in grouped.items():
                batch_deleted += await _delete_thread_batch(
                    session,
                    org_id=org_id,
                    thread_ids=thread_ids,
                    run_expired=run_expired,
                    cutoff=cutoff,
                )
        # The batch (FAR-1971b) is committed here. Stop once the per-tick row
        # budget (FAR-1971c) is spent, or once a fetch returned a short batch.
        deleted_total += batch_deleted
        if batch_deleted == 0 or len(candidates) < batch_size:
            break

    return deleted_total

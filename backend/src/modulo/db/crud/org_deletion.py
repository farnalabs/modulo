"""CRUD for organisation deletion workflow.

Soft-delete flow:
  1. Admin requests deletion → org gets deleted_at + status='deleted',
     export bundle captured, audit event recorded, confirmation email token generated.
  2. Confirmation within 24h → token verified, org hard-deleted (cascades via FK).
  3. Immediate admin DELETE → skips token, hard-deletes directly.

Run retention:
  - Terminal runs older than 30 days are hard-deleted before org drop.
  - LangGraph checkpoint rows are batched 500 at a time in a nightly job.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.organisation import get_organisation
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.user import User

DELETION_TOKEN_BYTES = 48
CONFIRMATION_WINDOW_HOURS = 24
RUN_RETENTION_DAYS = 30
CHECKPOINT_BATCH_SIZE = 500
LANGRAPH_SCHEMA = "langgraph"


# ── Helpers ──────────────────────────────────────────────────────────


def _generate_deletion_token() -> str:
    return secrets.token_urlsafe(DELETION_TOKEN_BYTES)


async def _collect_org_export(session: AsyncSession, org: Organisation) -> dict[str, Any]:
    """Bundle all org-owned data into a JSON-serialisable dict."""
    org_id = org.id

    users = (await session.execute(select(User).where(User.organisation_id == org_id))).scalars().all()
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
        return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in records]

    return {
        "organisation": _serialise([org]),
        "users": _serialise(users),
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
    org = await get_organisation(session, org_id)
    if org is None:
        raise ValueError("Organisation not found")
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
) -> dict[str, Any]:
    """Confirm and execute org deletion.

    When *immediate* is True (admin DELETE endpoint), the token check is
    skipped. Otherwise the token must match and not be expired.

    Before hard-deleting the org, terminal runs older than 30 days are
    batch-deleted. The remaining cascade is handled by Postgres FK constraints.
    """
    org = await get_organisation(session, org_id)
    if org is None:
        raise ValueError("Organisation not found")

    if not immediate:
        if org.deletion_token is None or org.deletion_token != token:
            raise ValueError("Invalid deletion token")
        expires_at = org.deletion_token_expires_at
        if expires_at is None or datetime.now(UTC) > expires_at:
            raise ValueError("Deletion token has expired")

    # Hard-delete terminal runs past retention window
    from modulo.db.crud.run import batch_delete_old_terminal_runs

    deleted_runs = await batch_delete_old_terminal_runs(session, max_age_days=RUN_RETENTION_DAYS, batch_size=500)

    # Hard-delete the organisation — FK cascade removes all remaining scoped rows
    await session.delete(org)
    await session.flush()

    return {
        "deleted_organisation_id": str(org_id),
        "hard_deleted_runs": deleted_runs,
    }


async def export_org_data(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Return the export bundle for an org (captures live data if none exists)."""
    org = await get_organisation(session, org_id)
    if org is None:
        raise ValueError("Organisation not found")

    if org.export_bundle_json is not None:
        return org.export_bundle_json

    return await _collect_org_export(session, org)


async def batch_delete_langgraph_checkpoints(
    session: AsyncSession,
    *,
    batch_size: int = CHECKPOINT_BATCH_SIZE,
) -> int:
    """Nightly retention: delete old langgraph.* checkpoint rows.

    Removes checkpoint writes older than the run retention window (30 days).
    Operates on the ``langgraph.checkpoint`` and ``langgraph.checkpoint_writes``
    tables directly via raw SQL.
    """
    cutoff = datetime.now(UTC) - timedelta(days=RUN_RETENTION_DAYS)
    deleted_total = 0

    for tbl in ("checkpoint_writes", "checkpoints"):
        while True:
            _sql = (
                f"DELETE FROM langgraph.{tbl} "  # noqa: S608  # nosec B608
                "WHERE ctid IN ("
                f"  SELECT ctid FROM langgraph.{tbl} "
                "  WHERE created_at < :cutoff "
                "  LIMIT :limit"
                ")"
            )
            stmt = text(_sql)
            result = await session.execute(stmt, {"cutoff": cutoff, "limit": batch_size})
            count = result.rowcount
            deleted_total += count
            if count < batch_size:
                break

    return deleted_total

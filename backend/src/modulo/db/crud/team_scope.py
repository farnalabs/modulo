"""Shared team-boundary helpers for the RLS-parity authorization floor.

The ``owner_team_id`` boundary predicate — org-level (NULL owner) OR owned by
the caller's team — and the effective-owner resolver are centralised here so
the semantics cannot drift between the list filters (pipelines, runs, triggers,
HITL pending gates, analytics facts) and the MCP per-row guards.

Live in the DB layer (not ``modulo.api``) so both ``modulo.db`` and
``modulo.core`` can import it without violating the layer contracts.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.pipeline import Pipeline


def team_scope_clause(owner_team_id_col: Any, team_id: Any) -> ColumnElement[bool]:
    """Build the team-boundary WHERE predicate for *team_id*.

    An org-level row (NULL owner) is visible to every team; a row owned by
    *team_id* is visible to that team. Pass an effective-owner expression
    (e.g. ``func.coalesce(Run.owner_team_id, Pipeline.owner_team_id)``) when
    the row's stamped owner must take precedence over the pipeline's current
    owner.
    """
    return or_(owner_team_id_col.is_(None), owner_team_id_col == team_id)


async def pipeline_owner_team_id(session: AsyncSession, pipeline_id: uuid.UUID) -> uuid.UUID | None:
    """Resolve a pipeline's ``owner_team_id`` (None for org-level pipelines)."""
    result = await session.execute(select(Pipeline.owner_team_id).where(Pipeline.id == pipeline_id))
    return result.scalar_one_or_none()


@dataclass(frozen=True)
class BlindPipelineScope:
    """A pipeline's team-gate inputs as seen even when RLS hides the row.

    FAR-1513: a caller-facing read under user RLS returns nothing for a
    team-private pipeline the caller cannot see, which made the pre-existing
    MCP gate fail OPEN (unknown visibility → allow). This read is org-scoped
    but team-BLIND: it flips the ``app.execution_context`` GUC so the
    ``rls_team_isolation`` policy's execution-context arm lets the org-scoped
    row through, then clears the GUC so the *rest* of the transaction keeps
    caller-facing visibility. Callers must act on ``row_present`` first —
    an absent row means the pipeline does not exist (or is soft-deleted),
    which is a denial by itself.
    """

    owner_team_id: uuid.UUID | None
    visibility: str | None


async def pipeline_team_scope_team_blind(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> BlindPipelineScope | None:
    """Read a pipeline's team-gate fields UNBLINDED to team visibility.

    Org-scoped (``app.organisation_id`` stays set) but team-blind: inside the
    same transaction the ``app.execution_context`` GUC is turned ON for the
    read and turned OFF again afterwards, so only this read sees through
    team isolation and to every later read in the session stays caller-facing.
    Returns ``None`` when no non-deleted pipeline with that id exists in the
    org — calls to never use this to *surface* the row, only to decide a
    boundary denial.

    Raises ``RuntimeError`` when called outside an active transaction (same
    contract as ``set_rls_execution_context``); on any other failure the GUC
    reverts with the transaction rollback, so no execution-context leak is
    possible from a failed read.
    """
    from modulo.db.rls import _ensure_active_transaction, set_rls_execution_context

    dialect = await _ensure_active_transaction(session)
    if dialect != "postgresql":
        # Only Postgres carries the team-isolation policy; on SQLite/MariaDB
        # team filtering does not exist, so the plain org-scoped read IS the
        # team-blind view there. session.info carries no effective GUC.
        stmt = select(Pipeline.owner_team_id, Pipeline.visibility).where(
            Pipeline.id == pipeline_id,
            Pipeline.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return BlindPipelineScope(owner_team_id=row[0], visibility=row[1])

    await set_rls_execution_context(session)
    try:
        stmt = select(Pipeline.owner_team_id, Pipeline.visibility).where(
            Pipeline.id == pipeline_id,
            Pipeline.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        row = result.first()
    finally:
        # Clear the GUC ONLY after a successful read: if the read raised, the
        # transaction unwinds and is_local reverts with the rollback, so there
        # is no branch that leaves execution context set on a live session.
        await session.execute(text("SELECT set_config('app.execution_context', '', true)"))
    if row is None:
        return None
    return BlindPipelineScope(owner_team_id=row[0], visibility=row[1])

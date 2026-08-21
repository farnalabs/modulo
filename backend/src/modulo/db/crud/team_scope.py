"""Shared team-boundary helpers for the RLS-parity authorization floor.

The ``owner_team_id`` boundary predicate — org-level (NULL owner) OR owned by
the caller's team — and the effective-owner resolver are centralised here so
the semantics cannot drift between the list filters (pipelines, runs, triggers,
HITL pending gates, analytics facts) and the MCP per-row guards.

Live in the DB layer (not ``modulo.api``) so both ``modulo.db`` and
``modulo.core`` can import it without violating the layer contracts.
"""

import uuid
from typing import Any

from sqlalchemy import ColumnElement, or_, select
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

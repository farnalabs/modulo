"""Shared team-boundary helpers for the RLS-parity authorization floor.

The ``owner_team_id`` boundary predicate — org-level (NULL owner) OR owned by
the caller's team — and the effective-owner resolver are centralised here so
the semantics cannot drift between the list filters (pipelines, runs, triggers,
HITL pending gates, analytics facts) and the MCP per-row guards.

Live in the DB layer (not ``modulo.api``) so both ``modulo.db`` and
``modulo.core`` can import it without violating the layer contracts — which is
also why :func:`team_blind_org_scope` (the FAR-1515 widened read) lives HERE:
the binding gates in ``modulo.core.team_visibility`` need it, and ``modulo.db``
must not import ``modulo.core``.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Text, cast, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.pipeline import Pipeline

# FAR-1515 CRITICAL 1: candidate rows behind the connector/model-backend team
# gates must be read TEAM-BLIND but ORG-SCOPED. ``rls_team_isolation``
# (migration 0124) hides a ``visibility='team'`` row the caller does not own,
# so a plain read in the request session returns NOTHING for another team's
# row - the mismatch loop would skip it silently and the write would be
# accepted (at run time the hub loads the same row team-blind). These
# statements widen only the TEAM clause (``app.execution_context``) while
# keeping the org AND gate, then restore the caller's previous GUCs so the
# rest of the request transaction keeps its original visibility. Same
# ``set_config(..., is_local => true)`` form as ``db/rls.py``
# (transaction-scoped, reverts on rollback).
_RLS_READ_GUCS_SQL = (
    "SELECT current_setting('app.organisation_id', true), current_setting('app.execution_context', true)"
)
_RLS_WIDEN_SQL = (
    "SELECT set_config('app.organisation_id', :oid, true), set_config('app.execution_context', 'true', true)"
)
_RLS_RESTORE_SQL = (
    "SELECT set_config('app.organisation_id', :oid, true), set_config('app.execution_context', :exec_ctx, true)"
)


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


async def _resolve_dialect(session: AsyncSession) -> str:
    """Return the session bind's dialect name (``"postgresql"``, ``"sqlite"``, ...).

    Mirrors ``db/rls._ensure_active_transaction``'s bind resolution: a sync
    session returns the engine directly, an async session may return a
    coroutine that must be awaited. Type-annotated loosely because test
    doubles satisfy it structurally.
    """
    bind: Any = session.get_bind()
    if asyncio.iscoroutine(bind):
        bind = await bind
    return str(bind.dialect.name)


@asynccontextmanager
async def team_blind_org_scope(session: AsyncSession, org_id: uuid.UUID) -> AsyncIterator[None]:
    """Run the wrapped block with team visibility widened to the WHOLE org.

    FAR-1515 CRITICAL 1: the team-scope gates must judge rows the request
    caller's own RLS context cannot see - another team's ``visibility='team'``
    connector, or (on the connector re-scope path) another team's
    team-private pipeline binding the connector. On Postgres the block saves
    the caller's ``app.organisation_id`` / ``app.execution_context`` GUCs,
    widens to the internal-execution context - still AND-gated by
    ``app.organisation_id = org_id``, so it can never cross organisations -
    runs, and restores the caller's GUCs in a ``finally`` so the rest of the
    request transaction keeps its original team visibility.

    Off Postgres there is no team RLS policy (the tenant filter scopes by org
    only), so reads are already team-blind: nothing is issued.
    """
    if await _resolve_dialect(session) != "postgresql":
        yield
        return
    prev_org, prev_exec_ctx = (await session.execute(text(_RLS_READ_GUCS_SQL))).one()
    await session.execute(text(_RLS_WIDEN_SQL), {"oid": str(org_id)})
    try:
        yield
    finally:
        await session.execute(
            text(_RLS_RESTORE_SQL),
            {"oid": prev_org or "", "exec_ctx": prev_exec_ctx or ""},
        )


async def pipelines_binding_connector(
    session: AsyncSession,
    org_id: uuid.UUID,
    connector_id: uuid.UUID,
) -> list[Pipeline]:
    """Candidate pipelines in *org_id* whose stored graph mentions *connector_id*.

    FAR-1515 MAJOR 5: the connector visibility/owner PATCH needs the set of
    pipelines that bind the instance it is about to re-scope. The JSON column
    is searched with a text-contains prefilter (portable across ``json`` on
    Postgres and the SQLite dev backend); a UUID can only appear as the exact
    id or inside a larger string, so the CALLER must confirm the real binding
    (``extract_connector_bindings`` + an exact id match) before acting on a
    candidate — this query is deliberately a cheap prefilter, not the verdict.

    Read through :func:`team_blind_org_scope` for the same reason the binding
    gate itself is: under ``rls_team_isolation`` another team's team-private
    pipeline (exactly the row whose binding a re-scope could strand) is
    invisible to the request caller, and a prefilter that cannot see its own
    targets fails open. Soft-deleted pipelines are excluded (they cannot run,
    so they cannot be harmed by the re-scope).
    """
    pattern = f"%{connector_id}%"
    async with team_blind_org_scope(session, org_id):
        result = await session.execute(
            select(Pipeline).where(
                Pipeline.organisation_id == org_id,
                Pipeline.deleted_at.is_(None),
                cast(Pipeline.graph_nodes_json, Text).like(pattern),
            )
        )
        return list(result.scalars())


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

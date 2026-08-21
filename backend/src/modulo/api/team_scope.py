"""Team-scope resolvers for the RLS-parity authorization floor (ADR 017 DECISION 2).

Each resolver loads a target row's ``owner_team_id`` and ``visibility`` so the
``require_team_membership_or_admin`` dependency can mirror the DB team-visibility
RLS policy exactly:

    (visibility = 'org' OR visibility IS NULL)
    OR (owner_team_id IS NULL)
    OR (owner_team_id IN (SELECT team_id FROM team_memberships WHERE account_id = ...))
    OR (org_role = 'admin')

Team-scoped resource set (Phase-1 floor): ``pipelines``,
``connector_instances``, ``model_backends``, ``environment_profiles``,
``library_primitives``, ``lifecycle_maps``. ``lifecycle_maps`` carries the
visibility CHECK constraint but only strict org RLS at the DB layer — the
membership gate here is its only team enforcement.

``runs`` is deliberately NOT team-scoped at the app layer: it has
``owner_team_id`` but no ``visibility`` column and strict org RLS, so it stays on
the org-role floor only (RLS parity — iteration-7 pinned special case; team
scoping of runs arrives with Phase-2 ``WITH CHECK``).

The matrix mapping each team-scoped route to its ``owner_team_id`` source is the
PR B deliverable; this module builds the MECHANISM and the pipeline resolver.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.team_membership import TeamMembership


@dataclass(frozen=True)
class TeamScopedResource:
    """The RLS-relevant fields of a team-scoped row, resolved by a provider."""

    owner_team_id: uuid.UUID | None
    visibility: str | None


TeamScopeProvider = Callable[[Request, AsyncSession], Awaitable[TeamScopedResource | None]]


async def team_membership_exists(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    team_id: uuid.UUID,
) -> bool:
    """Return True if ``account_id`` holds ANY membership row in ``team_id``.

    Mirrors the RLS ``owner_team_id IN (SELECT team_id FROM team_memberships
    WHERE account_id = ...)`` clause — any team role qualifies.
    """
    result = await session.execute(
        select(TeamMembership.id).where(
            TeamMembership.team_id == team_id,
            TeamMembership.account_id == account_id,
        )
    )
    return result.first() is not None


def team_scope_resolver(model: type[Any], *, path_param: str) -> TeamScopeProvider:
    """Build a resolver that loads ``owner_team_id``/``visibility`` by ID.

    The resolver reads the resource id from ``request.path_params[path_param]``
    and selects the row from ``model``. A missing row returns ``None`` so the
    dependency raises 404 (the route would 404 on the same id anyway). Callers
    must run this inside a transaction with ``set_rls_org``/``set_rls_user_context``
    active — the ``require_team_membership_or_admin`` dependency does this.
    """

    async def _resolve(request: Request, session: AsyncSession) -> TeamScopedResource | None:
        raw = request.path_params.get(path_param)
        if raw is None:
            return None
        try:
            obj_id = uuid.UUID(str(raw))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {path_param} path parameter",
            ) from None
        stmt = select(model.owner_team_id, model.visibility).where(model.id == obj_id)
        if hasattr(model, "deleted_at"):
            stmt = stmt.where(model.deleted_at.is_(None))
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return TeamScopedResource(owner_team_id=row[0], visibility=row[1])

    return _resolve


# Pipeline is the primary, most security-relevant team-scoped surface.
resolve_pipeline_team_scope = team_scope_resolver(Pipeline, path_param="pipeline_id")

# Pattern instantiations for the remaining team-scoped tables. Each is wired by
# PR B once its routes are swept; the resolver mechanism is identical.
resolve_connector_team_scope = team_scope_resolver(ConnectorInstance, path_param="connector_id")
resolve_model_backend_team_scope = team_scope_resolver(ModelBackend, path_param="backend_id")
resolve_environment_profile_team_scope = team_scope_resolver(EnvironmentProfile, path_param="profile_id")
resolve_library_primitive_team_scope = team_scope_resolver(LibraryPrimitive, path_param="primitive_id")
resolve_lifecycle_map_team_scope = team_scope_resolver(LifecycleMap, path_param="lifecycle_map_id")

# The team-scoped resource set (ADR 017 DECISION 2). ``runs`` is deliberately
# absent: it has ``owner_team_id`` but no ``visibility`` column and strict org
# RLS, so it stays on the org-role floor only (RLS parity). Pin that here so an
# accidental runs resolver is a test failure, not a silent scoping regression.
TEAM_SCOPED_RESOLVERS: dict[str, TeamScopeProvider] = {
    "pipelines": resolve_pipeline_team_scope,
    "connector_instances": resolve_connector_team_scope,
    "model_backends": resolve_model_backend_team_scope,
    "environment_profiles": resolve_environment_profile_team_scope,
    "library_primitives": resolve_library_primitive_team_scope,
    "lifecycle_maps": resolve_lifecycle_map_team_scope,
}

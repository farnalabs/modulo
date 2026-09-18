"""Team-scope resolvers for the RLS-parity authorization floor (ADR 038).

Each resolver loads a target row's ``owner_team_id`` and ``visibility`` so the
``require_team_membership_or_admin`` dependency can mirror the DB team-visibility
RLS policy exactly:

    (visibility = 'org' OR visibility IS NULL)
    OR (owner_team_id IS NULL)
    OR (owner_team_id IN (SELECT team_id FROM team_memberships WHERE account_id = ...))
    OR (org_role = 'admin')

Team-scoped resource set (Phase-1 floor): ``pipelines``,
``connector_instances``, ``model_backends``, ``environment_profiles``,
``library_primitives``, ``lifecycle_maps``, ``eval_datasets``,
``eval_suites``. ``lifecycle_maps`` carries the
visibility CHECK constraint but only strict org RLS at the DB layer — the
membership gate here is its only team enforcement.

``runs`` has ``owner_team_id`` but no ``visibility`` column and strict org RLS.
Run access is derived from pipeline access (ADR 038): a run executes with the
pipeline owner's authority, so runs stay on the org-role floor and
``owner_team_id`` is metadata, not a security control.  For ``trigger_run``
(POST /runs), the pipeline_id in the **request body** is read by a body resolver.

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
from modulo.db.models.eval_dataset import EvalDataset
from modulo.db.models.eval_suite import EvalSuite
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.team import Team
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
resolve_eval_dataset_team_scope = team_scope_resolver(EvalDataset, path_param="dataset_id")
resolve_eval_suite_team_scope = team_scope_resolver(EvalSuite, path_param="suite_id")


async def resolve_trigger_run_team_scope(
    request: Request,
    session: AsyncSession,
) -> TeamScopedResource | None:
    """Resolve team scope from the pipeline_id in a trigger_run request body.

    Unlike path-param resolvers, this reads ``pipeline_id`` from the JSON body
    (a Pydantic model that FastAPI has already validated).  The body is parsed
    lazily only when this resolver runs, avoiding an extra dependency-layer read.
    """
    import json as _json

    try:
        raw_body = await request.body()
        body = _json.loads(raw_body) if raw_body else {}
    except Exception:
        return None
    raw_pid = body.get("pipeline_id")
    if raw_pid is None:
        return None
    try:
        obj_id = uuid.UUID(str(raw_pid))
    except (ValueError, TypeError):
        return None
    stmt = select(Pipeline.owner_team_id, Pipeline.visibility).where(Pipeline.id == obj_id)
    if hasattr(Pipeline, "deleted_at"):
        stmt = stmt.where(Pipeline.deleted_at.is_(None))
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None
    return TeamScopedResource(owner_team_id=row[0], visibility=row[1])


# The team-scoped resource set (ADR 017 DECISION 2, extended by ADR 038).
#
# ── Registry contract (FAR-950) ──────────────────────────────────────────
# Every SQLAlchemy model with BOTH ``owner_team_id`` and ``visibility`` MUST
# appear here, keyed by its ``__tablename__``.  Models with ``owner_team_id``
# but NO ``visibility`` (runs, eval_suite_run, journey) are intentionally
# absent — they stay on the org-role floor because access derives from their
# parent entity (ADR 038 Decision §5).
#
# CI enforces this via ``tests/architecture/test_team_scope_wiring.py``:
#   • model → registry key mapping convention: key == __tablename__
#   • allowlist (no-visibility models) must be explicit with one-line reasons
#   • adding a model with owner_team_id + visibility without a resolver FAILS
#
# When adding a new team-scoped model:
#   1. Add the resolver here (resolve_<name>_team_scope = team_scope_resolver(...))
#   2. Add it to TEAM_SCOPED_RESOLVERS keyed by __tablename__
#   3. The test picks it up automatically from the model's columns
# ─────────────────────────────────────────────────────────────────────────
#
# ``runs`` is deliberately absent: it has ``owner_team_id`` but no
# ``visibility`` column and strict org RLS, so it stays on the org-role floor
# only (RLS parity). ``eval_suite_run`` and ``journey`` are derived entities
# (ADR 038 Decision §5) that stay on the org-role floor — their parent's team
# gate controls access.  The trigger_run body resolver is wired separately via
# ``require_team_membership_or_admin_any_credential``.
TEAM_SCOPED_RESOLVERS: dict[str, TeamScopeProvider] = {
    "pipelines": resolve_pipeline_team_scope,
    "connector_instances": resolve_connector_team_scope,
    "model_backends": resolve_model_backend_team_scope,
    "environment_profiles": resolve_environment_profile_team_scope,
    "library_primitives": resolve_library_primitive_team_scope,
    "lifecycle_maps": resolve_lifecycle_map_team_scope,
    "eval_datasets": resolve_eval_dataset_team_scope,
    "eval_suites": resolve_eval_suite_team_scope,
}

_CREATE_DENIAL_DETAIL = "Cannot assign a resource to a team you are not a member of"
_FOREIGN_ORG_DETAIL = "Team {team_id} not found in this organisation."


async def validate_owner_team_for_create(
    session: AsyncSession,
    principal: Any,
    owner_team_id: uuid.UUID | None,
) -> None:
    """Validate a client-supplied ``owner_team_id``/``target_team_id`` on a CREATE path.

    The PATCH/import paths enforce the team gate
    (``require_team_membership_or_admin`` / ``_validate_owner_team``), but CREATE
    and copy endpoints persisted the raw client value (issue #1793): an operator
    could mint team-private resources owned by a foreign-org or non-existent
    team, or hand a resource to a team they don't belong to. Fail CLOSED:

    1. the team must exist in the caller's organisation (404 otherwise — even
       for admins, so a typo/probe is never silently persisted);
    2. non-admin callers must hold a membership row in that team (403),
       mirroring the PATCH ownership-reassignment rule.
    """
    if owner_team_id is None:
        return
    result = await session.execute(
        select(Team.id).where(
            Team.id == owner_team_id,
            Team.organisation_id == principal.organisation_id,
            Team.deleted_at.is_(None),
        )
    )
    if result.first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_FOREIGN_ORG_DETAIL.format(team_id=owner_team_id),
        )
    org_role = getattr(principal, "org_role", None)
    if org_role == "admin":
        return
    is_member = await team_membership_exists(
        session,
        account_id=principal.account_id,
        team_id=owner_team_id,
    )
    if not is_member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CREATE_DENIAL_DETAIL)


async def validate_team_transition_for_update(
    session: AsyncSession,
    principal: Any,
    *,
    current_owner_team_id: uuid.UUID | None,
    current_visibility: str | None,
    new_owner_team_id: uuid.UUID | None,
    new_visibility: str | None,
    requires_owner_team_for_team_visibility: bool = True,
) -> None:
    """Mirror the PATCH team gate (``_assert_team_transition_allowed``) generically.

    Guards an UPDATE that changes ``visibility`` and/or ``owner_team_id``:
    org-admins bypass membership checks; non-admins must be members of the
    CURRENT team (when the resource is team-private) and of any NEW owner team
    they reassign the resource to. A ``visibility='team'`` assignment without an
    owner team is rejected (422). The NEW owner team must exist in the caller's
    organisation (404, fail closed — zero create/edit bypass parity with
    :func:`validate_owner_team_for_create`).
    """
    if new_visibility == "team" and requires_owner_team_for_team_visibility and new_owner_team_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="owner_team_id is required when visibility is 'team'",
        )
    if getattr(principal, "org_role", None) == "admin":
        return

    # Current-team gate when the resource is team-private (mirrors pipelines'
    # `_assert_team_transition_allowed`).
    is_team_private = current_visibility not in ("org", None) and current_owner_team_id is not None
    if current_owner_team_id is not None and is_team_private:
        is_member = await team_membership_exists(
            session, account_id=principal.account_id, team_id=current_owner_team_id
        )
        if not is_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a member of the team that owns this resource",
            )

    if new_owner_team_id is not None and new_owner_team_id != current_owner_team_id:
        exists = await session.execute(
            select(Team.id).where(
                Team.id == new_owner_team_id,
                Team.organisation_id == principal.organisation_id,
                Team.deleted_at.is_(None),
            )
        )
        if exists.first() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_FOREIGN_ORG_DETAIL.format(team_id=new_owner_team_id),
            )
        is_member = await team_membership_exists(session, account_id=principal.account_id, team_id=new_owner_team_id)
        if not is_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot reassign a resource to a team you are not a member of",
            )

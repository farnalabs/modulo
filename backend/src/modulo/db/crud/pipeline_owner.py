"""Eligibility invariant for pipeline accountability owners (FAR-1161).

An accountability owner (``business_owner_id`` / ``reliability_owner_id``)
MUST satisfy both conditions before assignment — the check FAILS CLOSED:

1. the account is an ACTIVE member of the pipeline's organisation
   (account row exists, ``accounts.active`` is true, and an
   ``org_memberships`` row exists for the org with ``deactivated_at`` NULL), and
2. the account satisfies the pipeline's visibility scope: when
   ``visibility == 'team'`` the account must hold a membership row in the
   pipeline's ``owner_team_id`` team.

Rationale (why fail closed): an incident notification carries the pipeline
name and a run link — delivering it to a principal outside the tenancy
boundary would leak it. An invalid owner is REJECTED with a specific error
(422), never silently dropped.

Membership representations (inspected for this ticket):
* org membership  -> ``OrgMembership`` (``org_memberships``), soft-deactivated
  via ``deactivated_at``; account-level deactivation via ``Account.active``.
* team membership -> ``TeamMembership`` (``team_memberships``), any role row
  qualifies (mirrors the RLS ``owner_team_id IN (SELECT team_id FROM
  team_memberships ...)`` clause in ``api.team_scope``).

Raises ``fastapi.HTTPException(422)`` directly — the same service-layer
pattern ``db.crud.hitl_gate_guard`` uses for structured authz denials, so
``handle_db_errors``' HTTPException passthrough returns the specific detail
without a 500.
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.team_membership import TeamMembership

__all__ = [
    "ACCOUNTABILITY_OWNER_FIELDS",
    "validate_accountability_owner",
]

#: The two accountability-owner column/field names on ``pipelines``.
ACCOUNTABILITY_OWNER_FIELDS: tuple[str, str] = ("business_owner_id", "reliability_owner_id")


def _reject(field: str, owner_account_id: uuid.UUID, reason: str) -> NoReturn:
    """Raise the specific 422 for an ineligible owner (fail closed)."""
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"{field}: account {owner_account_id} {reason}",
    )


async def validate_accountability_owner(
    session: AsyncSession,
    *,
    owner_account_id: uuid.UUID | None,
    field: str,
    org_id: uuid.UUID,
    visibility: str,
    owner_team_id: uuid.UUID | None,
) -> None:
    """Validate one accountability-owner assignment. ``None`` is a no-op (clear).

    Args:
        session: session with the caller's RLS org context already applied.
        owner_account_id: the candidate owner; ``None`` means "no owner" and
            is accepted without queries (clearing a field is always allowed).
        field: the column name being assigned (used in the error detail so
            the caller knows WHICH owner was rejected).
        org_id: the pipeline's organisation.
        visibility: the pipeline's EFFECTIVE visibility (post-update value).
        owner_team_id: the pipeline's EFFECTIVE owner team (post-update value).
    """
    if owner_account_id is None:
        return

    account_row = (
        await session.execute(
            select(Account.id, Account.active).where(Account.id == owner_account_id),
        )
    ).first()
    if account_row is None:
        _reject(field, owner_account_id, "does not exist")
    if not account_row[1]:
        _reject(field, owner_account_id, "is deactivated")

    membership = (
        await session.execute(
            select(OrgMembership.deactivated_at).where(
                OrgMembership.account_id == owner_account_id,
                OrgMembership.organisation_id == org_id,
            ),
        )
    ).first()
    if membership is None:
        _reject(field, owner_account_id, "is not a member of this organisation")
    if membership[0] is not None:
        _reject(field, owner_account_id, "has a deactivated membership in this organisation")

    if visibility == "team":
        # Fail closed: a team-visible pipeline must have an owner team (the
        # ck_pipelines_team_owner CHECK enforces this at the DB layer, but
        # internal callers on non-Postgres backends bypass CHECKs — reject
        # here too rather than skip the team gate).
        if owner_team_id is None:
            _reject(field, owner_account_id, "cannot be assigned: visibility 'team' requires an owner team")
        team_row = (
            await session.execute(
                select(TeamMembership.id).where(
                    TeamMembership.team_id == owner_team_id,
                    TeamMembership.account_id == owner_account_id,
                ),
            )
        ).first()
        if team_row is None:
            _reject(field, owner_account_id, "is not a member of the pipeline's owner team")

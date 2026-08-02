"""Scope rls_team_isolation policies to the current organisation.

Revision ID: 0033_fix_rls_team_isolation_org_scope
Revises: 0031_add_saq_routing_columns
Create Date: 2026-08-02

The team-scoped RLS policies (``rls_team_isolation``) created by 0002 and 0003
were missing the organisation condition:

    USING (
      (visibility = 'org' OR visibility IS NULL)
      OR (owner_team_id IS NULL)
      OR (owner_team_id IN (SELECT team_id FROM team_memberships ...))
      OR (nullif(current_setting('app.org_role', true), '') = 'admin')
    )

Postgres ORs every policy on a table, so ``rls_team_isolation`` grants access
to *any* row whose ``visibility = 'org'`` (or with a NULL owner team) regardless
of ``organisation_id`` — a cross-tenant data leak across
``pipelines``, ``stages``, ``connector_instances``, ``model_backends``,
``environment_profiles`` and ``library_primitives``.

This migration rewrites the policy so the team-visibility rules only apply
*within* the caller's organisation, matching the intent of
``rls_org_isolation``.

Fully revertible: downgrade restores the original org-agnostic policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_fix_rls_team_isolation_org_scope"
down_revision: str | None = "0031_add_saq_routing_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that carry the org-agnostic rls_team_isolation policy from 0002/0003.
_TEAM_SCOPED: tuple[str, ...] = (
    "library_primitives",
    "pipelines",
    "stages",
    "connector_instances",
    "model_backends",
    "environment_profiles",
)


def _team_policy_using() -> str:
    strict_org = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    return (
        f"({strict_org}) AND ("  # noqa: S608  # nosec B608 - policy body built from constant fragments
        "(visibility = 'org' OR visibility IS NULL) "
        "OR (owner_team_id IS NULL) "
        "OR (owner_team_id IN ("
        "SELECT team_id FROM team_memberships "
        "WHERE account_id = nullif(current_setting('app.user_id', true), '')::uuid"
        ")) "
        "OR (nullif(current_setting('app.org_role', true), '') = 'admin')"
        ")"
    )


def upgrade() -> None:
    using = _team_policy_using()
    for table in _TEAM_SCOPED:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"'))
        op.execute(sa.text(f'CREATE POLICY rls_team_isolation ON "{table}" USING ({using})'))


def downgrade() -> None:
    team = (
        "(visibility = 'org' OR visibility IS NULL) "
        "OR (owner_team_id IS NULL) "
        "OR (owner_team_id IN ("
        "SELECT team_id FROM team_memberships "
        "WHERE account_id = nullif(current_setting('app.user_id', true), '')::uuid"
        ")) "
        "OR (nullif(current_setting('app.org_role', true), '') = 'admin')"
    )
    for table in _TEAM_SCOPED:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"'))
        op.execute(sa.text(f'CREATE POLICY rls_team_isolation ON "{table}" USING ({team})'))

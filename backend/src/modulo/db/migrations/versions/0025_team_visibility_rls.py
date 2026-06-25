"""Add team-scoped RLS policies for visibility-controlled tables.

Creates rls_team_isolation policy on tables that have both owner_team_id
and visibility columns: pipelines, stages, connector_instances,
model_backends, library_primitives.

The policy allows:
- Rows with visibility = 'org' or NULL (org-wide visibility)
- Rows with owner_team_id IS NULL (no team assignment)
- Rows whose owner_team_id the current user belongs to
- All rows if the user has org_role = 'admin'

Revision ID: 0025_team_visibility_rls
Revises: 0024_audit_append_only
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_team_visibility_rls"
down_revision: str | Sequence[str] | None = "0024_audit_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TEAM_SCOPED_TABLES = (
    "pipelines",
    "stages",
    "connector_instances",
    "model_backends",
    "library_primitives",
)

_TEAM_POLICY_USING = (
    "(visibility = 'org' OR visibility IS NULL) "
    "OR (owner_team_id IS NULL) "
    "OR (owner_team_id IN ("
    "SELECT team_id FROM team_memberships "
    "WHERE user_id = nullif(current_setting('app.user_id', true), '')::uuid"
    ")) "
    "OR (nullif(current_setting('app.org_role', true), '') = 'admin')"
)


def upgrade() -> None:
    for table in _TEAM_SCOPED_TABLES:
        op.execute(
            sa.text(
                f'CREATE POLICY rls_team_isolation ON "{table}" '
                f"USING ({_TEAM_POLICY_USING})"
            )
        )


def downgrade() -> None:
    for table in reversed(_TEAM_SCOPED_TABLES):
        op.execute(
            sa.text(f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"')
        )

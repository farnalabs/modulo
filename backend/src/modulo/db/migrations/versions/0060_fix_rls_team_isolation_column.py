"""Fix rls_team_isolation policy after column rename.

Migration 0025 created rls_team_isolation policy on 5 tables referencing
team_memberships.user_id. Migration 0045 renamed that column to
account_id, breaking the policy for all non-admin queries.

Drops and recreates the policy with the correct column name.

Revision ID: 0060_fix_rls_team_isolation_column
Revises: 0059_feedback_annotation
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060_fix_rls_team_isolation_column"
down_revision: str | Sequence[str] | None = "0059_feedback_annotation"
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
    "WHERE account_id = nullif(current_setting('app.user_id', true), '')::uuid"
    ")) "
    "OR (nullif(current_setting('app.org_role', true), '') = 'admin')"
)


def upgrade() -> None:
    for table in _TEAM_SCOPED_TABLES:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"'))
        op.execute(sa.text(f'CREATE POLICY rls_team_isolation ON "{table}" USING ({_TEAM_POLICY_USING})'))


def downgrade() -> None:
    for table in reversed(_TEAM_SCOPED_TABLES):
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"'))

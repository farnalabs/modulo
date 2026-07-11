"""Fix RLS policy on remy_skills to allow NULL-organisation_id rows.

``remy_skills`` has a dual-ownership model via the ``ck_remy_skills_owner``
check constraint: rows belong either to an organisation (``organisation_id`` set,
``user_id`` NULL) or to a user (``organisation_id`` NULL, ``user_id`` set).

Migration 0088 applied a strict RLS policy that requires ``organisation_id`` to
match the current org context. This hides user-owned rows (which have
``organisation_id IS NULL``) from ALL queries, including the ``/me/remy/skills``
endpoint that fetches the current user's personal skills.

This migration replaces the strict policy with a relaxed one that also permits
rows where ``organisation_id IS NULL``. The application layer (`get_user_skills`
in ``admin_remy.py``) separately filters by ``user_id`` for user-scoped access,
so there is no cross-user visibility from this change.

Revision ID: 0089_fix_remy_skills_rls_policy
Revises: 0088_rls_missing_policies
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0089_fix_remy_skills_rls_policy"
down_revision: str | Sequence[str] | None = "0088_rls_missing_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "remy_skills"

_OLD_POLICY = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
_NEW_POLICY = (
    "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid OR organisation_id IS NULL"
)


def upgrade() -> None:
    op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{_TABLE}"'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{_TABLE}" USING ({_NEW_POLICY})'))


def downgrade() -> None:
    op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{_TABLE}"'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{_TABLE}" USING ({_OLD_POLICY})'))

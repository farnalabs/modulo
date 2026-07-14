"""Enable RLS on pipeline_folders.

Revision ID: 0008_rls_pipeline_folders
Revises: 0007_pipeline_folders
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_rls_pipeline_folders"
down_revision: str | None = "0007_pipeline_folders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRICT_RLS: tuple[str, ...] = ("pipeline_folders",)


def upgrade() -> None:
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    for table in _STRICT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({strict})'))


def downgrade() -> None:
    for table in _STRICT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

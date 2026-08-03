"""Enable RLS on snapshot_schema_pins.

Revision ID: 0023_rls_snapshot_schema_pins
Revises: 0022_create_snapshot_schema_pins_table
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_rls_snapshot_schema_pins"
down_revision: str | None = "0022_create_snapshot_schema_pins_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRICT_RLS: tuple[str, ...] = ("snapshot_schema_pins",)


def upgrade() -> None:
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    for table in _STRICT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({strict})'))


def downgrade() -> None:
    for table in _STRICT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

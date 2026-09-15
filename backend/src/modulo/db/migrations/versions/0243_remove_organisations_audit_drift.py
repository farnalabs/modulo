"""Remove organisations audit columns that are not declared on the ORM.

Migrations 0233 (``updated_at``/``updated_by``/``deleted_by``) added columns to
``organisations`` that the ``Organisation`` ORM model does NOT declare — see
src/modulo/db/models/organisation.py. Those columns therefore introduced schema
drift vs the ORM. Note that ``created_by`` IS declared on the ORM as a FK
(``fk_organisations_created_by``, added by 0236 and kept in step with the ORM by
PR #553) — so it is NOT drift and is deliberately left in place.

Migration 0239 was written to drop this drift but was changed to a no-op on the
mistaken belief (PR #553) that the ORM had been aligned to declare the columns.
It had not, so the columns remained. Migration 0240_reinstate_organisations
_audit_columns then re-added them (idempotently, guarded by information_schema
checks) — reintroducing the very drift the integration test
test_migrated_schema_matches_orm_metadata forbids.

This migration performs the correction 0239 should have: it removes the drift
columns (but not the legitimate ``created_by`` FK) so the migrated schema
matches the ORM metadata. It chains off 0242_org_mint_budget_usage (main's head,
FAR-795) and is idempotent — it is a no-op if the columns are already absent.

Revision ID: 0243_remove_organisations_audit_drift
Revises: 0242_org_mint_budget_usage
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0243_remove_organisations_audit_drift"
down_revision = "0242_org_mint_budget_usage"
branch_labels = None
depends_on = None


_DRIFT_COLUMNS = ("updated_at", "updated_by", "deleted_by")


def _column_names() -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns("organisations")}


def upgrade() -> None:
    columns = _column_names()
    for col in _DRIFT_COLUMNS:
        if col in columns:
            op.drop_column("organisations", col)


def downgrade() -> None:
    columns = _column_names()
    if "updated_at" not in columns:
        op.add_column(
            "organisations",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.current_timestamp(),
                onupdate=sa.func.current_timestamp(),
                nullable=False,
            ),
        )
    if "updated_by" not in columns:
        op.add_column("organisations", sa.Column("updated_by", sa.Uuid(), nullable=True))
    if "deleted_by" not in columns:
        op.add_column("organisations", sa.Column("deleted_by", sa.Uuid(), nullable=True))

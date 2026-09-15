"""Remove organisations audit columns/FK that are not declared on the ORM.

Migrations 0233 (``updated_at``/``updated_by``/``deleted_by``) and 0236
(``fk_organisations_created_by``) added columns/constraints to
``organisations`` that the ``Organisation`` ORM model does NOT declare — see
src/modulo/db/models/organisation.py: only ``created_by`` exists, and it is
deliberately NOT a FK ("This is deliberately not an FK: the first organisation
must exist before its first user."). Those migrations therefore introduced
schema drift vs the ORM.

Migration 0239 was originally written to drop this drift but was changed to a
no-op on the mistaken belief (PR #553) that the ORM had been aligned to declare
the columns. It had not, so the columns remained. Migration 0240 was then
written to "reinstate" them, which double-adds ``updated_at`` and crashes on a
fresh database (``DuplicateColumn``) and, on a live database, reintroduces the
very drift the integration test test_migrated_schema_matches_orm_metadata
forbids.

This migration performs the correction 0239 should have: it removes the drift
columns/FK so the migrated schema matches the ORM metadata. It is idempotent —
it is a no-op if the columns/FK are already absent (e.g. on a database where a
prior run of 0240 already removed them). 0240 never completed successfully
anywhere (it always hit DuplicateColumn at upgrade), so correcting it here is
safe on every environment.

Revision ID: 0240_reinstate_organisations_audit_columns
Revises: 0239_revert_organisations_audit_drift
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0240_reinstate_organisations_audit_columns"
down_revision = "0239_revert_organisations_audit_drift"

_DRIFT_COLUMNS = ("updated_at", "updated_by", "deleted_by")
_FK_NAME = "fk_organisations_created_by"


def _column_names() -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns("organisations")}


def _fk_names() -> set[str]:
    return {fk["name"] for fk in inspect(op.get_bind()).get_foreign_keys("organisations")}


def upgrade() -> None:
    columns = _column_names()
    for col in _DRIFT_COLUMNS:
        if col in columns:
            op.drop_column("organisations", col)
    if _FK_NAME in _fk_names():
        op.drop_constraint(_FK_NAME, "organisations", type_="foreignkey")


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
    if _FK_NAME not in _fk_names():
        op.create_foreign_key(
            _FK_NAME,
            "organisations",
            "accounts",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )

"""Drop the fk_organisations_created_by FK to match the non-FK Organisation.created_by ORM.

The Organisation model declares ``created_by`` as a plain nullable UUID column — it is
deliberately NOT a foreign key: the first organisation must exist before its first user,
so a FK to accounts.id would block bootstrap ordering. The database previously carried the
``fk_organisations_created_by`` FK (added by 0236_add_organisations_constraints), which is
schema drift vs the ORM. Migration 0243_remove_organisations_audit_drift (FAR-811) keeps this
FK on the assumption that the ORM declares it, but PR #559 dropped the FK declaration from the
Organisation model, so the FK must be removed to keep the migrated schema in step with the ORM.

This migration chains off 0244_pipeline_stdout_retention_config (the current head) and is
idempotent — it is a no-op if the FK is already absent.

Revision ID: 0245_drop_organisations_created_by_fk
Revises: 0244_pipeline_stdout_retention_config
Create Date: 2026-09-15
"""

from alembic import op
from sqlalchemy import inspect

revision = "0245_drop_organisations_created_by_fk"
down_revision = "0244_pipeline_stdout_retention_config"
branch_labels = None
depends_on = None


def _fk_exists() -> bool:
    fks = inspect(op.get_bind()).get_foreign_keys("organisations")
    return any(fk.get("name") == "fk_organisations_created_by" for fk in fks)


def upgrade() -> None:
    if _fk_exists():
        op.drop_constraint("fk_organisations_created_by", "organisations", type_="foreignkey")


def downgrade() -> None:
    if not _fk_exists():
        op.create_foreign_key(
            "fk_organisations_created_by",
            "organisations",
            "accounts",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )

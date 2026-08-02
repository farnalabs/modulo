"""Reconcile ORM <-> migration drift found by the schema-parity integration test.

Revision ID: 0035_reconcile_schema_drift
Revises: 0034_api_key_lookup_org_function
Create Date: 2026-08-02

The ``test_migrated_schema_matches_orm_metadata`` canary asserts the migrated
DB schema exactly matches the SQLAlchemy ORM metadata. It had accumulated 42
drift items. This migration fixes the GENUINE bugs — the items that would
break ORM inserts at runtime or are dead schema — while leaving benign
migration-managed artifacts (perf indexes, comments) to the test's known-drift
allowlist:

1. ``run_number_counters`` — orphaned table. Migration 0025 created it for the
   atomic counter optimisation (PR #296), but the code was reverted back to
   ``SELECT MAX(run_number)`` (commit 1045aa98f) without dropping the table.
   It has no ORM model and no RLS policy, so it also failed
   ``test_rls_policies_exist_on_all_org_scoped_tables``. Dropping it removes
   dead schema.

2. ``snapshot_schema_pins.created_at`` / ``updated_at`` — the ORM model
   inherits ``TimestampMixin`` (via ``OrgScoped``), so every ORM insert emits a
   RETURNING clause referencing these columns. Migration 0022 created the table
   without them, so ``pipeline_snapshot.create_snapshot_from_live_graph`` and
   ``clone_pipeline`` would crash with "column does not exist".

3. ``oauth_consent_states.created_at`` — same class of bug: the ORM model
   declares ``created_at`` with a server default, and ``auth.oauth`` inserts
   ``OAuthConsentState`` rows. Migration 0032 omitted the column.

4. ``ix_snapshot_schema_pins_organisation_id`` — the ORM ``OrgScoped`` base
   declares ``index=True`` on ``organisation_id``; migration 0022 created the
   table without the index.

All additions are idempotent-safe against re-run on already-patched schemas
where possible (the columns are additive; the table drop is the only removal,
and it targets a table nothing in ``src/`` references).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0035_reconcile_schema_drift"
down_revision: str | sa.Sequence[str] | None = (
    "0034_api_key_lookup_org_function",
    "0032_oauth_consent_pkce",
)
branch_labels: str | sa.Sequence[str] | None = None
depends_on: str | sa.Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("run_number_counters")

    op.add_column(
        "snapshot_schema_pins",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.add_column(
        "snapshot_schema_pins",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_snapshot_schema_pins_organisation_id",
        "snapshot_schema_pins",
        ["organisation_id"],
    )

    op.add_column(
        "oauth_consent_states",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("oauth_consent_states", "created_at")
    op.drop_index("ix_snapshot_schema_pins_organisation_id", table_name="snapshot_schema_pins")
    op.drop_column("snapshot_schema_pins", "updated_at")
    op.drop_column("snapshot_schema_pins", "created_at")
    op.create_table(
        "run_number_counters",
        sa.Column("organisation_id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("next_run_number", sa.Integer(), nullable=False, server_default="1"),
    )

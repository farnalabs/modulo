"""Convert full unique constraints to partial unique indexes for soft-delete keys.

Revision ID: 0127_soft_delete_partial_unique
Revises: 0126_human_set_eval_type
Create Date: 2026-08-23

Several OrgScoped + SoftDeleteMixin tables declared a *full* UNIQUE
constraint on their natural/business key (organisation_id + name, etc.).
Because every read filters on ``deleted_at IS NULL`` under RLS, a soft-deleted
row still occupies the unique slot, which blocks re-creating an otherwise
valid active row with the same key. This is a data-correctness bug: after
soft-deleting e.g. a node category, you cannot re-create one with the same
name in the same org.

We drop the full unique constraints and replace them with partial unique
indexes ``WHERE deleted_at IS NULL``, mirroring the pattern already used by
``teams`` (uq_teams_organisation_name) and ``cost_components``. Only live
rows participate in the uniqueness check; soft-deleted rows are excluded.

Postgres-only concern: ``postgresql_where`` is ignored by the deprecated
SQLite / MariaDB backends, where the index is created without the predicate.
"""

from alembic import op
from sqlalchemy import text

revision = "0127_soft_delete_partial_unique"
down_revision = "0126_human_set_eval_type"
branch_labels = None
depends_on = None

# (table, old_constraint_name, new_partial_index_name, columns)
_PARTIAL_UNIQUES = (
    ("node_categories", "uq_node_categories_org_name", "uq_node_categories_org_name", ["organisation_id", "name"]),
    (
        "parameter_schemas",
        "uq_parameter_schemas_org_name",
        "uq_parameter_schemas_org_name",
        ["organisation_id", "name"],
    ),
    (
        "parameter_sets",
        "uq_parameter_sets_schema_name",
        "uq_parameter_sets_schema_name",
        ["parameter_schema_id", "name"],
    ),
    (
        "error_forwarder_configs",
        "uq_org_forwarder_type",
        "uq_org_forwarder_type",
        ["organisation_id", "forwarder_type"],
    ),
    (
        "library_primitives",
        "uq_library_primitive_version",
        "uq_library_primitive_version",
        ["organisation_id", "source", "slug", "version"],
    ),
)


def upgrade():
    for table, old_name, new_name, columns in _PARTIAL_UNIQUES:
        op.drop_constraint(old_name, table, type_="unique")
        op.create_index(
            new_name,
            table,
            columns,
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        )


def downgrade():
    bind = op.get_bind()
    for table, old_name, new_name, columns in _PARTIAL_UNIQUES:
        op.drop_index(new_name, table_name=table, postgresql_drop_where=text("deleted_at IS NULL"))
        # De-duplicate rows that share a business key before restoring the full
        # unique constraint. After upgrade, the partial unique index permits an
        # active row and one or more soft-deleted rows to coexist on the same key
        # (the documented "soft-delete then re-create" flow). Re-adding the full
        # constraint on that state would raise a unique-violation and abort the
        # downgrade. Keep the active row when one exists, otherwise keep the most
        # recently deleted row, and delete the remaining duplicates.
        key_cols = ", ".join(columns)
        # ``table`` / ``key_cols`` come from the hardcoded ``_PARTIAL_UNIQUES``
        # constant below, never from user input, so the f-string is safe.
        de_dup = (
            f"DELETE FROM {table} WHERE id IN ("  # noqa: S608  # nosec B608
            f"SELECT id FROM ("
            f"SELECT id, ROW_NUMBER() OVER (PARTITION BY {key_cols} "
            f"ORDER BY (deleted_at IS NULL) DESC, deleted_at DESC NULLS LAST) AS rn "
            f"FROM {table}) sub WHERE rn > 1)"
        )
        bind.execute(text(de_dup))
        op.create_unique_constraint(old_name, table, columns)

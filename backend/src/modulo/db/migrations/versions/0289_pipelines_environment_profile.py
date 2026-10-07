"""Add the per-pipeline environment-profile binding column (FAR-1558 slice 1).

Revision ID: 0289_pipelines_environment_profile
Revises: 0288_runs_execution_origin
Create Date: 2026-10-07

FAR-1558 (ratified Option A, per-pipeline binding): ``pipelines`` gains a
nullable reference to the environment profile its ``sandbox_agent`` nodes
dispatch on:

* ``environment_profile_id`` — UUID NULL, FK -> ``environment_profiles.id``,
  ON DELETE SET NULL (deleting a profile UNBINDS the pipeline instead of
  deleting it), plus ``ix_pipelines_environment_profile_id`` and the same-org
  tenant trigger ``trg_pipelines_environment_profile_id_tenant``.

Nullable by design: NULL is the historical default and must stay byte-identical
to today's behaviour — an unbound pipeline resolves the dispatch route
``provider_type="none"`` (the legacy E2B path).

The column is COPIED into the EXISTING ``pipeline_snapshots.environment_profile_id``
at snapshot freeze (``db/crud/pipeline_snapshot.create_snapshot_from_live_graph``);
that snapshot column and its own FK/index/tenant trigger have existed since
0003/0110 and were simply never written, and it is what dispatch reads
(``core/bundled_runner/runner_dispatch``, ``core/pipeline_engine/executor``,
``core/graph_validator``).

Shape mirrors 0258_pipeline_accountability_owners (add_column + FK SET NULL +
index) and 0110's tenant-trigger statement for a ``pipelines`` FK column.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0289_pipelines_environment_profile"
down_revision: str | None = "0288_runs_execution_origin"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "pipelines",
        sa.Column(
            "environment_profile_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_pipelines_environment_profile_id",
        "pipelines",
        "environment_profiles",
        ["environment_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_pipelines_environment_profile_id",
        "pipelines",
        ["environment_profile_id"],
    )
    # Same-org tenant guard: a pipeline may only reference a profile in ITS
    # organisation (RAISE ... ERRCODE 23503). Idempotent DO-block form, the
    # same shape 0110 uses for trg_pipelines_owner_team_id_tenant.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_pipelines_environment_profile_id_tenant') "
        "THEN CREATE TRIGGER trg_pipelines_environment_profile_id_tenant BEFORE INSERT OR UPDATE OF "
        "environment_profile_id, organisation_id ON public.pipelines FOR EACH ROW EXECUTE FUNCTION "
        "public.enforce_same_organisation('environment_profiles', 'environment_profile_id'); END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_pipelines_environment_profile_id_tenant ON public.pipelines;")
    op.drop_index("ix_pipelines_environment_profile_id", table_name="pipelines")
    op.drop_constraint("fk_pipelines_environment_profile_id", "pipelines", type_="foreignkey")
    op.drop_column("pipelines", "environment_profile_id")

"""Add the OAuth client team-boundary column (FAR-1476 slice 1).

Revision ID: 0291_oauth_clients_team_id
Revises: 0290_scheduled_reports_due_scan
Create Date: 2026-10-09

FAR-1476 binds an OAuth client to a team via a nullable ``team_id`` on
``oauth_clients``, mirroring the existing ``org_api_keys.team_id``
credential-scoping pattern:

* ``team_id`` — UUID NULL, FK -> ``teams.id``, ON DELETE SET NULL (deleting a
  team UNBINDS the client instead of deleting it), plus
  ``ix_oauth_clients_team_id`` and the same-org tenant trigger
  ``trg_oauth_clients_team_id_tenant``.

Nullable by design: NULL is the historical default and must stay
byte-identical to today's behaviour — an unbound (org-wide) client resolves
``_ctx_team_id = None`` on the MCP auth legs, so the team-scoped-credential
machinery never constrains it. A non-null value scopes every token issued to
that client to the owning team.

Shape mirrors 0289_pipelines_environment_profile (add_column + FK SET NULL +
index + same-org tenant trigger) and 0108's tenant-trigger statement for the
``org_api_keys`` ``team_id`` column.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0291_oauth_clients_team_id"
down_revision: str | None = "0290_scheduled_reports_due_scan"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_clients",
        sa.Column(
            "team_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_oauth_clients_team_id",
        "oauth_clients",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_oauth_clients_team_id",
        "oauth_clients",
        ["team_id"],
    )
    # Same-org tenant guard: an OAuth client may only reference a team in ITS
    # organisation (RAISE ... ERRCODE 23503). Idempotent DO-block form, the
    # same shape 0108 uses for trg_org_api_keys_team_id_tenant.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_oauth_clients_team_id_tenant') "
        "THEN CREATE TRIGGER trg_oauth_clients_team_id_tenant BEFORE INSERT OR UPDATE OF "
        "team_id, organisation_id ON public.oauth_clients FOR EACH ROW EXECUTE FUNCTION "
        "public.enforce_same_organisation('teams', 'team_id'); END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_oauth_clients_team_id_tenant ON public.oauth_clients;")
    op.drop_index("ix_oauth_clients_team_id", table_name="oauth_clients")
    op.drop_constraint("fk_oauth_clients_team_id", "oauth_clients", type_="foreignkey")
    op.drop_column("oauth_clients", "team_id")

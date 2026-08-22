"""Drop the redundant org-only RLS policy on team-scoped tables (cross-team leak).

Revision ID: 0122_fix_team_rls_policies
Revises: 0121_metrics_staging
Create Date: 2026-08-22

The five team-scoped tables (``pipelines``, ``connector_instances``,
``model_backends``, ``environment_profiles``, ``library_primitives``) carried
TWO permissive RLS policies:

* ``rls_org_isolation`` — ``organisation_id = current_app_org``
* ``rls_team_isolation`` — the org check AND the team-visibility clause
  (``visibility='org' OR owner_team_id IS NULL OR owner_team_id IN
  (my team_memberships) OR org_role='admin'``)

PostgreSQL ORs permissive policies, so the org-only policy alone permitted
every row in the organisation — the team policy was dead weight. A user who was
a member of Team B could read Team A's ``visibility='team'`` rows via the
API's RLS (GET-by-ID and LIST both returned them). The team policy already
includes the org check, so dropping the org-only policy is safe: the sole
remaining gate is the full team-visibility policy.

The app-layer defense-in-depth (``require_team_membership_or_admin`` on the
GET route) shipped in the same change; this migration fixes the root cause at
the database layer.

Postgres-only (RLS policies do not exist on the deprecated MariaDB / SQLite
backends).
"""

from alembic import op

revision = "0122_fix_team_rls_policies"
down_revision = "0121_metrics_staging"
branch_labels = None
depends_on = None

_TEAM_SCOPED_TABLES = (
    "pipelines",
    "connector_instances",
    "model_backends",
    "environment_profiles",
    "library_primitives",
)

_ORG_ONLY_STRICT = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    for table in _TEAM_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON public.{table}")


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    for table in _TEAM_SCOPED_TABLES:
        op.execute(f"CREATE POLICY rls_org_isolation ON public.{table} USING ({_ORG_ONLY_STRICT})")

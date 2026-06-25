"""Enable row-level security on all org-scoped tables.

Revision ID: 0002_rls_policies
Revises: 0001_initial_schema
Create Date: 2026-06-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_rls_policies"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SECURITY NOTE: FORCE ROW LEVEL SECURITY is intentionally absent here.
# PostgreSQL superusers bypass RLS regardless of ENABLE. Adding FORCE would
# require the app to connect as a dedicated non-superuser role (e.g. modulo_app)
# which is the correct production setup and is enforced at the infrastructure
# layer. DO NOT connect to production Postgres as a superuser — org isolation
# would be silently bypassed. See docs/ops/database.md for role configuration.

# Every table with an organisation_id column receives an isolation policy.
# The organisations table itself is intentionally excluded — it is the root
# tenant entity and is accessed before org context is established.
_ORG_SCOPED_TABLES = (
    "org_daily_run_counts",
    "users",
    "audit_events",
    "schemas",
    "teams",
    "connector_instances",
    "library_primitives",
    "model_backends",
    "org_api_keys",
    "schema_versions",
    "stages",
    "agents",
    "pipelines",
    "pipeline_edges",
    "pipeline_snapshots",
    "triggers",
    "runs",
    "webhook_dedup_hashes",
    "hitl_claims",
    "notification_delivery_log",
    "trigger_events",
    "webhook_payloads",
)

# nullif(…, '') converts the empty-string sentinel (used on pool checkout reset)
# to NULL so that organisation_id = NULL is always false — no rows visible when
# the org context has not been set.
_POLICY_USING = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"


def upgrade() -> None:
    for table in _ORG_SCOPED_TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({_POLICY_USING})'))


def downgrade() -> None:
    for table in reversed(_ORG_SCOPED_TABLES):
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

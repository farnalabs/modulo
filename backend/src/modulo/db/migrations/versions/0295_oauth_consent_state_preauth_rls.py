"""FAR-1476: give ``oauth_consent_states`` the NULL-context RLS arm.

The anonymous pre-auth consent-context endpoint
(``GET /api/v1/mcp/oauth/consent/context?state=...``) must read a pending
``oauth_consent_states`` row BEFORE it knows which organisation it belongs to —
the organisation is a column OF that row. The table's ``rls_org_isolation``
policy (0108) had only the strict ``organisation_id = NULLIF(current_setting(
'app.organisation_id', true), '')`` arm, so under the runtime ``modulo_app``
role (non-owner, RLS-filtered) an unscoped read returned zero rows and the
endpoint would 404 every request in production while staying green against the
unit tier's mocked sessions.

The sibling table ``oauth_clients`` was deliberately given the NULL-context arm
by the same 0108 migration for the analogous pre-auth read in
``/mcp/oauth/authorize`` (the browser presents only a ``client_id`` there). This
migration brings ``oauth_consent_states`` onto that same contract.

Exposure is bounded and grants nothing by itself: a consent state is
single-use, TTL-bounded (~15 min), and code minting still requires the
authenticated approve POST whose ``consume`` UPDATE runs under the approver's
own org context.

Postgres-only policy DDL (SQLite/ORM-created schemas never materialise RLS
policies; the unit/BDD tiers mock the session). Downgrade restores the strict
0108 policy.

Revision ID: 0295_oauth_consent_state_preauth_rls
Revises: 0294_eval_results_org_fk
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op

revision: str = "0295_oauth_consent_state_preauth_rls"
down_revision: str | None = "0294_eval_results_org_fk"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Static DDL literals only — the FAR-915 ``migration-fstring-sql`` semgrep rule
# forbids f-string identifier interpolation in migrations.
_DROP_POLICY_SQL = "DROP POLICY IF EXISTS rls_org_isolation ON public.oauth_consent_states"
_CREATE_POLICY_STRICT_SQL = (
    "CREATE POLICY rls_org_isolation ON public.oauth_consent_states "
    "USING ((organisation_id = "
    "(NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid))"
)
_CREATE_POLICY_PREAUTH_SQL = (
    "CREATE POLICY rls_org_isolation ON public.oauth_consent_states "
    "USING ((organisation_id = "
    "(NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) "
    "OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL))"
)


def upgrade() -> None:
    op.execute(_DROP_POLICY_SQL)
    op.execute(_CREATE_POLICY_PREAUTH_SQL)


def downgrade() -> None:
    op.execute(_DROP_POLICY_SQL)
    op.execute(_CREATE_POLICY_STRICT_SQL)

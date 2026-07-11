"""Enable row-level security on org-scoped tables added after migration 0002.

Migration 0002 enabled RLS + a ``rls_org_isolation`` policy on the 22 tables
that existed at the time, and 0045 covered ``saved_views``. Every org-scoped
table added since then shipped WITHOUT an RLS policy. This migration closes
that gap for all 35 uncovered tables that carry an ``organisation_id`` column.

Two predicate variants are used, informed by a parallel audit of the auth flows:

1. Strict (31 tables) — identical to 0002:
       organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid
   No rows are visible unless a matching org context is set. Used for every
   table that is only ever queried WITH an org context established.

2. NULL-context carve-out (4 identity-bootstrap tables):
       organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid
       OR nullif(current_setting('app.organisation_id', true), '') IS NULL
   These tables are queried BEFORE an org context is established (login,
   refresh-token rotation, OAuth code/token exchange, membership lookup). A
   strict policy would return zero rows during those flows and break auth, so
   the policy also permits access when NO org context is set. Once a context IS
   set, isolation is enforced normally.

Per-table decisions:
- token_families            → NULL-context carve-out. Refresh-token rotation
  happens during the auth lifecycle before an org context exists.
- oauth_token_families       → NULL-context carve-out. OAuth refresh-token
  family lookup runs pre-context.
- oauth_authorization_codes  → NULL-context carve-out. OAuth code exchange
  runs pre-context.
- org_memberships            → NULL-context carve-out. Membership lookup during
  login / org selection happens before an org is chosen.
- audit_chain_heads, chat_messages, chat_sessions, composite_templates,
  environment_profiles, error_events, error_forwarder_configs, error_groups,
  error_notification_rules, eval_definitions, eval_results, feedback_records,
  mcp_setup_tokens, node_categories, node_observations, nodes,
  notification_endpoints, notifications, oauth_clients, primitive_abuse_reports,
  primitive_ratings, publishers, remy_context_sources, remy_skills,
  scheduled_reports, secrets, spend_anomalies, sso_providers, team_memberships,
  variant_groups, workspace_leases → strict. All queried only WITH an org
  context; no carve-out. (remy_skills / remy_context_sources permit NULL-org,
  user-owned rows at the application layer via their owner check constraints,
  but the RLS policy stays strict — user-scoped access is enforced in code,
  not via the empty-org-context carve-out.)

SECURITY NOTE (carried over from 0002): FORCE ROW LEVEL SECURITY is
intentionally absent. PostgreSQL superusers and table owners bypass ENABLEd
RLS; enforcing isolation requires connecting as a dedicated non-superuser,
non-owner role — an infrastructure-layer concern. See docs/ops/database.md.

Revision ID: 0088_rls_missing_policies
Revises: 0087_environment_profiles
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0088_rls_missing_policies"
down_revision: str | Sequence[str] | None = "0087_environment_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Org-scoped tables that take the strict predicate (queried only WITH an org
# context). See module docstring for the full rationale.
_ORG_SCOPED_TABLES = (
    "audit_chain_heads",
    "chat_messages",
    "chat_sessions",
    "composite_templates",
    "environment_profiles",
    "error_events",
    "error_forwarder_configs",
    "error_groups",
    "error_notification_rules",
    "eval_definitions",
    "eval_results",
    "feedback_records",
    "mcp_setup_tokens",
    "node_categories",
    "node_observations",
    "nodes",
    "notification_endpoints",
    "notifications",
    "oauth_clients",
    "primitive_abuse_reports",
    "primitive_ratings",
    "publishers",
    "remy_context_sources",
    "remy_skills",
    "scheduled_reports",
    "secrets",
    "spend_anomalies",
    "sso_providers",
    "team_memberships",
    "variant_groups",
    "workspace_leases",
)

# Identity-bootstrap tables that are queried BEFORE an org context is set.
# Their policy additionally permits access when no org context is present.
_NULL_CONTEXT_TABLES = (
    "token_families",
    "oauth_token_families",
    "oauth_authorization_codes",
    "org_memberships",
)

# nullif(…, '') converts the empty-string sentinel (used on pool checkout reset)
# to NULL so that organisation_id = NULL is always false — no rows visible when
# the org context has not been set.
_POLICY_USING = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
# Adds a carve-out that lets identity-bootstrap queries run before an org
# context exists: when the setting is empty/unset, all rows are visible.
_NULL_CONTEXT_USING = f"{_POLICY_USING} OR nullif(current_setting('app.organisation_id', true), '') IS NULL"


def upgrade() -> None:
    for table in _ORG_SCOPED_TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({_POLICY_USING})'))
    for table in _NULL_CONTEXT_TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({_NULL_CONTEXT_USING})'))


def downgrade() -> None:
    for table in reversed(_NULL_CONTEXT_TABLES):
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
    for table in reversed(_ORG_SCOPED_TABLES):
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

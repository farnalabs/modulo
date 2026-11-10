"""Drop the redundant org-only RLS policy on team-scoped tables (cross-team leak)
and add the execution-context escape hatch.

Revision ID: 0124_fix_team_rls_policies
Revises: 0123_relax_registry_signature_check
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

Execution-context escape hatch
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The background execution layer (executor, cron, dispatch, recovery, cost
controller, housekeeping) reads these team-scoped tables with ONLY org scope —
``set_rls_org`` without ``set_rls_user_context``. With empty
``app.user_id``/``app.org_role``, the team policy's membership clause matches
nothing, so team-private rows (``visibility='team'``, ``owner_team_id`` set)
became invisible to background reads after the org-only policy was dropped
(e.g. a team-private pipeline fails at run start with ``NoResultFound``).

This migration therefore re-creates ``rls_team_isolation`` on all five tables
with an OR'd ``app.execution_context`` clause inside the org-gated AND group:

* Background machinery sets ``app.execution_context='true'`` (via
  ``set_rls_execution_context`` in ``db/rls.py``) so internal reads see all
  org rows while still being org-scoped.
* User-facing sessions never set it, so user-facing team isolation is
  preserved — the org check remains an AND gate, so the escape hatch can never
  leak rows across organisations.

The downgrade restores the ORIGINAL policy text (without the execution-context
clause) and re-adds the org-only policy.

Postgres-only (RLS policies do not exist on the deprecated MariaDB / SQLite
backends).
"""

from alembic import op

revision = "0124_fix_team_rls_policies"
down_revision = "0123_relax_registry_signature_check"
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

# Original rls_team_isolation USING expression (verbatim from 0109/0110):
# org check AND (visibility='org' OR visibility IS NULL OR owner_team_id IS NULL
# OR owner_team_id IN (my team_memberships) OR org_role='admin').
_TEAM_POLICY_ORIGINAL = (
    "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)"
    " AND (((visibility)::text = 'org'::text)"
    " OR (visibility IS NULL)"
    " OR (owner_team_id IS NULL)"
    " OR (owner_team_id IN ( SELECT team_memberships.team_id FROM public.team_memberships"
    " WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)))"
    " OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text)))"
)

# Same policy with the execution-context escape hatch OR'd inside the
# org-gated AND group. The org check remains an AND gate, so the hatch only
# widens the team-visibility clause within the organisation — it can never
# leak rows across organisations.
_EXEC_CONTEXT_CLAUSE = "(NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text)"
_TEAM_POLICY_EXEC_CONTEXT = _TEAM_POLICY_ORIGINAL.replace(
    "= 'admin'::text)))",
    "= 'admin'::text) OR " + _EXEC_CONTEXT_CLAUSE + "))",
)


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    for table in _TEAM_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON public.{table}")
        # Re-create the team policy so background execution can read
        # team-private rows with org scope only.
        op.execute(f"DROP POLICY IF EXISTS rls_team_isolation ON public.{table}")
        op.execute(f"CREATE POLICY rls_team_isolation ON public.{table} USING ({_TEAM_POLICY_EXEC_CONTEXT})")


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    for table in _TEAM_SCOPED_TABLES:
        # Restore the ORIGINAL team policy (without the execution-context clause).
        op.execute(f"DROP POLICY IF EXISTS rls_team_isolation ON public.{table}")
        op.execute(f"CREATE POLICY rls_team_isolation ON public.{table} USING ({_TEAM_POLICY_ORIGINAL})")
        op.execute(f"CREATE POLICY rls_org_isolation ON public.{table} USING ({_ORG_ONLY_STRICT})")

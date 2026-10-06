"""Add DB team RLS to the last neither-layer tables (FAR-1514).

Revision ID: 0286_team_rls_lifecycle_evals
Revises: 0285_system_audit_events
Create Date: 2026-10-06

``lifecycle_maps``, ``eval_datasets`` and ``eval_suites`` all carry
``visibility`` + ``owner_team_id`` (and the ``visibility IN ('org','team')`` /
``visibility = 'org' OR owner_team_id IS NOT NULL`` CHECK constraints) but —
unlike the five tables fixed by 0124 — they had NEITHER enforcement layer:

* the DB layer carried only the org-only ``rls_org_isolation`` policy
  (``0110`` for ``lifecycle_maps``, ``0130``/``0131`` for the eval tables), and
* the request-time ``require_team_membership_or_admin`` gate was wired on the
  eval routes (FAR-947) but NOT on the ``lifecycle_maps`` routes.

Postgres ORs permissive policies, so an org-only policy sitting alongside a
team policy makes the team policy dead weight — the cross-team leak 0124
fixed. This migration therefore follows 0124 exactly: DROP the org-only
policy and create ``rls_team_isolation`` carrying the full visibility matrix
plus the execution-context escape hatch.

Policy body (``_TEAM_POLICY_EXEC_CONTEXT``) is verbatim 0124's:

    (organisation_id = current_app_org)
    AND (  visibility = 'org'
        OR visibility IS NULL
        OR owner_team_id IS NULL
        OR owner_team_id IN (my team_memberships)
        OR app.org_role = 'admin'
        OR app.execution_context = 'true')

The org check stays an AND gate, so the escape hatch only widens the
team-visibility clause WITHIN the organisation — it can never leak rows across
organisations.

Why the execution-context clause matters here: the background execution layer
(executor, cron, SAQ suite-run dispatch, housekeeping, seed) reads these tables
with org scope only (``set_rls_org`` without ``set_rls_user_context``). With an
empty ``app.user_id``/``app.org_role`` the membership clause would match
nothing, so team-private rows would become invisible to background reads (e.g.
a scheduled suite run on a team-private ``eval_suite`` failing with
``SuiteRun ... not found``). Background machinery sets
``app.execution_context='true'`` (``set_rls_execution_context``) and sees all
org rows; user-facing sessions never set it, so team isolation is preserved.

Data safety: policy DDL only — no rows are read, written or rewritten. All
statements are existence-guarded (``DROP POLICY IF EXISTS`` followed by
``CREATE POLICY``), so re-running the upgrade is a no-op and a partially-run
upgrade completes cleanly.

Postgres-only (RLS policies do not exist on the deprecated MariaDB / SQLite
backends).

Downgrade restores the original state: the org-only policy back on all three
tables, with ``rls_team_isolation`` dropped.
"""

from __future__ import annotations

import re

from alembic import op

revision: str = "0286_team_rls_lifecycle_evals"
down_revision: str | None = "0285_system_audit_events"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# The three neither-layer tables this migration closes out.
_TEAM_SCOPED_TABLES: tuple[str, ...] = (
    "lifecycle_maps",
    "eval_datasets",
    "eval_suites",
)

# FAR-915 convention (see .semgrep/migration-fstring-sql.yml): every
# interpolated identifier passes through this guard, and the f-string SQL is
# built inside a helper so no ``op.execute(f"...")`` appears directly.
_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


def _validate_identifier(name: str) -> str:
    """Guard an interpolated SQL identifier before it reaches the statement."""
    if _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


_ORG_ONLY_POLICY = "organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid"

# Original rls_team_isolation USING expression — verbatim from 0124
# (_TEAM_POLICY_ORIGINAL), itself verbatim from 0109/0110:
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
# org-gated AND group — verbatim 0124 construction.
_EXEC_CONTEXT_CLAUSE = "(NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text)"
_TEAM_POLICY_EXEC_CONTEXT = _TEAM_POLICY_ORIGINAL.replace(
    "= 'admin'::text)))",
    "= 'admin'::text) OR " + _EXEC_CONTEXT_CLAUSE + "))",
)


def _drop_org_only_policy_sql(table: str) -> str:
    """Built through a helper so no direct ``op.execute(f"...")`` appears here."""
    return f"DROP POLICY IF EXISTS rls_org_isolation ON public.{_validate_identifier(table)}"


def _drop_team_policy_sql(table: str) -> str:
    return f"DROP POLICY IF EXISTS rls_team_isolation ON public.{_validate_identifier(table)}"


def _create_team_policy_sql(table: str) -> str:
    return (
        f"CREATE POLICY rls_team_isolation ON public.{_validate_identifier(table)} USING ({_TEAM_POLICY_EXEC_CONTEXT})"
    )


def _create_org_only_policy_sql(table: str) -> str:
    return f"CREATE POLICY rls_org_isolation ON public.{_validate_identifier(table)} USING ({_ORG_ONLY_POLICY})"


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    for table in _TEAM_SCOPED_TABLES:
        # The org-only policy ORs in every org row and would make the team
        # policy dead weight — drop it (0124 pattern).
        op.execute(_drop_org_only_policy_sql(table))
        op.execute(_drop_team_policy_sql(table))
        op.execute(_create_team_policy_sql(table))


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    for table in _TEAM_SCOPED_TABLES:
        op.execute(_drop_team_policy_sql(table))
        op.execute(_drop_org_only_policy_sql(table))
        op.execute(_create_org_only_policy_sql(table))

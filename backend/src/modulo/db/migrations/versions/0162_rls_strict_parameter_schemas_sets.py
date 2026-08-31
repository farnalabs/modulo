"""Make parameter_schemas / parameter_sets RLS strict (fail-closed).

Revision ID: 0162_rls_strict_parameter_schemas_sets
Revises: 0161_accounts_must_change_password
Create Date: 2026-08-27

Renumber note: originally ``0151_rls_strict_parameter_schemas_sets`` chained off
``0150``, but main advanced through ``0151_fix_constraints`` -> ``0152_dismissed_by_user_id_index``
-> ``0153_add_numeric_check_constraints`` -> ``0154_add_web_vital_events_time_index``
-> ``0155_add_hot_query_indexes`` -> ``0156_add_soft_delete_partial_uniques``
-> ``0157_add_numeric_check_constraints`` -> ``0158_sso_provider_id`` -> ``0159_pipeline_retry_compensation``
-> ``0160_run_idempotency_key`` -> ``0161_accounts_must_change_password``, so the
``0151``/``0152`` and later ``0155``/``0156`` numeric prefixes collided with main.
Renumbered to ``0162`` and re-parented onto the real queued-head
``0161_accounts_must_change_password`` (end of PR #2092/#2153) so the graph stays
a single linear chain.

RLS audit: ``parameter_schemas`` and ``parameter_sets`` were OR-ing a *strict*
``rls_org_isolation`` policy with a stacked permissive
``rls_org_isolation_null_context`` policy (``current_setting(...) IS NULL OR
...``). In Postgres, multiple permissive policies are OR'd together, so a
null/empty ``app.organisation_id`` context made the null-context branch TRUE and
opened the whole table to the caller — a classic fail-open. These are NOT
identity-bootstrap tables, so the null-context loophole must be closed.

This drops the ``rls_org_isolation_null_context`` policy and re-creates
``rls_org_isolation`` in the canonical null-safe strict form — a null/empty
context yields ``NULL`` which matches no row, failing closed instead of
raising (the old strict form called ``current_setting`` without the ``true``
flag, which raises when the GUC is unset).

Postgres-only: RLS policies are Postgres-specific; SQLite relies on app-level
tenant filtering.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0162_rls_strict_parameter_schemas_sets"
down_revision: str | None = "0161_accounts_must_change_password"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRICT_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
_OLD_STRICT_SCOPE = "organisation_id = current_setting('app.organisation_id')::uuid"
_NULL_CONTEXT_SCOPE = (
    "current_setting('app.organisation_id', true) IS NULL "
    "OR organisation_id = current_setting('app.organisation_id')::uuid"
)

_TABLES = ("parameter_schemas", "parameter_sets")


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def _upgrade_table(table: str) -> None:
    # Drop the stacked permissive policy AND the old strict policy, then
    # re-create the single canonical null-safe strict policy.
    op.execute(f"DROP POLICY IF EXISTS rls_org_isolation_null_context ON public.{table}")
    op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON public.{table}")
    op.execute(f"CREATE POLICY rls_org_isolation ON public.{table} USING ({_STRICT_SCOPE})")


def _downgrade_table(table: str) -> None:
    # Restore the exact prior state: strict rls_org_isolation + fail-open
    # rls_org_isolation_null_context.
    op.execute(f"DROP POLICY IF EXISTS rls_org_isolation_null_context ON public.{table}")
    op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON public.{table}")
    op.execute(f"CREATE POLICY rls_org_isolation ON public.{table} USING ({_OLD_STRICT_SCOPE})")
    op.execute(f"CREATE POLICY rls_org_isolation_null_context ON public.{table} USING ({_NULL_CONTEXT_SCOPE})")


def upgrade() -> None:
    if not _is_postgres(op.get_bind()):
        return
    for table in _TABLES:
        _upgrade_table(table)


def downgrade() -> None:
    if not _is_postgres(op.get_bind()):
        return
    for table in _TABLES:
        _downgrade_table(table)

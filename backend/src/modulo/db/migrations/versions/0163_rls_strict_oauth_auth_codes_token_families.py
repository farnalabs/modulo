"""Close vestigial fail-open RLS on oauth_authorization_codes / oauth_token_families.

Revision ID: 0163_rls_strict_oauth_auth_codes_token_families
Revises: 0162_rls_strict_parameter_schemas_sets
Create Date: 2026-08-27

Renumber note: originally ``0152_rls_strict_oauth_auth_codes_token_families`` chained
off the RLS parameter-schemas migration, but main advanced through ``0151``-``0158_sso_provider_id``
-> ``0159_pipeline_retry_compensation`` -> ``0160_run_idempotency_key`` ->
``0161_accounts_must_change_password`` -> ``0162_rls_strict_parameter_schemas_sets``,
so this is renumbered to ``0163`` and re-parented onto ``0162_rls_strict_parameter_schemas_sets``
(the end of the queued PR stack).

RLS audit: ``oauth_authorization_codes`` and ``oauth_token_families`` carried a
``rls_org_isolation`` policy with a vestigial fail-open branch::

    (organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid)
    OR (nullif(current_setting('app.organisation_id', true), '') IS NULL)

That ``OR (... IS NULL)`` branch returns TRUE whenever the organisation context
is empty, so a caller with no org context could read every row in these tables.
There is no identity bootstrap read against either table — all reads happen
after ``set_rls_org`` is called — so the null-context branch is dead weight
that must be closed. The policy is re-created strict/fail-closed: a null/empty
context yields ``NULL`` which matches no row.

``oauth_clients`` (genuine bootstrap lookup by globally-unique client_id),
``org_memberships`` and ``token_families`` (genuine bootstrap) are intentionally
LEFT untouched.

Postgres-only: RLS policies are Postgres-specific; SQLite relies on app-level
tenant filtering.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0163_rls_strict_oauth_auth_codes_token_families"
down_revision: str | None = "0162_rls_strict_parameter_schemas_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRICT_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
_FAIL_OPEN_SCOPE = (
    "(organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid) "
    "OR (nullif(current_setting('app.organisation_id', true), '') IS NULL)"
)

_TABLES = ("oauth_authorization_codes", "oauth_token_families")


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres(op.get_bind()):
        return
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON public.{table}")
        op.execute(f"CREATE POLICY rls_org_isolation ON public.{table} USING ({_STRICT_SCOPE})")


def downgrade() -> None:
    if not _is_postgres(op.get_bind()):
        return
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON public.{table}")
        op.execute(f"CREATE POLICY rls_org_isolation ON public.{table} USING ({_FAIL_OPEN_SCOPE})")

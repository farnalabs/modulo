"""SECURITY DEFINER lookup for resolving an org API key's organisation.

Revision ID: 0034_api_key_lookup_org_function
Revises: 0033_fix_rls_team_isolation_org_scope
Create Date: 2026-08-02

The runtime app role (``modulo_app``) is a DML-granted, non-owner role, so it
is subject to RLS on ``org_api_keys``. Resolving a key's organisation from its
lookup prefix therefore cannot use ``SET LOCAL row_security TO OFF`` — that
bypass only applies to superusers/owners/BYPASSRLS and raises
``InsufficientPrivilegeError`` for a regular role. This function is owned by
the migration (admin) role, so it executes with the owner's privileges and
returns the key's organisation without exposing the key hash.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034_api_key_lookup_org_function"
down_revision: str | None = "0033_fix_rls_team_isolation_org_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.lookup_api_key_org(lookup_prefix_value text)
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT organisation_id
            FROM org_api_keys
            WHERE lookup_prefix = lookup_prefix_value
              AND revoked_at IS NULL
            LIMIT 1
        $$
        """
    )
    # The function only reveals the org id for a known key prefix (keys are
    # unguessable), and it never exposes the hashed secret. Grant to PUBLIC so
    # the runtime role name can vary across deployments.
    op.execute("GRANT EXECUTE ON FUNCTION public.lookup_api_key_org(text) TO PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.lookup_api_key_org(text)")

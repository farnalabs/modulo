"""Break-glass columns + caller-bound deactivate_break_glass SECURITY DEFINER (deliverable A).

Revision ID: 0036_break_glass_columns
Revises: merge_break_glass_heads
Create Date: 2026-08-02

Per docs/break-glass-admin-recovery-plan.md v17 (deliverable A):

1. Three nullable columns on ``accounts`` + a CHECK constraint tying them to the
   ``is_break_glass`` flag.
2. The four transferred tables + ``lookup_api_key_org(text)`` are re-owned to
   ``modulo_migrate`` (NOLOGIN, BYPASSRLS, owner of the SECURITY DEFINER so the
   function's cross-org reads/writes work under FORCE RLS).
3. A single caller-bound SECURITY DEFINER ``deactivate_break_glass`` that
   validates its arguments internally, acquires the target orgs' advisory locks
   with the SAME two-int4 MD5 derivation as the app guard (locks.py), enforces
   the per-org last-admin invariant (skipped ONLY for genuinely break-glass-only
   orgs), and gates ``force_last_admin`` on ``session_user='modulo_breakglass'``.
4. ``modulo_app``/``modulo_breakglass`` get EXECUTE on the function; PUBLIC is
   revoked at creation.

The pre-0036 owners are captured into a marker row in a NON-public schema
(``modulo_internal``) BEFORE the first ``OWNER TO`` / ``ALTER FUNCTION OWNER``
so the downgrade can restore them exactly. The marker is created outside the
``public`` schema so it stays invisible to the ORM-schema parity canary
(``compare_metadata`` inspects the public schema only). The roles themselves are
NOT created here — ``bootstrap_role.py`` owns role provisioning (modulo_migrate,
modulo_breakglass, modulo_app must all exist before this migration runs).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_break_glass_columns"
down_revision: str | Sequence[str] | None = "merge_break_glass_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEACTIVATE_BREAK_GLASS_SQL = """
CREATE FUNCTION public.deactivate_break_glass(caller_account_id uuid, target_account_id uuid,
                                              force_last_admin boolean DEFAULT false) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = off
  AS $$
  DECLARE tgt_org RECORD; k1 int4; k2 int4; is_operator bool; is_bg_target bool;
  BEGIN
    is_operator := session_user = 'modulo_breakglass';
    is_bg_target := EXISTS (SELECT 1 FROM public.accounts WHERE id = $2 AND is_break_glass IS TRUE);
    IF $3 AND NOT is_operator THEN
      RAISE EXCEPTION 'force_last_admin requires operator' USING ERRCODE = 'M2010';
    END IF;
    IF NOT (is_operator
         OR EXISTS (SELECT 1 FROM public.org_memberships caller
                    WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'
                      AND EXISTS (SELECT 1 FROM public.org_memberships tgt
                                  WHERE tgt.account_id = $2 AND tgt.organisation_id = caller.organisation_id))
         OR (EXISTS (SELECT 1 FROM public.accounts c WHERE c.id = $1 AND c.is_break_glass IS TRUE AND c.active IS TRUE)
             AND is_bg_target
             AND EXISTS (SELECT 1 FROM public.org_memberships cm JOIN public.org_memberships tm
                         ON tm.organisation_id = cm.organisation_id
                         WHERE cm.account_id = $1 AND tm.account_id = $2))) THEN
      RAISE EXCEPTION 'caller not authorized to deactivate target' USING ERRCODE = 'M2010';
    END IF;

    FOR tgt_org IN SELECT DISTINCT organisation_id FROM public.org_memberships WHERE account_id = $2 AND deactivated_at IS NULL
                  ORDER BY organisation_id LOOP
      SELECT ('x' || substr(md5(tgt_org.organisation_id::text), 1, 8))::bit(32)::int4,
             ('x' || substr(md5(tgt_org.organisation_id::text), 9, 8))::bit(32)::int4
        INTO k1, k2;
      PERFORM pg_advisory_xact_lock(k1, k2);
    END LOOP;

    FOR tgt_org IN SELECT DISTINCT organisation_id FROM public.org_memberships WHERE account_id = $2 AND deactivated_at IS NULL LOOP
      IF NOT $3
         AND (SELECT count(*) FROM public.org_memberships om
              JOIN public.accounts a ON a.id = om.account_id
              WHERE om.organisation_id = tgt_org.organisation_id AND om.deactivated_at IS NULL
                AND om.role = 'admin' AND a.active IS TRUE AND a.is_break_glass IS FALSE
                AND a.id <> $2) = 0
         AND EXISTS (SELECT 1 FROM public.org_memberships om2 JOIN public.accounts a2 ON a2.id = om2.account_id
                     WHERE om2.organisation_id = tgt_org.organisation_id
                       AND a2.is_break_glass IS FALSE) THEN
        RAISE EXCEPTION 'deactivation would orphan org' USING ERRCODE = 'M2020';
      END IF;
    END LOOP;

    IF is_operator THEN
      UPDATE public.token_families SET is_blacklisted = true, blacklisted_at = now() WHERE account_id = $2;
      UPDATE public.org_api_keys SET revoked_at = now() WHERE account_id = $2 AND revoked_at IS NULL;
      UPDATE public.org_memberships SET deactivated_at = now() WHERE account_id = $2;
    ELSE
      UPDATE public.token_families SET is_blacklisted = true, blacklisted_at = now()
        WHERE account_id = $2 AND family_id IN
          (SELECT tf.family_id FROM public.token_families tf
           JOIN public.org_memberships caller ON caller.organisation_id = tf.organisation_id
           WHERE tf.account_id = $2 AND caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin');
      UPDATE public.org_api_keys SET revoked_at = now()
        WHERE account_id = $2 AND revoked_at IS NULL AND organisation_id IN
          (SELECT caller.organisation_id FROM public.org_memberships caller
           WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin');
      UPDATE public.org_memberships SET deactivated_at = now()
        WHERE account_id = $2 AND organisation_id IN
          (SELECT caller.organisation_id FROM public.org_memberships caller
           WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin');
    END IF;
    UPDATE public.accounts SET active = false WHERE id = $2;
    IF NOT FOUND THEN RAISE EXCEPTION 'target does not exist' USING ERRCODE = 'M2040'; END IF;
    IF is_bg_target THEN
      UPDATE public.accounts SET break_glass_expires_at = NULL, break_glass_deactivated_at = now(),
                                 password_hash = gen_random_uuid()::text WHERE id = $2;
    END IF;
  END $$;
"""


def _capture_baseline() -> None:
    """Capture the pre-0036 owners of the transferred tables + function.

    Must run BEFORE the first ``OWNER TO`` / ``ALTER FUNCTION OWNER`` statement
    so the downgrade can restore the exact baseline. The marker lives in the
    non-public ``modulo_internal`` schema to stay invisible to the ORM-schema
    parity canary (``compare_metadata`` inspects only the public schema).
    """
    op.execute("CREATE SCHEMA IF NOT EXISTS modulo_internal")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS modulo_internal.break_glass_owner_baseline (
            obj_kind text NOT NULL,
            obj_name text NOT NULL PRIMARY KEY,
            owner_role text NOT NULL
        )
        """
    )
    op.execute("TRUNCATE modulo_internal.break_glass_owner_baseline")
    op.execute(
        """
        INSERT INTO modulo_internal.break_glass_owner_baseline (obj_kind, obj_name, owner_role)
        SELECT 'table', c.relname, pg_get_userbyid(c.relowner)
        FROM pg_class c
        WHERE c.oid IN (
            'public.accounts'::regclass,
            'public.org_memberships'::regclass,
            'public.token_families'::regclass,
            'public.org_api_keys'::regclass
        )
        """
    )
    op.execute(
        """
        INSERT INTO modulo_internal.break_glass_owner_baseline (obj_kind, obj_name, owner_role)
        SELECT 'function', 'lookup_api_key_org', pg_get_userbyid(p.proowner)
        FROM pg_proc p
        WHERE p.oid = 'public.lookup_api_key_org(text)'::regprocedure
        """
    )


def upgrade() -> None:
    op.execute("ALTER TABLE public.accounts ADD COLUMN is_break_glass BOOL NOT NULL DEFAULT false")
    op.execute("ALTER TABLE public.accounts ADD COLUMN break_glass_expires_at timestamptz NULL")
    op.execute("ALTER TABLE public.accounts ADD COLUMN break_glass_deactivated_at timestamptz NULL")
    op.execute(
        "ALTER TABLE public.accounts ADD CONSTRAINT ck_accounts_break_glass_expiry CHECK ("
        "NOT is_break_glass OR break_glass_expires_at IS NOT NULL OR break_glass_deactivated_at IS NOT NULL)"
    )

    _capture_baseline()

    # ONE ALTER per table — multi-table OWNER TO is invalid SQL.
    op.execute("ALTER TABLE public.accounts OWNER TO modulo_migrate")
    op.execute("ALTER TABLE public.org_memberships OWNER TO modulo_migrate")
    op.execute("ALTER TABLE public.token_families OWNER TO modulo_migrate")
    op.execute("ALTER TABLE public.org_api_keys OWNER TO modulo_migrate")
    op.execute("ALTER FUNCTION public.lookup_api_key_org(text) OWNER TO modulo_migrate")

    op.execute(_DEACTIVATE_BREAK_GLASS_SQL)
    op.execute("ALTER FUNCTION public.deactivate_break_glass(uuid, uuid, boolean) OWNER TO modulo_migrate")
    op.execute("REVOKE EXECUTE ON FUNCTION public.deactivate_break_glass(uuid, uuid, boolean) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.deactivate_break_glass(uuid, uuid, boolean) TO modulo_app, modulo_breakglass"
    )


def _restore_owner(obj_kind: str, obj_name: str, owner_role: str) -> None:
    quoted = f'"{owner_role.replace(chr(34), chr(34) + chr(34))}"'
    if obj_kind == "function":
        op.execute(f"ALTER FUNCTION public.{obj_name}(text) OWNER TO {quoted}")
    else:
        op.execute(f"ALTER TABLE public.{obj_name} OWNER TO {quoted}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.deactivate_break_glass(uuid, uuid, boolean)")

    # Restore the captured baseline owners (reverse of the OWNER TO above).
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT obj_kind, obj_name, owner_role "
                "FROM modulo_internal.break_glass_owner_baseline "
                "ORDER BY obj_name"
            )
        )
        .fetchall()
    )
    for obj_kind, obj_name, owner_role in rows:
        _restore_owner(obj_kind, obj_name, owner_role)

    op.execute("ALTER TABLE public.accounts DROP CONSTRAINT IF EXISTS ck_accounts_break_glass_expiry")
    op.execute("ALTER TABLE public.accounts DROP COLUMN IF EXISTS break_glass_deactivated_at")
    op.execute("ALTER TABLE public.accounts DROP COLUMN IF EXISTS break_glass_expires_at")
    op.execute("ALTER TABLE public.accounts DROP COLUMN IF EXISTS is_break_glass")
    op.execute("DROP TABLE IF EXISTS modulo_internal.break_glass_owner_baseline")

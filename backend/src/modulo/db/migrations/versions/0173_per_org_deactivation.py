"""Per-org user deactivation: the membership tombstone, not accounts.active.

Revision ID: 0173_per_org_deactivation
Revises: 0172_seed_orphan_organisation
Create Date: 2026-09-02

Why per-org (gh-1794 / FAR-533)
-------------------------------
The caller-bound SECURITY DEFINER ``deactivate_break_glass`` already scoped
its ``org_memberships`` / ``token_families`` / ``org_api_keys`` revocations to
the CALLING admin's organisation in the non-operator branch, but then
unconditionally ran ``UPDATE public.accounts SET active = false`` — an
ACCOUNT-GLOBAL flag flipped on the authorisation basis of a SINGLE shared org.
An admin of org A could therefore lock a shared user out of orgs B/C where
they hold no authority (and ``admin_reactivate_user`` could unlock them
everywhere). The org-membership ``deactivated_at`` tombstone the function
already writes IS the per-org deactivation signal; role resolution, JWT
revalidation and refresh already honour it (ADR 017).

This migration redefines the function (identical signature, identical
authorisation / advisory-lock / last-admin logic) so that:

* the non-operator branch keeps the org-scoped revocations VERBATIM and no
  longer touches ``accounts.active`` — deactivation is per-org;
* the operator / break-glass branch (``session_user = 'modulo_breakglass'``)
  KEEPS the global ``accounts.active = false`` flip — break-glass is a global
  ban by design;
* the M2040 "target does not exist" semantics are preserved in both branches
  (operator branch via ``GET DIAGNOSTICS``/FOUND after the accounts UPDATE as
  before; non-operator branch via an explicit existence check, since the
  accounts UPDATE no longer runs there).

Legacy-data caveat (accepted by Duncan): accounts deactivated under the OLD
global behaviour have ``accounts.active = false`` and stay globally inactive
until an OPERATOR reactivates the account — clearing the membership tombstone
alone cannot restore a globally-flipped account, and this migration
deliberately does NOT back-fill ``accounts.active`` back to true.
"""

from __future__ import annotations

from alembic import op

revision: str = "0173_per_org_deactivation"
down_revision: str | None = "0172_seed_orphan_organisation"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Identical to the 0108 definition EXCEPT the ``UPDATE public.accounts SET
# active = false`` statement (and its M2040 NOT FOUND check) moved inside the
# operator branch, and the non-operator branch gained an equivalent explicit
# M2040 existence check. Non-operator revocations are verbatim from 0108.
_PER_ORG_DEACTIVATE_BREAK_GLASS = (
    "CREATE OR REPLACE FUNCTION public.deactivate_break_glass(caller_account_id uuid, "
    "target_account_id uuid, force_last_admin boolean DEFAULT false) RETURNS void LANGUAGE plpgsql "
    "SECURITY DEFINER SET search_path TO 'pg_catalog', 'public' SET row_security TO 'off' AS $_$ "
    "DECLARE tgt_org RECORD; k1 int4; k2 int4; is_operator bool; is_bg_target bool; "
    "BEGIN is_operator := session_user = 'modulo_breakglass'; "
    "is_bg_target := EXISTS (SELECT 1 FROM public.accounts WHERE id = $2 AND is_break_glass IS TRUE); "
    "IF $3 AND NOT is_operator THEN RAISE EXCEPTION 'force_last_admin requires operator' USING ERRCODE = 'M2010'; END IF; "
    "IF NOT (is_operator OR EXISTS (SELECT 1 FROM public.org_memberships caller WHERE caller.account_id = $1 "
    "AND caller.deactivated_at IS NULL AND caller.role = 'admin' AND EXISTS (SELECT 1 FROM public.org_memberships tgt "
    "WHERE tgt.account_id = $2 AND tgt.organisation_id = caller.organisation_id)) "
    "OR (EXISTS (SELECT 1 FROM public.accounts c WHERE c.id = $1 AND c.is_break_glass IS TRUE AND c.active IS TRUE) "
    "AND is_bg_target AND EXISTS (SELECT 1 FROM public.org_memberships cm JOIN public.org_memberships tm "
    "ON tm.organisation_id = cm.organisation_id WHERE cm.account_id = $1 AND tm.account_id = $2))) "
    "THEN RAISE EXCEPTION 'caller not authorized to deactivate target' USING ERRCODE = 'M2010'; END IF; "
    " FOR tgt_org IN SELECT DISTINCT organisation_id FROM public.org_memberships WHERE account_id = $2 "
    "AND deactivated_at IS NULL ORDER BY organisation_id LOOP "
    "SELECT ('x' || substr(md5(tgt_org.organisation_id::text), 1, 8))::bit(32)::int4, "
    "('x' || substr(md5(tgt_org.organisation_id::text), 9, 8))::bit(32)::int4 INTO k1, k2; "
    "PERFORM pg_advisory_xact_lock(k1, k2); END LOOP; "
    " FOR tgt_org IN SELECT DISTINCT organisation_id FROM public.org_memberships WHERE account_id = $2 "
    "AND deactivated_at IS NULL LOOP "
    "IF NOT $3 AND (SELECT count(*) FROM public.org_memberships om JOIN public.accounts a ON a.id = om.account_id "
    "WHERE om.organisation_id = tgt_org.organisation_id AND om.deactivated_at IS NULL AND om.role = 'admin' "
    "AND a.active IS TRUE AND a.is_break_glass IS FALSE AND a.id <> $2) = 0 "
    "AND EXISTS (SELECT 1 FROM public.org_memberships om2 JOIN public.accounts a2 ON a2.id = om2.account_id "
    "WHERE om2.organisation_id = tgt_org.organisation_id AND a2.is_break_glass IS FALSE) "
    "THEN RAISE EXCEPTION 'deactivation would orphan org' USING ERRCODE = 'M2020'; END IF; END LOOP; "
    " /* gh-1794/FAR-533: the global accounts.active flip is OPERATOR-ONLY (break-glass = global ban). */ "
    "IF is_operator THEN "
    "UPDATE public.token_families SET is_blacklisted = true, blacklisted_at = now() WHERE account_id = $2; "
    "UPDATE public.org_api_keys SET revoked_at = now() WHERE account_id = $2 AND revoked_at IS NULL; "
    "UPDATE public.org_memberships SET deactivated_at = now() WHERE account_id = $2; "
    "UPDATE public.accounts SET active = false WHERE id = $2; "
    "IF NOT FOUND THEN RAISE EXCEPTION 'target does not exist' USING ERRCODE = 'M2040'; END IF; "
    "ELSE "
    " /* gh-1794/FAR-533: per-org deactivation — the org-membership deactivated_at tombstone written below IS "
    "the deactivation signal; accounts.active is left untouched so other orgs are unaffected. "
    "The explicit existence check preserves the M2040 semantics the old accounts UPDATE provided. */ "
    "IF NOT EXISTS (SELECT 1 FROM public.accounts WHERE id = $2) "
    "THEN RAISE EXCEPTION 'target does not exist' USING ERRCODE = 'M2040'; END IF; "
    "UPDATE public.token_families SET is_blacklisted = true, blacklisted_at = now() WHERE account_id = $2 "
    "AND family_id IN (SELECT tf.family_id FROM public.token_families tf JOIN public.org_memberships caller "
    "ON caller.organisation_id = tf.organisation_id WHERE tf.account_id = $2 AND caller.account_id = $1 "
    "AND caller.deactivated_at IS NULL AND caller.role = 'admin'); "
    "UPDATE public.org_api_keys SET revoked_at = now() WHERE account_id = $2 AND revoked_at IS NULL "
    "AND organisation_id IN (SELECT caller.organisation_id FROM public.org_memberships caller "
    "WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'); "
    "UPDATE public.org_memberships SET deactivated_at = now() WHERE account_id = $2 "
    "AND organisation_id IN (SELECT caller.organisation_id FROM public.org_memberships caller "
    "WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'); "
    "END IF; "
    "IF is_bg_target THEN "
    "UPDATE public.accounts SET break_glass_expires_at = NULL, break_glass_deactivated_at = now(), "
    "password_hash = gen_random_uuid()::text WHERE id = $2; "
    "END IF; "
    "END $_$;"
)


def upgrade() -> None:
    op.execute(_PER_ORG_DEACTIVATE_BREAK_GLASS)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops. The
    # pre-0173 function body is recoverable by re-applying the 0108 statement;
    # re-defining it here would resurrect the global accounts.active flip the
    # fix exists to remove.
    pass

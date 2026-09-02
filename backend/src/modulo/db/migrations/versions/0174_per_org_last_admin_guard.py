"""Per-org M2020 last-admin guard in deactivate_break_glass (FAR-539).

Revision ID: 0174_per_org_last_admin_guard
Revises: 0173_per_org_deactivation
Create Date: 2026-09-02

Why the guard was over-conservative
-----------------------------------
FAR-533 (migration ``0173_per_org_deactivation``) made the non-operator branch
of the caller-bound SECURITY DEFINER ``deactivate_break_glass`` per-org: it
tombstones ONLY the caller's active-admin org memberships and leaves
``accounts.active`` (and every other org of the target) untouched. But its
M2020 "deactivation would orphan org" guard still iterated EVERY active org of
the TARGET and refused if the deactivation would orphan ANY of them.

Concrete example: Alice is an admin of org-A; Bob is a plain member of org-A
and the LAST admin of org-B. Alice deactivating Bob in org-A was refused with
M2020 because org-B would "lose its last admin" — yet the non-operator branch
never touches org-B (Bob's org-B membership stays active and admin), so org-B
cannot be orphaned. The refusal protected an org the call does not modify.

What this revision changes
--------------------------
The function body is the FAR-533 per-org definition VERBATIM except the SECOND
FOR loop (the M2020 guard): its cursor query now selects only the orgs this
call actually tombstones —

* operator branch (``session_user = 'modulo_breakglass'``) tombstones every
  org of the target, so ``is_operator`` keeps the all-orgs iteration verbatim
  (the operator branch's guard semantics are unchanged);
* the non-operator branch tombstones only the caller's active-admin orgs, so
  the guard iterates exactly that set (the same predicate as the
  ``org_memberships`` UPDATE in the ELSE branch).

The guard's M2020 condition itself (zero remaining active non-break-glass
admins, org still has non-break-glass members) is unchanged, as are the M2010
authorisation arms, the M2040 semantics, the first FOR loop's advisory locks
(deliberately still taken on EVERY org of the target — over-locking is
harmless and keeps the operator branch byte-identical), and the
break-glass-target scramble.

A genuine same-org refusal is preserved: the caller is an active admin of
every org the non-operator branch tombstones, so a regular caller deactivating
another admin of their own org can no longer orphan it (the caller remains);
the refusals that remain are self-deactivation of an org's last admin and a
break-glass-account caller (excluded from the admin count by
``a.is_break_glass IS FALSE``) removing the last non-break-glass admin of the
org being tombstoned.

Stacking note: this revision sits directly on ``0173_per_org_deactivation``
and re-applies that body WITH the relaxed guard. Any FUTURE migration that
redefines ``deactivate_break_glass`` must carry this relaxed M2020 guard —
re-applying an older (all-orgs) body would silently revert FAR-539.
"""

from __future__ import annotations

from alembic import op

revision: str = "0174_per_org_last_admin_guard"
down_revision: str | None = "0173_per_org_deactivation"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Identical to the FAR-533 per-org definition (0173_per_org_deactivation)
# EXCEPT the second FOR loop's cursor query, which restricts the M2020 guard
# to the orgs the call actually tombstones (all of the target's orgs for the
# operator branch — verbatim behaviour — and only the caller's active-admin
# orgs for the non-operator branch).
_PER_ORG_LAST_ADMIN_DEACTIVATE_BREAK_GLASS = (
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
    " /* FAR-539: the M2020 last-admin guard is PER-ORG — it covers only the orgs this call tombstones. "
    "The operator branch tombstones EVERY org of the target, so is_operator keeps the all-orgs iteration "
    "verbatim; the non-operator branch tombstones only the caller's active-admin orgs (the same predicate "
    "as the org_memberships UPDATE below), so the guard iterates exactly that set. A target who is the last "
    "admin of org-B can be deactivated from org-A because org-B is untouched and cannot be orphaned. */ "
    " FOR tgt_org IN SELECT DISTINCT om.organisation_id FROM public.org_memberships om "
    "WHERE om.account_id = $2 AND om.deactivated_at IS NULL "
    "AND (is_operator OR EXISTS (SELECT 1 FROM public.org_memberships caller "
    "WHERE caller.account_id = $1 AND caller.organisation_id = om.organisation_id "
    "AND caller.deactivated_at IS NULL AND caller.role = 'admin')) LOOP "
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
    op.execute(_PER_ORG_LAST_ADMIN_DEACTIVATE_BREAK_GLASS)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops. The
    # pre-0174 function body is recoverable by re-applying the
    # 0173_per_org_deactivation statement; re-defining it here would resurrect
    # the all-orgs M2020 guard this fix exists to relax.
    pass

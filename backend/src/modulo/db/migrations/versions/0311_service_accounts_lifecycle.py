"""FAR-1478: service-account lifecycle column + structural admin-role ceiling + immutable kind.

Slice 2 of ADR 056 / ADR 058 makes a service principal usable: it is created
with its own name, granted team/org roles (ceiling ``operator``) and mints its
own ``mk_`` keys. Three invariants in that flow need DB-level structure, not
only application checks:

1. ``accounts.decommissioned_at`` — the EXPLICIT retirement marker for a
   service principal (ADR 058: "decommissioned explicitly, not by owner
   death"). A service account is never torn down because a human went away;
   an org admin retires it deliberately, and the timestamp is the durable
   record of that act. The accompanying CHECK

       decommissioned_at IS NULL OR kind = 'service'

   makes the marker meaningful: humans are deactivated per-org
   (``org_memberships.deactivated_at``, FAR-533) and can never carry the
   decommission state, so the two lifecycles cannot be conflated by a future
   write path.

2. ``trg_org_memberships_service_role_ceiling`` — a service principal can
   never hold the ``admin`` org role (ADR 058: "machine principals never hold
   admin"; ADR 056: org-*owned*, not org-*unrestricted*; ceiling is
   ``operator``). ``org_memberships.role`` is the ONE role vocabulary that
   still admits ``admin`` (team roles and API-key roles are already CHECKed to
   operator/runner, and ``ck_accounts_service_privilege`` already bars a
   service row from ``is_system_admin``/``is_break_glass``), and a CHECK
   constraint cannot join across tables — so the ceiling is a BEFORE-row
   trigger: any writer (REST, raw SQL, a future write path that forgets to
   check) is refused with ERRCODE 23514 (check_violation) when it tries to
   grant ``admin`` to a service account. The application surfaces reject the
   grant with a 422 first; the trigger is the structural backstop.

3. ``trg_accounts_kind_immutable`` — ``accounts.kind`` is a CREATE-time
   discriminator (ADR 056/058: a principal is BORN human or service), never
   mutated after insert. The role ceiling above (2) reads the CURRENT
   ``accounts.kind``, so without this trigger a raw-SQL
   ``UPDATE accounts SET kind='service'`` on a human holding an ``admin``
   org membership would leave that membership intact — the exact state ADR
   056/058 declare structurally impossible — and no application path writes
   the column post-insert (provisioning stamps it at INSERT). A BEFORE
   UPDATE OF kind trigger refuses ANY kind transition with ERRCODE 23514,
   making the flip structurally impossible for every writer.

The trigger functions are SECURITY INVOKER (the runtime role already SELECTs
``accounts`` for every identity lookup, and a definer-rights escalation on an
authorization guard would be the wrong shape). Deploy ordering (ADR 030):
migrate FIRST, then roll code — old code never writes ``decommissioned_at``.

Revision ID: 0311_service_accounts_lifecycle
Revises: 0310_library_cluster_db_hardening
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0311_service_accounts_lifecycle"
down_revision: str | None = "0310_library_cluster_db_hardening"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_ACCOUNTS = "accounts"
_DECOMMISSIONED_CHECK = "ck_accounts_decommissioned_service"
_DECOMMISSIONED_PREDICATE = "decommissioned_at IS NULL OR kind = 'service'"

_TRIGGER_FUNCTION = "enforce_service_role_ceiling"
_TRIGGER_NAME = "trg_org_memberships_service_role_ceiling"

# DDL is deliberately built from LITERAL strings (no f-string / %-format
# splicing — the ruff S608 gate): the identifiers above are mirrored as
# literals here and pinned by tests/unit/db/test_migration_0311_service_accounts_lifecycle.
_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION public.enforce_service_role_ceiling() RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'pg_catalog', 'public'
AS $_$
DECLARE
    target_kind text;
BEGIN
    IF NEW.role = 'admin' THEN
        SELECT kind INTO target_kind FROM public.accounts WHERE id = NEW.account_id;
        IF target_kind = 'service' THEN
            RAISE EXCEPTION 'service principals cannot hold the admin role'
                USING ERRCODE = '23514',
                      DETAIL = 'ADR 058: a service account''s org-role ceiling is operator.',
                      HINT = 'Grant operator, runner or viewer instead.';
        END IF;
    END IF;
    RETURN NEW;
END;
$_$;
"""

# Idempotent DO-block form, the shape every tenant trigger in 0108/0293 uses,
# so a re-run of the upgrade on an already-migrated database is a no-op.
_TRIGGER_DDL = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_org_memberships_service_role_ceiling') "
    "THEN CREATE TRIGGER trg_org_memberships_service_role_ceiling BEFORE INSERT OR UPDATE OF role ON public.org_memberships "
    "FOR EACH ROW EXECUTE FUNCTION public.enforce_service_role_ceiling(); END IF; END $$;"
)

# ADR 056/058: ``kind`` is a CREATE-time discriminator — a principal is BORN
# human or service and never converted. Without this trigger a raw-SQL kind
# flip (human -> service) on an account holding an ``admin`` org membership
# would leave that membership intact (the role ceiling above fires only on
# membership writes, and ``accounts`` has no RLS), producing the exact state
# ADR 056/058 declare structurally impossible. No application path writes
# ``accounts.kind`` post-insert (provisioning stamps it at INSERT), so
# immutability costs nothing and closes the hole for every future writer.
_KIND_IMMUTABLE_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION public.enforce_account_kind_immutable() RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'pg_catalog', 'public'
AS $_$
BEGIN
    IF NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'account kind is immutable'
            USING ERRCODE = '23514',
                  DETAIL = 'ADR 056/058: an account''s kind is fixed at creation.',
                  HINT = 'Provision a new service account instead of converting an existing row.';
    END IF;
    RETURN NEW;
END;
$_$;
"""

_KIND_IMMUTABLE_TRIGGER_DDL = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_accounts_kind_immutable') "
    "THEN CREATE TRIGGER trg_accounts_kind_immutable BEFORE UPDATE OF kind ON public.accounts "
    "FOR EACH ROW EXECUTE FUNCTION public.enforce_account_kind_immutable(); END IF; END $$;"
)


def upgrade() -> None:
    op.add_column(
        _ACCOUNTS,
        sa.Column("decommissioned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(_DECOMMISSIONED_CHECK, _ACCOUNTS, _DECOMMISSIONED_PREDICATE)
    op.execute(_FUNCTION_DDL)
    op.execute(_TRIGGER_DDL)
    op.execute(_KIND_IMMUTABLE_FUNCTION_DDL)
    op.execute(_KIND_IMMUTABLE_TRIGGER_DDL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_accounts_kind_immutable ON public.accounts;")
    op.execute("DROP FUNCTION IF EXISTS public.enforce_account_kind_immutable();")
    op.execute("DROP TRIGGER IF EXISTS trg_org_memberships_service_role_ceiling ON public.org_memberships;")
    op.execute("DROP FUNCTION IF EXISTS public.enforce_service_role_ceiling();")
    op.drop_constraint(_DECOMMISSIONED_CHECK, _ACCOUNTS, type_="check")
    op.drop_column(_ACCOUNTS, "decommissioned_at")

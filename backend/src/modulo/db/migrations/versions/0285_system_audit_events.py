"""FAR-1517: ``system_audit_events`` — org-independent durable audit ledger.

Revision ID: 0285_system_audit_events
Revises: 0284_add_rejected_run_status
Create Date: 2026-10-06

Why
---
``audit_events.organisation_id`` FKs ``organisations.id`` with ``ON DELETE
CASCADE``, so hard-deleting an organisation destroys its whole audit trail —
including the ``org_deletion_requested`` row written moments before the delete.
A post-commit append is no fix: it can never satisfy the FK on an org that no
longer exists. This revision adds a ledger that a hard delete cannot reach:

* NO ``organisation_id`` column (the org id rides as ``org_id``, a plain uuid
  with no FK) — nothing cascades into the table, and the row stays outside the
  ``rls_org_isolation`` / ORM-tenant-filter regime, both of which key on the
  exact ``organisation_id`` column name;
* NO foreign keys at all — the evidence must outlive whatever it references;
* append-only, enforced in the database: ``BEFORE UPDATE`` / ``BEFORE DELETE``
  triggers raise, mirroring ``audit_events`` (0005/0108).

Write path: :func:`modulo.core.system_audit_logger.append_system_audit_event`,
called IN the deleting transaction before the org row is removed, so the record
commits atomically with the delete and any append failure rolls the whole
destructive act back (fail-closed).

No RLS ceremony here is deliberate, not an omission: there is no
``organisation_id`` column for the coverage tests to require a policy on, and
a policy would hide the one record an operator needs after the org is gone.
Explicit DML grants are applied anyway (0204/0242 precedent) so the write
succeeds regardless of how the table's default privileges resolve.

Second concern in the same revision — unblocking the org hard-delete
-------------------------------------------------------------------
Proving the fix above surfaced a pre-existing conflict: ``audit_events`` and
``error_events`` are append-only (0005/0108 guards raise on UPDATE/DELETE),
yet BOTH also FK ``organisation_id`` to ``organisations`` with ``ON DELETE
CASCADE`` (0108 / 0110). A cascaded delete is still a row-level DELETE, so the
guard fired on it: hard-deleting an organisation carrying even one audit or
error row raised ``audit_events are append-only: DELETE is not permitted``
(observed directly in the migrated test database) and the whole deletion
rolled back — no org with real history could be deleted at all.

The guards now let an RI cascade through (``pg_trigger_depth() > 1``: the
foreign-key system's own cascade trigger wraps ours) while a direct
UPDATE/DELETE statement still raises. That restores the behaviour the FK
always declared, and it is precisely why the org-lifecycle evidence must also
land in ``system_audit_events`` above: the org-scoped trail now genuinely
goes away with the org.

Downgrade drops the triggers, the trigger function, the table, and restores
the two append-only guards to their pre-0285 bodies.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0285_system_audit_events"
down_revision: str | None = "0284_add_rejected_run_status"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

# Module-level tuple of the tables this migration makes append-only, so an
# architecture test can enumerate them the way the RLS tests enumerate their
# own coverage constants.
_APPEND_ONLY_TABLES = ("system_audit_events",)

_TABLE = "system_audit_events"

# Pre-0285 bodies (verbatim 0108), restored on downgrade.
_AUDIT_APPEND_ONLY_ORIGINAL = """
CREATE OR REPLACE FUNCTION public.audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'audit_events are append-only: DELETE is not permitted';
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'audit_events are append-only: UPDATE is not permitted';
    END IF;
    RETURN NULL;
END;
$$;
"""

_ERROR_APPEND_ONLY_ORIGINAL = """
CREATE OR REPLACE FUNCTION public.error_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'error_events are append-only: DELETE is not permitted';
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'error_events are append-only: UPDATE is not permitted';
    END IF;
    RETURN NULL;
END;
$$;
"""

# 0285 bodies: a direct statement still raises (trigger depth 1), an
# ON DELETE CASCADE from the parent org passes (the RI cascade trigger wraps
# ours, so the guard runs at depth >= 2). The allowed path returns OLD —
# a BEFORE trigger returning NULL would silently SKIP the delete.
_AUDIT_APPEND_ONLY_ALLOW_CASCADE = """
CREATE OR REPLACE FUNCTION public.audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'audit_events are append-only: UPDATE is not permitted';
    ELSIF TG_OP = 'DELETE' THEN
        IF pg_trigger_depth() <= 1 THEN
            RAISE EXCEPTION 'audit_events are append-only: DELETE is not permitted';
        END IF;
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
"""

_ERROR_APPEND_ONLY_ALLOW_CASCADE = """
CREATE OR REPLACE FUNCTION public.error_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'error_events are append-only: UPDATE is not permitted';
    ELSIF TG_OP = 'DELETE' THEN
        IF pg_trigger_depth() <= 1 THEN
            RAISE EXCEPTION 'error_events are append-only: DELETE is not permitted';
        END IF;
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
"""


def _role_exists(bind: sa.Connection, role: str) -> bool:
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _is_postgres(bind: sa.Connection) -> bool:
    return str(bind.dialect.name).startswith("postgres")


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("resource_type", sa.String(length=100), nullable=True),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("current_timestamp"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_system_audit_events_event_type", _TABLE, ["event_type"], unique=False)
    op.create_index("ix_system_audit_events_org_id_created_at", _TABLE, ["org_id", "created_at"], unique=False)

    if not pg:
        # SQLite (unit-test schema): no trigger functions, no roles, no grants.
        return

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION public.system_audit_events_append_only() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'system_audit_events are append-only: DELETE is not permitted';
                ELSIF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'system_audit_events are append-only: UPDATE is not permitted';
                END IF;
                RETURN NULL;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='system_audit_events_no_delete') "
            "THEN CREATE TRIGGER system_audit_events_no_delete BEFORE DELETE ON public.system_audit_events "
            "FOR EACH ROW EXECUTE FUNCTION public.system_audit_events_append_only(); END IF; END $$;"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='system_audit_events_no_update') "
            "THEN CREATE TRIGGER system_audit_events_no_update BEFORE UPDATE ON public.system_audit_events "
            "FOR EACH ROW EXECUTE FUNCTION public.system_audit_events_append_only(); END IF; END $$;"
        )
    )

    # Unblock the org hard-delete: the append-only guards on audit_events /
    # error_events fired on the organisations ON DELETE CASCADE itself, so an
    # org carrying audit or error rows could never be deleted (see the module
    # docstring). Direct tampering stays blocked at trigger depth 1.
    op.execute(sa.text(_AUDIT_APPEND_ONLY_ALLOW_CASCADE))
    op.execute(sa.text(_ERROR_APPEND_ONLY_ALLOW_CASCADE))

    # Explicit DML grants (0204/0242 precedent): BYPASSRLS skips row security,
    # NOT table privileges — and the table may be created under a role whose
    # default privileges do not reach modulo_app. The DDL is a literal: the
    # migration-fstring-sql rule forbids interpolating into op.execute (it
    # cannot bind parameters), and both names below are module constants
    # checked for role existence first.
    if _role_exists(bind, _APP_ROLE):
        op.execute(sa.text("GRANT SELECT, INSERT, UPDATE, DELETE ON system_audit_events TO modulo_app"))
    if _role_exists(bind, _SYSTEM_ROLE):
        op.execute(sa.text("GRANT SELECT, INSERT, UPDATE, DELETE ON system_audit_events TO modulo_system"))


def downgrade() -> None:
    bind = op.get_bind()

    if _is_postgres(bind):
        op.execute(sa.text("DROP TRIGGER IF EXISTS system_audit_events_no_update ON public.system_audit_events"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS system_audit_events_no_delete ON public.system_audit_events"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS public.system_audit_events_append_only()"))
        # Restore the two shared append-only guards to their pre-0285 bodies.
        op.execute(sa.text(_AUDIT_APPEND_ONLY_ORIGINAL))
        op.execute(sa.text(_ERROR_APPEND_ONLY_ORIGINAL))

    op.drop_index("ix_system_audit_events_org_id_created_at", table_name=_TABLE)
    op.drop_index("ix_system_audit_events_event_type", table_name=_TABLE)
    op.drop_table(_TABLE)

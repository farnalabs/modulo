"""Harden tenant-scoped FKs to organisations (FAR-294).

Revision ID: 0118_org_fk_hardening
Revises: 0117_toctou_hardening
Create Date: 2026-08-20

Adds a DB-level foreign key from every ``organisation_id`` column to
``organisations(id)`` across all tenant-scoped tables. This closes the class of
defect that put an invalid organisation FK "everywhere" in prod: with the FK in
place, the database itself rejects writes that would create an orphaned tenant
row.

The FK is only added when the table's existing rows are already clean (no
orphaned references), so this migration never fails on a live dataset that still
contains invalid org FKs. Any table that still has orphans is left untouched by
design — those rows are surfaced for triage by the housekeeping ``invalid_org_fk``
scan rather than being silently deleted.

This is a reconciliation-style migration: it is idempotent (guards on existing
constraints) and intentionally non-reversible.
"""

from alembic import op
from sqlalchemy import text

revision = "0118_org_fk_hardening"
down_revision = "0117_toctou_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            DO $$
            DECLARE
                rec RECORD;
                fk_exists BOOLEAN;
                orphan_count BIGINT;
                constraint_name TEXT;
            BEGIN
                FOR rec IN
                    SELECT c.table_name
                    FROM information_schema.columns c
                    WHERE c.table_schema = 'public'
                      AND c.column_name = 'organisation_id'
                      AND c.table_name <> 'organisations'
                LOOP
                    constraint_name := 'fk_' || rec.table_name || '_organisation_id';
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_constraint con
                        JOIN pg_class rel ON rel.oid = con.conrelid
                        JOIN pg_class refrel ON refrel.oid = con.confrelid
                        JOIN pg_attribute att
                            ON att.attrelid = rel.oid
                           AND att.attnum = ANY(con.conkey)
                        WHERE rel.relname = rec.table_name
                          AND con.contype = 'f'
                          AND refrel.relname = 'organisations'
                          AND att.attname = 'organisation_id'
                    ) INTO fk_exists;
                    IF NOT fk_exists THEN
                        EXECUTE format(
                            'SELECT count(*) FROM %I t '
                            'WHERE t.organisation_id IS NOT NULL '
                            'AND NOT EXISTS ('
                            '  SELECT 1 FROM organisations o WHERE o.id = t.organisation_id'
                            ')',
                            rec.table_name
                        ) INTO orphan_count;
                        IF orphan_count = 0 THEN
                            EXECUTE format(
                                'ALTER TABLE %I ADD CONSTRAINT %I '
                                'FOREIGN KEY (organisation_id) '
                                'REFERENCES organisations(id) ON DELETE CASCADE',
                                rec.table_name,
                                constraint_name
                            );
                        END IF;
                    END IF;
                END LOOP;
            END $$;
            """
        )
    )


def downgrade() -> None:
    # Reconciliation migrations are not reversible: the FK constraints are part of
    # the canonical schema and are re-added (idempotently) on re-upgrade.
    pass

"""Add soft-delete + audit columns to agents, connector_instances, scheduled_reports.

This mirrors the established convention for OrgScoped soft-delete tables
(see ``SoftDeleteMixin`` in ``db/models/base.py`` and the
``0125_soft_delete_lookup_indexes`` migration):

* Apply ``SoftDeleteMixin`` (``deleted_at``) to the three models.
* Add ``deleted_by`` / ``created_by`` / ``updated_by`` audit columns
  (nullable, FK to ``accounts`` with ``ON DELETE SET NULL``).
* Add the partial composite index ``(organisation_id, deleted_at)
  WHERE deleted_at IS NULL`` that the 0125 migration added for every other
  OrgScoped + SoftDeleteMixin table - these three were not yet
  SoftDeleteMixin at that time, so they are covered here.
* Add the ``created_by`` tenant-enforcement trigger (cross-organisation
  reference guard), matching the pattern used for ``created_by`` on other
  tables (e.g. ``scheduled_reports`` already has this trigger).

``deleted_at``/``deleted_by`` enable soft delete: user-facing deletes stamp
these columns instead of hard-removing rows, so references (snapshots,
bindings, history) keep resolving.
"""

from alembic import op

revision: str = "0132_agent_connector_report_soft_delete_audit"
down_revision: str | None = "0131_eval_dataset_corpus"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- agents ---
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "deleted_by" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "created_by" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "updated_by" uuid;')
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_created_by') "
        'THEN ALTER TABLE public."agents" ADD CONSTRAINT fk_agents_created_by '
        "FOREIGN KEY (created_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_updated_by') "
        'THEN ALTER TABLE public."agents" ADD CONSTRAINT fk_agents_updated_by '
        "FOREIGN KEY (updated_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_deleted_by') "
        'THEN ALTER TABLE public."agents" ADD CONSTRAINT fk_agents_deleted_by '
        "FOREIGN KEY (deleted_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_agents_created_by_tenant') "
        "THEN CREATE TRIGGER trg_agents_created_by_tenant BEFORE INSERT OR UPDATE OF created_by, organisation_id "
        'ON public."agents" FOR EACH ROW '
        "EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'created_by'); END IF; END $$;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agents_organisation_id_deleted_at "
        'ON public."agents" USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);'
    )

    # --- connector_instances ---
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "deleted_by" uuid;')
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "created_by" uuid;')
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "updated_by" uuid;')
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_connector_instances_created_by') "
        'THEN ALTER TABLE public."connector_instances" ADD CONSTRAINT fk_connector_instances_created_by '
        "FOREIGN KEY (created_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_connector_instances_updated_by') "
        'THEN ALTER TABLE public."connector_instances" ADD CONSTRAINT fk_connector_instances_updated_by '
        "FOREIGN KEY (updated_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_connector_instances_deleted_by') "
        'THEN ALTER TABLE public."connector_instances" ADD CONSTRAINT fk_connector_instances_deleted_by '
        "FOREIGN KEY (deleted_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_connector_instances_created_by_tenant') "
        "THEN CREATE TRIGGER trg_connector_instances_created_by_tenant BEFORE INSERT OR UPDATE OF created_by, organisation_id "
        'ON public."connector_instances" FOR EACH ROW '
        "EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'created_by'); END IF; END $$;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_instances_organisation_id_deleted_at "
        'ON public."connector_instances" USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);'
    )

    # --- scheduled_reports (created_by column + trigger already exist) ---
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "deleted_by" uuid;')
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "updated_by" uuid;')
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_scheduled_reports_updated_by') "
        'THEN ALTER TABLE public."scheduled_reports" ADD CONSTRAINT fk_scheduled_reports_updated_by '
        "FOREIGN KEY (updated_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_scheduled_reports_deleted_by') "
        'THEN ALTER TABLE public."scheduled_reports" ADD CONSTRAINT fk_scheduled_reports_deleted_by '
        "FOREIGN KEY (deleted_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_scheduled_reports_organisation_id_deleted_at "
        'ON public."scheduled_reports" USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);'
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_scheduled_reports_organisation_id_deleted_at;")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_scheduled_reports_deleted_by') "
        'THEN ALTER TABLE public."scheduled_reports" DROP CONSTRAINT fk_scheduled_reports_deleted_by; END IF; END $$;'
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_scheduled_reports_updated_by') "
        'THEN ALTER TABLE public."scheduled_reports" DROP CONSTRAINT fk_scheduled_reports_updated_by; END IF; END $$;'
    )
    op.execute('ALTER TABLE public."scheduled_reports" DROP COLUMN IF EXISTS "updated_by";')
    op.execute('ALTER TABLE public."scheduled_reports" DROP COLUMN IF EXISTS "deleted_by";')
    op.execute('ALTER TABLE public."scheduled_reports" DROP COLUMN IF EXISTS "deleted_at";')

    op.execute('DROP TRIGGER IF EXISTS trg_connector_instances_created_by_tenant ON public."connector_instances";')
    op.execute("DROP INDEX IF EXISTS ix_connector_instances_organisation_id_deleted_at;")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_connector_instances_deleted_by') "
        'THEN ALTER TABLE public."connector_instances" DROP CONSTRAINT fk_connector_instances_deleted_by; END IF; END $$;'
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_connector_instances_updated_by') "
        'THEN ALTER TABLE public."connector_instances" DROP CONSTRAINT fk_connector_instances_updated_by; END IF; END $$;'
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_connector_instances_created_by') "
        'THEN ALTER TABLE public."connector_instances" DROP CONSTRAINT fk_connector_instances_created_by; END IF; END $$;'
    )
    op.execute('ALTER TABLE public."connector_instances" DROP COLUMN IF EXISTS "updated_by";')
    op.execute('ALTER TABLE public."connector_instances" DROP COLUMN IF EXISTS "deleted_by";')
    op.execute('ALTER TABLE public."connector_instances" DROP COLUMN IF EXISTS "created_by";')
    op.execute('ALTER TABLE public."connector_instances" DROP COLUMN IF EXISTS "deleted_at";')

    op.execute('DROP TRIGGER IF EXISTS trg_agents_created_by_tenant ON public."agents";')
    op.execute("DROP INDEX IF EXISTS ix_agents_organisation_id_deleted_at;")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_deleted_by') "
        'THEN ALTER TABLE public."agents" DROP CONSTRAINT fk_agents_deleted_by; END IF; END $$;'
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_updated_by') "
        'THEN ALTER TABLE public."agents" DROP CONSTRAINT fk_agents_updated_by; END IF; END $$;'
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_created_by') "
        'THEN ALTER TABLE public."agents" DROP CONSTRAINT fk_agents_created_by; END IF; END $$;'
    )
    op.execute('ALTER TABLE public."agents" DROP COLUMN IF EXISTS "updated_by";')
    op.execute('ALTER TABLE public."agents" DROP COLUMN IF EXISTS "deleted_by";')
    op.execute('ALTER TABLE public."agents" DROP COLUMN IF EXISTS "created_by";')
    op.execute('ALTER TABLE public."agents" DROP COLUMN IF EXISTS "deleted_at";')

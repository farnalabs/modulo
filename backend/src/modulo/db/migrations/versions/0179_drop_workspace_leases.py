"""Drop the unused workspace_leases table (FAR-587 / ADR 029).

Revision ID: 0179_drop_workspace_leases
Revises: 0178_env_profiles_runner_docker
Create Date: 2026-09-03

Why
---
The ``WorkspaceLease`` scaffolding (model, hub ``create_lease`` /
``destroy_lease``, the run-retention pre-delete workaround and its one API
reader ``GET /runs/{run_id}/workspace-lease``) never shipped a production
writer — ADR 004's "WorkspaceLease remains useful" bet did not pay off.
Capacity tracking uses ``runs.sandbox_dispatch_state`` plus the dispatch-time
advisory gate instead (D8 of the Agent Execution Tiers plan). D2 (FAR-587)
deletes the scaffolding in code and this migration drops the table.

Guarded upgrade
---------------
The drop REFUSES (raises) when lease rows exist, so a database that somehow
acquired lease rows is never silently destroyed — the operator must inspect
and clear them deliberately. An empty (or absent) table drops cleanly.

Guarded downgrade (tested)
--------------------------
Recreates the table EMPTY with its original shape (columns, PK, CHECK,
RESTRICT foreign keys, defaults, indexes, RLS policy and same-org tenant
triggers — the union of migrations 0005 and 0110's idempotent DDL). Lease row
data cannot be restored; the recreated table is intentionally empty.
"""

from __future__ import annotations

from alembic import op

revision: str = "0179_drop_workspace_leases"
down_revision: str | None = "0178_env_profiles_runner_docker"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        r"""
        DO $$
        DECLARE
            lease_rows bigint;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'workspace_leases'
            ) THEN
                SELECT count(*) INTO lease_rows FROM public.workspace_leases;
                IF lease_rows > 0 THEN
                    RAISE EXCEPTION
                        'Refusing to drop workspace_leases: % lease row(s) present '
                        '(FAR-587 guard). Inspect and clear them deliberately first.',
                        lease_rows;
                END IF;
                DROP TABLE public.workspace_leases;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Table (guarded) — empty recreate with the original 0005/0110 shape.
    op.execute(
        r"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'workspace_leases'
            ) THEN
                CREATE TABLE public.workspace_leases (
                    id uuid NOT NULL,
                    organisation_id uuid NOT NULL,
                    environment_profile_id uuid NOT NULL,
                    run_id uuid NOT NULL,
                    provider_ref character varying(255) NOT NULL,
                    status character varying(30) DEFAULT 'pending'::character varying NOT NULL,
                    repository_url character varying(1000),
                    repository_ref character varying(255),
                    lease_started_at timestamp with time zone,
                    lease_expires_at timestamp with time zone DEFAULT (now() + '00:30:00'::interval) NOT NULL,
                    resource_usage_json json,
                    output_artifact_refs_json json,
                    error_message text,
                    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
                );
            END IF;
        END $$;
        """
    )
    # Primary key.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='workspace_leases_pkey') "
        "THEN ALTER TABLE public.workspace_leases ADD CONSTRAINT workspace_leases_pkey PRIMARY KEY (id); "
        "END IF; END $$;"
    )
    # Status CHECK.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_workspace_leases_status') "
        "THEN ALTER TABLE public.workspace_leases ADD CONSTRAINT ck_workspace_leases_status CHECK "
        "(((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, "
        "'completed'::character varying, 'failed'::character varying, "
        "'expired'::character varying])::text[]))); END IF; END $$;"
    )
    # Foreign keys (RESTRICT — the original delete-protection semantics).
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname='workspace_leases_environment_profile_id_fkey') THEN "
        "ALTER TABLE public.workspace_leases ADD CONSTRAINT "
        "workspace_leases_environment_profile_id_fkey FOREIGN KEY (environment_profile_id) "
        "REFERENCES environment_profiles(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname='workspace_leases_organisation_id_fkey') THEN "
        "ALTER TABLE public.workspace_leases ADD CONSTRAINT "
        "workspace_leases_organisation_id_fkey FOREIGN KEY (organisation_id) "
        "REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname='workspace_leases_run_id_fkey') THEN "
        "ALTER TABLE public.workspace_leases ADD CONSTRAINT "
        "workspace_leases_run_id_fkey FOREIGN KEY (run_id) "
        "REFERENCES runs(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    # Indexes.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_leases_environment_profile_id "
        "ON public.workspace_leases USING btree (environment_profile_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_leases_organisation_id "
        "ON public.workspace_leases USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_workspace_leases_run_id ON public.workspace_leases USING btree (run_id);")
    # Same-org tenant triggers (0110).
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_workspace_leases_environment_profile_id_tenant') "
        "THEN CREATE TRIGGER trg_workspace_leases_environment_profile_id_tenant "
        "BEFORE INSERT OR UPDATE OF environment_profile_id, organisation_id ON public.workspace_leases "
        "FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('environment_profiles', 'environment_profile_id'); "
        "END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_workspace_leases_run_id_tenant') "
        "THEN CREATE TRIGGER trg_workspace_leases_run_id_tenant "
        "BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.workspace_leases "
        "FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id'); "
        "END IF; END $$;"
    )
    # RLS (0110).
    op.execute("ALTER TABLE public.workspace_leases ENABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.workspace_leases;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.workspace_leases USING "
        "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )

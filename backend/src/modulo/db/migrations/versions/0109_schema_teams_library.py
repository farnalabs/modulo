"""Schema reconciliation — Teams, library & schemas reconciliation (library primitives, schemas, parameter schemas/sets, composite templates, saved views, node categories).

Idempotent, data-safe reconciliation that brings any database to the current
schema state for this domain without assuming prior migration history:

- CREATE TABLE/INDEX/SEQUENCE IF NOT EXISTS; ADD COLUMN IF NOT EXISTS
- constraints (PK/FK/UNIQUE/CHECK) added only when absent (pg_constraint guards)
- triggers created only when absent; policies DROP+CREATE (idempotent)
- RLS enablement re-applied; functions CREATE OR REPLACE
- data-safe SET NOT NULL / SET DEFAULT / ALTER TYPE (never over NULL rows)

Safe on fresh databases (after the v2 base) and on existing databases stamped
at the previous revision (no-ops on existing objects; repairs missing ones).

Revision ID: 0109_schema_teams_library
Revises: 0108_schema_org_identity
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0109_schema_teams_library"
down_revision: str | None = "0108_schema_org_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.parameter_schemas ( id uuid NOT NULL, organisation_id uuid NOT NULL, name character varying(255) NOT NULL, description text, version integer DEFAULT 1 NOT NULL, parameters json DEFAULT '[]'::jsonb NOT NULL, created_at timestamp with time zone DEFAULT now() NOT NULL, updated_at timestamp with time zone DEFAULT now() NOT NULL, account_id uuid NOT NULL, deleted_at timestamp with time zone );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.parameter_sets ( id uuid NOT NULL, parameter_schema_id uuid NOT NULL, organisation_id uuid NOT NULL, account_id uuid NOT NULL, version integer DEFAULT 1 NOT NULL, schema_version integer NOT NULL, name character varying(255) NOT NULL, description text, \"values\" json DEFAULT '{}'::jsonb NOT NULL, created_at timestamp with time zone DEFAULT now() NOT NULL, updated_at timestamp with time zone DEFAULT now() NOT NULL, deleted_at timestamp with time zone );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_folders ( id uuid NOT NULL, organisation_id uuid NOT NULL, name character varying(255) NOT NULL, parent_id uuid, sort_order integer DEFAULT 0 NOT NULL, account_id uuid NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL );"
    )
    op.execute('ALTER TABLE public."primitive_ratings" ADD COLUMN IF NOT EXISTS "primitive_id" uuid;')
    op.execute('ALTER TABLE public."primitive_ratings" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."primitive_ratings" ADD COLUMN IF NOT EXISTS "thumbs_up" boolean;')
    op.execute('ALTER TABLE public."primitive_ratings" ADD COLUMN IF NOT EXISTS "comment" text;')
    op.execute('ALTER TABLE public."primitive_ratings" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."primitive_ratings" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."primitive_ratings" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."primitive_ratings" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "primitive_id" uuid;')
    op.execute('ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "rating_id" uuid;')
    op.execute('ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "reporter_account_id" uuid;')
    op.execute('ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "reason" character varying(500);')
    op.execute(
        'ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "status" character varying(20) DEFAULT \'pending\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "reviewed_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "reviewer_account_id" uuid;')
    op.execute('ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."primitive_abuse_reports" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "schema_id" uuid;')
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "version" character varying(50);')
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "version_number" integer;')
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "definition_json" json;')
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "published" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "deprecated" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."schema_versions" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."tier_catalog" ADD COLUMN IF NOT EXISTS "tier_id" character varying(255);')
    op.execute('ALTER TABLE public."tier_catalog" ADD COLUMN IF NOT EXISTS "label" character varying(255);')
    op.execute('ALTER TABLE public."tier_catalog" ADD COLUMN IF NOT EXISTS "rank" integer;')
    op.execute('ALTER TABLE public."tier_catalog" ADD COLUMN IF NOT EXISTS "requires_license" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."tier_catalog" ADD COLUMN IF NOT EXISTS "description" character varying(2000);')
    op.execute('ALTER TABLE public."feature_flag_catalog" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute(
        'ALTER TABLE public."feature_flag_catalog" ADD COLUMN IF NOT EXISTS "description" character varying(2000);'
    )
    op.execute('ALTER TABLE public."feature_flag_catalog" ADD COLUMN IF NOT EXISTS "tier_id" character varying(255);')
    op.execute('ALTER TABLE public."feature_flag_catalog" ADD COLUMN IF NOT EXISTS "depends_on" json;')
    op.execute('ALTER TABLE public."feature_flag_catalog" ADD COLUMN IF NOT EXISTS "is_active" boolean DEFAULT true;')
    op.execute('ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "contact_email" character varying(255);')
    op.execute('ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "public_key_hex" character varying(128);')
    op.execute(
        'ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "trust_tier" character varying(10) DEFAULT \'amber\'::character varying;'
    )
    op.execute('ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "verified_since" timestamp with time zone;')
    op.execute('ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "website_url" character varying(2000);')
    op.execute('ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."publishers" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "user_id" uuid;')
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "name" character varying(100);')
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "description" text;')
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "triggers" json;')
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "body" text;')
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "active" boolean DEFAULT true;')
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "source_mode" character varying(16);')
    op.execute('ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute(
        'ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."remy_skills" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."remy_context_sources" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."remy_context_sources" ADD COLUMN IF NOT EXISTS "user_id" uuid;')
    op.execute('ALTER TABLE public."remy_context_sources" ADD COLUMN IF NOT EXISTS "source_key" character varying(64);')
    op.execute(
        'ALTER TABLE public."remy_context_sources" ADD COLUMN IF NOT EXISTS "source_mode" character varying(16) DEFAULT \'always_on\'::character varying;'
    )
    op.execute('ALTER TABLE public."remy_context_sources" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute(
        'ALTER TABLE public."remy_context_sources" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."remy_context_sources" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "name" character varying(100);')
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "description" text;')
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "color" character varying(7);')
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "icon" character varying(50);')
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "sort_order" integer;')
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."node_categories" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "description" text;')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "sub_pipeline_graph_json" json;')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "parameter_ports_json" json;')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "input_schema_id" uuid;')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "output_schema_id" uuid;')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "version" character varying(50);')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "parameter_schema_id" uuid;')
    op.execute(
        'ALTER TABLE public."composite_templates" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "description" text;')
    op.execute('ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "version" integer DEFAULT 1;')
    op.execute(
        'ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "parameters" json DEFAULT \'[]\'::jsonb;'
    )
    op.execute(
        'ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT now();'
    )
    op.execute(
        'ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT now();'
    )
    op.execute('ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."parameter_schemas" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "parameter_schema_id" uuid;')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "version" integer DEFAULT 1;')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "schema_version" integer;')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "description" text;')
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "values" json DEFAULT \'{}\'::jsonb;')
    op.execute(
        'ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT now();'
    )
    op.execute(
        'ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT now();'
    )
    op.execute('ALTER TABLE public."parameter_sets" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "source" character varying(20);')
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "primitive_type" character varying(20);'
    )
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "slug" character varying(255);')
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "description" character varying(2000);'
    )
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "author" character varying(255);')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "version" character varying(50);')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "tags" json;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "content_json" json;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "source_url" character varying(2000);')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "forked_from" uuid;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "checksum" character varying(128);')
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "ed25519_signature" character varying(256);'
    )
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "verified" boolean;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "category" character varying(50);')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "download_count" integer;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "average_rating" numeric(3,2);')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "review_count" integer;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "owner_team_id" uuid;')
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "visibility" character varying(10) DEFAULT \'org\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "contribution_status" character varying(20);'
    )
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "auto_update" boolean DEFAULT true;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "version_group_id" uuid;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "update_available_version_id" uuid;')
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "tier" character varying(20) DEFAULT \'native\'::character varying;'
    )
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."library_primitives" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "description" character varying(2000);')
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "abstract_name" character varying(255);')
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "deprecated" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "deprecated_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "system" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."schemas" ADD COLUMN IF NOT EXISTS "folder_id" uuid;')
    op.execute('ALTER TABLE public."schema_folders" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."schema_folders" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."schema_folders" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."schema_folders" ADD COLUMN IF NOT EXISTS "parent_id" uuid;')
    op.execute('ALTER TABLE public."schema_folders" ADD COLUMN IF NOT EXISTS "sort_order" integer DEFAULT 0;')
    op.execute('ALTER TABLE public."schema_folders" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute(
        'ALTER TABLE public."schema_folders" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."schema_folders" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_ratings' AND column_name='primitive_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_ratings\" WHERE \"primitive_id\" IS NULL) THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"primitive_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_ratings' AND column_name='thumbs_up' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_ratings\" WHERE \"thumbs_up\" IS NULL) THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"thumbs_up\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_ratings' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_ratings\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_ratings' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_ratings\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_ratings' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_ratings\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_ratings' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_ratings\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='primitive_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_abuse_reports\" WHERE \"primitive_id\" IS NULL) THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"primitive_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='reason' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_abuse_reports\" WHERE \"reason\" IS NULL) THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"reason\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_abuse_reports\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_abuse_reports\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_abuse_reports\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_abuse_reports\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"primitive_abuse_reports\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='schema_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"schema_id\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"schema_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"version\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='version_number' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"version_number\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"version_number\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='definition_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"definition_json\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"definition_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='published' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"published\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"published\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='deprecated' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"deprecated\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"deprecated\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_versions' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_versions\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tier_catalog' AND column_name='tier_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"tier_catalog\" WHERE \"tier_id\" IS NULL) THEN ALTER TABLE public.\"tier_catalog\" ALTER COLUMN \"tier_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tier_catalog' AND column_name='label' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"tier_catalog\" WHERE \"label\" IS NULL) THEN ALTER TABLE public.\"tier_catalog\" ALTER COLUMN \"label\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tier_catalog' AND column_name='rank' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"tier_catalog\" WHERE \"rank\" IS NULL) THEN ALTER TABLE public.\"tier_catalog\" ALTER COLUMN \"rank\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tier_catalog' AND column_name='requires_license' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"tier_catalog\" WHERE \"requires_license\" IS NULL) THEN ALTER TABLE public.\"tier_catalog\" ALTER COLUMN \"requires_license\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feature_flag_catalog' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feature_flag_catalog\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"feature_flag_catalog\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feature_flag_catalog' AND column_name='tier_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feature_flag_catalog\" WHERE \"tier_id\" IS NULL) THEN ALTER TABLE public.\"feature_flag_catalog\" ALTER COLUMN \"tier_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feature_flag_catalog' AND column_name='is_active' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feature_flag_catalog\" WHERE \"is_active\" IS NULL) THEN ALTER TABLE public.\"feature_flag_catalog\" ALTER COLUMN \"is_active\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"publishers\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='public_key_hex' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"publishers\" WHERE \"public_key_hex\" IS NULL) THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"public_key_hex\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='trust_tier' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"publishers\" WHERE \"trust_tier\" IS NULL) THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"trust_tier\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"publishers\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"publishers\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"publishers\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"publishers\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_skills\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='body' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_skills\" WHERE \"body\" IS NULL) THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"body\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='active' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_skills\" WHERE \"active\" IS NULL) THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"active\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_skills\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_skills\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_skills\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_context_sources' AND column_name='source_key' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_context_sources\" WHERE \"source_key\" IS NULL) THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"source_key\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_context_sources' AND column_name='source_mode' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_context_sources\" WHERE \"source_mode\" IS NULL) THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"source_mode\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_context_sources' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_context_sources\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_context_sources' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_context_sources\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_context_sources' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"remy_context_sources\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_categories\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='color' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_categories\" WHERE \"color\" IS NULL) THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"color\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='sort_order' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_categories\" WHERE \"sort_order\" IS NULL) THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"sort_order\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_categories\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_categories\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_categories\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_categories\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_categories\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='sub_pipeline_graph_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"sub_pipeline_graph_json\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"sub_pipeline_graph_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='parameter_ports_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"parameter_ports_json\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"parameter_ports_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"version\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"composite_templates\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_schemas\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_schemas\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_schemas\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_schemas\" WHERE \"version\" IS NULL) THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='parameters' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_schemas\" WHERE \"parameters\" IS NULL) THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"parameters\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_schemas\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_schemas\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_schemas\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='parameter_schema_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"parameter_schema_id\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"parameter_schema_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"version\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='schema_version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"schema_version\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"schema_version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='values' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"values\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"values\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"parameter_sets\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='source' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"source\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"source\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='primitive_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"primitive_type\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"primitive_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='slug' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"slug\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"slug\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='author' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"author\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"author\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"version\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='tags' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"tags\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"tags\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='content_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"content_json\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"content_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='visibility' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"visibility\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"visibility\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='auto_update' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"auto_update\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"auto_update\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='tier' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"tier\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"tier\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"library_primitives\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schemas\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schemas\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='deprecated' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schemas\" WHERE \"deprecated\" IS NULL) THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"deprecated\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schemas\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schemas\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schemas\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schemas\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='system' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schemas\" WHERE \"system\" IS NULL) THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"system\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_folders' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_folders\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_folders' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_folders\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_folders' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_folders\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_folders' AND column_name='sort_order' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_folders\" WHERE \"sort_order\" IS NULL) THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"sort_order\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_folders' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_folders\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_folders' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_folders\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_folders' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"schema_folders\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='composite_templates' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='composite_templates' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='feature_flag_catalog' AND a.attname='is_active' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"feature_flag_catalog\" ALTER COLUMN \"is_active\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='library_primitives' AND a.attname='auto_update' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"auto_update\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='library_primitives' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='library_primitives' AND a.attname='tier' AND pg_get_expr(ad.adbin, ad.adrelid) = '''native''::character varying') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"tier\" SET DEFAULT 'native'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='library_primitives' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='library_primitives' AND a.attname='visibility' AND pg_get_expr(ad.adbin, ad.adrelid) = '''org''::character varying') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"visibility\" SET DEFAULT 'org'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='node_categories' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='node_categories' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='parameter_schemas' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'now()') THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"created_at\" SET DEFAULT now(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='parameter_schemas' AND a.attname='parameters' AND pg_get_expr(ad.adbin, ad.adrelid) = '''[]''::jsonb') THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"parameters\" SET DEFAULT '[]'::jsonb; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='parameter_schemas' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'now()') THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"updated_at\" SET DEFAULT now(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='parameter_schemas' AND a.attname='version' AND pg_get_expr(ad.adbin, ad.adrelid) = '1') THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"version\" SET DEFAULT 1; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='parameter_sets' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'now()') THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"created_at\" SET DEFAULT now(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='parameter_sets' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'now()') THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"updated_at\" SET DEFAULT now(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='parameter_sets' AND a.attname='values' AND pg_get_expr(ad.adbin, ad.adrelid) = '''{}''::jsonb') THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"values\" SET DEFAULT '{}'::jsonb; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='parameter_sets' AND a.attname='version' AND pg_get_expr(ad.adbin, ad.adrelid) = '1') THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"version\" SET DEFAULT 1; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='primitive_abuse_reports' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='primitive_abuse_reports' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''pending''::character varying') THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"status\" SET DEFAULT 'pending'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='primitive_abuse_reports' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='primitive_ratings' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='primitive_ratings' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='publishers' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='publishers' AND a.attname='trust_tier' AND pg_get_expr(ad.adbin, ad.adrelid) = '''amber''::character varying') THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"trust_tier\" SET DEFAULT 'amber'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='publishers' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='remy_context_sources' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='remy_context_sources' AND a.attname='source_mode' AND pg_get_expr(ad.adbin, ad.adrelid) = '''always_on''::character varying') THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"source_mode\" SET DEFAULT 'always_on'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='remy_context_sources' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='remy_skills' AND a.attname='active' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"active\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='remy_skills' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='remy_skills' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schema_folders' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schema_folders' AND a.attname='sort_order' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"sort_order\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schema_folders' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schema_versions' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schema_versions' AND a.attname='deprecated' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"deprecated\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schema_versions' AND a.attname='published' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"published\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schema_versions' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"schema_versions\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schemas' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schemas' AND a.attname='deprecated' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"deprecated\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schemas' AND a.attname='system' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"system\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='schemas' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='tier_catalog' AND a.attname='requires_license' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"tier_catalog\" ALTER COLUMN \"requires_license\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='input_schema_id' AND is_nullable='NO') THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"input_schema_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='output_schema_id' AND is_nullable='NO') THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"output_schema_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='composite_templates' AND column_name='parameter_schema_id' AND is_nullable='NO') THEN ALTER TABLE public.\"composite_templates\" ALTER COLUMN \"parameter_schema_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feature_flag_catalog' AND column_name='depends_on' AND is_nullable='NO') THEN ALTER TABLE public.\"feature_flag_catalog\" ALTER COLUMN \"depends_on\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feature_flag_catalog' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"feature_flag_catalog\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='average_rating' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"average_rating\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='category' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"category\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='checksum' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"checksum\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='contribution_status' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"contribution_status\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='download_count' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"download_count\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='ed25519_signature' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"ed25519_signature\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='forked_from' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"forked_from\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='owner_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"owner_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='review_count' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"review_count\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='source_url' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"source_url\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='update_available_version_id' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"update_available_version_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='verified' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"verified\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='library_primitives' AND column_name='version_group_id' AND is_nullable='NO') THEN ALTER TABLE public.\"library_primitives\" ALTER COLUMN \"version_group_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_categories' AND column_name='icon' AND is_nullable='NO') THEN ALTER TABLE public.\"node_categories\" ALTER COLUMN \"icon\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_schemas' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"parameter_schemas\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='parameter_sets' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"parameter_sets\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='rating_id' AND is_nullable='NO') THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"rating_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='reporter_account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"reporter_account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='reviewed_at' AND is_nullable='NO') THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"reviewed_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_abuse_reports' AND column_name='reviewer_account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"primitive_abuse_reports\" ALTER COLUMN \"reviewer_account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_ratings' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='primitive_ratings' AND column_name='comment' AND is_nullable='NO') THEN ALTER TABLE public.\"primitive_ratings\" ALTER COLUMN \"comment\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='contact_email' AND is_nullable='NO') THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"contact_email\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='verified_since' AND is_nullable='NO') THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"verified_since\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='publishers' AND column_name='website_url' AND is_nullable='NO') THEN ALTER TABLE public.\"publishers\" ALTER COLUMN \"website_url\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_context_sources' AND column_name='organisation_id' AND is_nullable='NO') THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"organisation_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_context_sources' AND column_name='user_id' AND is_nullable='NO') THEN ALTER TABLE public.\"remy_context_sources\" ALTER COLUMN \"user_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='organisation_id' AND is_nullable='NO') THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"organisation_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='source_mode' AND is_nullable='NO') THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"source_mode\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='triggers' AND is_nullable='NO') THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"triggers\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='remy_skills' AND column_name='user_id' AND is_nullable='NO') THEN ALTER TABLE public.\"remy_skills\" ALTER COLUMN \"user_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_folders' AND column_name='parent_id' AND is_nullable='NO') THEN ALTER TABLE public.\"schema_folders\" ALTER COLUMN \"parent_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='abstract_name' AND is_nullable='NO') THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"abstract_name\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='deprecated_at' AND is_nullable='NO') THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"deprecated_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schemas' AND column_name='folder_id' AND is_nullable='NO') THEN ALTER TABLE public.\"schemas\" ALTER COLUMN \"folder_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tier_catalog' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"tier_catalog\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_composite_templates_organisation_id ON public.composite_templates USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_composite_templates_parameter_schema_id ON public.composite_templates USING btree (parameter_schema_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_library_primitives_organisation_id ON public.library_primitives USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_node_categories_organisation_id ON public.node_categories USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_parameter_schemas_organisation_id ON public.parameter_schemas USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_parameter_sets_organisation_id ON public.parameter_sets USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_parameter_sets_parameter_schema_id ON public.parameter_sets USING btree (parameter_schema_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_primitive_abuse_reports_organisation_id ON public.primitive_abuse_reports USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_primitive_abuse_reports_primitive_id ON public.primitive_abuse_reports USING btree (primitive_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_primitive_ratings_account_id ON public.primitive_ratings USING btree (account_id) WHERE (account_id IS NOT NULL);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_primitive_ratings_organisation_id ON public.primitive_ratings USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_primitive_ratings_primitive_id ON public.primitive_ratings USING btree (primitive_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_publishers_organisation_id ON public.publishers USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_publishers_public_key_hex ON public.publishers USING btree (public_key_hex);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_remy_context_sources_organisation_id ON public.remy_context_sources USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_remy_skills_organisation_id ON public.remy_skills USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_remy_skills_user_id ON public.remy_skills USING btree (user_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_schema_folders_organisation_id ON public.schema_folders USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_schema_folders_parent_id ON public.schema_folders USING btree (parent_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_schema_versions_organisation_id ON public.schema_versions USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_schema_versions_schema_id ON public.schema_versions USING btree (schema_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_schemas_folder_id ON public.schemas USING btree (folder_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_schemas_organisation_id ON public.schemas USING btree (organisation_id);")
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_composite_templates_account_id_tenant') THEN CREATE TRIGGER trg_composite_templates_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.composite_templates FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_library_primitives_account_id_tenant') THEN CREATE TRIGGER trg_library_primitives_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_library_primitives_fork_provenance') THEN CREATE TRIGGER trg_library_primitives_fork_provenance BEFORE INSERT OR UPDATE OF forked_from ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_library_fork_provenance(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_library_primitives_forked_from_tenant') THEN CREATE TRIGGER trg_library_primitives_forked_from_tenant BEFORE INSERT OR UPDATE OF forked_from, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'forked_from'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_library_primitives_owner_team_id_tenant') THEN CREATE TRIGGER trg_library_primitives_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_library_primitives_update_available_version_id_tenant') THEN CREATE TRIGGER trg_library_primitives_update_available_version_id_tenant BEFORE INSERT OR UPDATE OF update_available_version_id, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'update_available_version_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_node_categories_account_id_tenant') THEN CREATE TRIGGER trg_node_categories_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.node_categories FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_primitive_abuse_reports_primitive_id_tenant') THEN CREATE TRIGGER trg_primitive_abuse_reports_primitive_id_tenant BEFORE INSERT OR UPDATE OF primitive_id, organisation_id ON public.primitive_abuse_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'primitive_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_primitive_abuse_reports_rating_id_tenant') THEN CREATE TRIGGER trg_primitive_abuse_reports_rating_id_tenant BEFORE INSERT OR UPDATE OF rating_id, organisation_id ON public.primitive_abuse_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('primitive_ratings', 'rating_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_primitive_abuse_reports_reporter_account_id_tenant') THEN CREATE TRIGGER trg_primitive_abuse_reports_reporter_account_id_tenant BEFORE INSERT OR UPDATE OF reporter_account_id, organisation_id ON public.primitive_abuse_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'reporter_account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_primitive_abuse_reports_reviewer_account_id_tenant') THEN CREATE TRIGGER trg_primitive_abuse_reports_reviewer_account_id_tenant BEFORE INSERT OR UPDATE OF reviewer_account_id, organisation_id ON public.primitive_abuse_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'reviewer_account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_primitive_ratings_account_id_tenant') THEN CREATE TRIGGER trg_primitive_ratings_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.primitive_ratings FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_primitive_ratings_primitive_id_tenant') THEN CREATE TRIGGER trg_primitive_ratings_primitive_id_tenant BEFORE INSERT OR UPDATE OF primitive_id, organisation_id ON public.primitive_ratings FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'primitive_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_remy_context_sources_user_id_tenant') THEN CREATE TRIGGER trg_remy_context_sources_user_id_tenant BEFORE INSERT OR UPDATE OF user_id, organisation_id ON public.remy_context_sources FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'user_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_remy_skills_user_id_tenant') THEN CREATE TRIGGER trg_remy_skills_user_id_tenant BEFORE INSERT OR UPDATE OF user_id, organisation_id ON public.remy_skills FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'user_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_schema_versions_account_id_tenant') THEN CREATE TRIGGER trg_schema_versions_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.schema_versions FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_schema_versions_schema_id_tenant') THEN CREATE TRIGGER trg_schema_versions_schema_id_tenant BEFORE INSERT OR UPDATE OF schema_id, organisation_id ON public.schema_versions FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('schemas', 'schema_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_schemas_account_id_tenant') THEN CREATE TRIGGER trg_schemas_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.schemas FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute("ALTER TABLE public.composite_templates ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.library_primitives ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.node_categories ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.parameter_schemas ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.parameter_sets ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.primitive_abuse_reports ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.primitive_ratings ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.publishers ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.remy_context_sources ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.remy_skills ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.schema_folders ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.schema_versions ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.schemas ENABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.composite_templates;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.composite_templates USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.library_primitives;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.library_primitives USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.node_categories;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.node_categories USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.parameter_schemas;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.parameter_schemas USING ((organisation_id = (current_setting('app.organisation_id'::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.parameter_sets;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.parameter_sets USING ((organisation_id = (current_setting('app.organisation_id'::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.primitive_abuse_reports;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.primitive_abuse_reports USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.primitive_ratings;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.primitive_ratings USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.publishers;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.publishers USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.remy_context_sources;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.remy_context_sources USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (organisation_id IS NULL)));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.remy_skills;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.remy_skills USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (organisation_id IS NULL)));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.schema_folders;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.schema_folders USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.schema_versions;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.schema_versions USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.schemas;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.schemas USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation_null_context ON public.parameter_schemas;")
    op.execute(
        "CREATE POLICY rls_org_isolation_null_context ON public.parameter_schemas USING (((current_setting('app.organisation_id'::text, true) IS NULL) OR (organisation_id = (current_setting('app.organisation_id'::text))::uuid)));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation_null_context ON public.parameter_sets;")
    op.execute(
        "CREATE POLICY rls_org_isolation_null_context ON public.parameter_sets USING (((current_setting('app.organisation_id'::text, true) IS NULL) OR (organisation_id = (current_setting('app.organisation_id'::text))::uuid)));"
    )
    op.execute("DROP POLICY IF EXISTS rls_team_isolation ON public.library_primitives;")
    op.execute(
        "CREATE POLICY rls_team_isolation ON public.library_primitives USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id FROM public.team_memberships WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text))));"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='composite_templates_pkey') THEN ALTER TABLE public.composite_templates ADD CONSTRAINT composite_templates_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='feature_flag_catalog_pkey') THEN ALTER TABLE public.feature_flag_catalog ADD CONSTRAINT feature_flag_catalog_pkey PRIMARY KEY (name); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='library_primitives_pkey') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT library_primitives_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='node_categories_pkey') THEN ALTER TABLE public.node_categories ADD CONSTRAINT node_categories_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parameter_schemas_pkey') THEN ALTER TABLE public.parameter_schemas ADD CONSTRAINT parameter_schemas_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parameter_sets_pkey') THEN ALTER TABLE public.parameter_sets ADD CONSTRAINT parameter_sets_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_abuse_reports_pkey') THEN ALTER TABLE public.primitive_abuse_reports ADD CONSTRAINT primitive_abuse_reports_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_ratings_pkey') THEN ALTER TABLE public.primitive_ratings ADD CONSTRAINT primitive_ratings_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='publishers_pkey') THEN ALTER TABLE public.publishers ADD CONSTRAINT publishers_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='remy_context_sources_pkey') THEN ALTER TABLE public.remy_context_sources ADD CONSTRAINT remy_context_sources_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='remy_skills_pkey') THEN ALTER TABLE public.remy_skills ADD CONSTRAINT remy_skills_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schema_folders_pkey') THEN ALTER TABLE public.schema_folders ADD CONSTRAINT schema_folders_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schema_versions_pkey') THEN ALTER TABLE public.schema_versions ADD CONSTRAINT schema_versions_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schemas_pkey') THEN ALTER TABLE public.schemas ADD CONSTRAINT schemas_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='tier_catalog_pkey') THEN ALTER TABLE public.tier_catalog ADD CONSTRAINT tier_catalog_pkey PRIMARY KEY (tier_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_library_primitive_version') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT uq_library_primitive_version UNIQUE (organisation_id, source, slug, version); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_node_categories_org_name') THEN ALTER TABLE public.node_categories ADD CONSTRAINT uq_node_categories_org_name UNIQUE (organisation_id, name); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_parameter_schemas_org_name') THEN ALTER TABLE public.parameter_schemas ADD CONSTRAINT uq_parameter_schemas_org_name UNIQUE (organisation_id, name); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_parameter_sets_schema_name') THEN ALTER TABLE public.parameter_sets ADD CONSTRAINT uq_parameter_sets_schema_name UNIQUE (parameter_schema_id, name); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_ratings_per_user') THEN ALTER TABLE public.primitive_ratings ADD CONSTRAINT uq_ratings_per_user UNIQUE (organisation_id, primitive_id, account_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_publishers_org_key') THEN ALTER TABLE public.publishers ADD CONSTRAINT uq_publishers_org_key UNIQUE (organisation_id, public_key_hex); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_publishers_org_name') THEN ALTER TABLE public.publishers ADD CONSTRAINT uq_publishers_org_name UNIQUE (organisation_id, name); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_remy_context_sources_key') THEN ALTER TABLE public.remy_context_sources ADD CONSTRAINT uq_remy_context_sources_key UNIQUE (organisation_id, user_id, source_key); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_schema_versions_schema_version_organisation') THEN ALTER TABLE public.schema_versions ADD CONSTRAINT uq_schema_versions_schema_version_organisation UNIQUE (schema_id, version, organisation_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_schemas_organisation_name') THEN ALTER TABLE public.schemas ADD CONSTRAINT uq_schemas_organisation_name UNIQUE (organisation_id, name); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_contribution_status') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT ck_library_primitives_contribution_status CHECK (((contribution_status)::text = ANY ((ARRAY['draft'::character varying, 'review_queue'::character varying, 'published'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_rating') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT ck_library_primitives_rating CHECK (((average_rating IS NULL) OR ((average_rating >= (1)::numeric) AND (average_rating <= (5)::numeric)))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_source') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT ck_library_primitives_source CHECK (((source)::text = ANY ((ARRAY['local'::character varying, 'registry'::character varying, 'modulo'::character varying, 'community'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_source_fields') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT ck_library_primitives_source_fields CHECK (((((source)::text = 'local'::text) AND (source_url IS NULL) AND (checksum IS NULL) AND (ed25519_signature IS NULL) AND (verified IS NULL) AND (download_count IS NULL) AND (average_rating IS NULL) AND (review_count IS NULL)) OR (((source)::text = 'modulo'::text) AND (source_url IS NULL) AND (checksum IS NULL) AND (ed25519_signature IS NULL) AND (verified IS NULL) AND (download_count IS NULL) AND (average_rating IS NULL) AND (review_count IS NULL)) OR (((source)::text = 'community'::text) AND (source_url IS NULL) AND (checksum IS NULL) AND (ed25519_signature IS NULL) AND (download_count IS NULL) AND (average_rating IS NULL) AND (review_count IS NULL)) OR (((source)::text = 'registry'::text) AND (owner_team_id IS NULL) AND ((visibility)::text = 'org'::text) AND (forked_from IS NULL) AND (source_url IS NOT NULL) AND (checksum IS NOT NULL) AND (ed25519_signature IS NOT NULL) AND (verified IS NOT NULL) AND (download_count IS NOT NULL) AND (average_rating IS NOT NULL) AND (review_count IS NOT NULL)))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_team_owner') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT ck_library_primitives_team_owner CHECK ((((visibility)::text = ANY ((ARRAY['org'::character varying, 'community'::character varying])::text[])) OR (owner_team_id IS NOT NULL))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_tier') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT ck_library_primitives_tier CHECK (((tier)::text = ANY ((ARRAY['native'::character varying, 'preview'::character varying, 'in_dev'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_type') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT ck_library_primitives_type CHECK (((primitive_type)::text = ANY ((ARRAY['schema'::character varying, 'workflow'::character varying, 'agent'::character varying, 'integration'::character varying, 'test_fixture'::character varying, 'pipeline_template'::character varying, 'composite'::character varying, 'lifecycle_map'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_library_primitives_visibility') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT ck_library_primitives_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying, 'community'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_abuse_reports_status') THEN ALTER TABLE public.primitive_abuse_reports ADD CONSTRAINT ck_abuse_reports_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'reviewed'::character varying, 'dismissed'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_remy_context_sources_mode') THEN ALTER TABLE public.remy_context_sources ADD CONSTRAINT ck_remy_context_sources_mode CHECK (((source_mode)::text = ANY ((ARRAY['always_on'::character varying, 'tool'::character varying, 'off'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_remy_context_sources_owner') THEN ALTER TABLE public.remy_context_sources ADD CONSTRAINT ck_remy_context_sources_owner CHECK ((((organisation_id IS NOT NULL) AND (user_id IS NULL)) OR ((organisation_id IS NULL) AND (user_id IS NOT NULL)))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_remy_skills_owner') THEN ALTER TABLE public.remy_skills ADD CONSTRAINT ck_remy_skills_owner CHECK ((((organisation_id IS NOT NULL) AND (user_id IS NULL)) OR ((organisation_id IS NULL) AND (user_id IS NOT NULL)))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='composite_templates_account_id_fkey') THEN ALTER TABLE public.composite_templates ADD CONSTRAINT composite_templates_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='composite_templates_organisation_id_fkey') THEN ALTER TABLE public.composite_templates ADD CONSTRAINT composite_templates_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='composite_templates_parameter_schema_id_fkey') THEN ALTER TABLE public.composite_templates ADD CONSTRAINT composite_templates_parameter_schema_id_fkey FOREIGN KEY (parameter_schema_id) REFERENCES parameter_schemas(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_composite_templates_input_schema_id') THEN ALTER TABLE public.composite_templates ADD CONSTRAINT fk_composite_templates_input_schema_id FOREIGN KEY (input_schema_id) REFERENCES schemas(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_composite_templates_output_schema_id') THEN ALTER TABLE public.composite_templates ADD CONSTRAINT fk_composite_templates_output_schema_id FOREIGN KEY (output_schema_id) REFERENCES schemas(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='feature_flag_catalog_tier_id_fkey') THEN ALTER TABLE public.feature_flag_catalog ADD CONSTRAINT feature_flag_catalog_tier_id_fkey FOREIGN KEY (tier_id) REFERENCES tier_catalog(tier_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='library_primitives_account_id_fkey') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT library_primitives_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='library_primitives_forked_from_fkey') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT library_primitives_forked_from_fkey FOREIGN KEY (forked_from) REFERENCES library_primitives(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='library_primitives_organisation_id_fkey') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT library_primitives_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='library_primitives_owner_team_id_fkey') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT library_primitives_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='library_primitives_update_available_version_id_fkey') THEN ALTER TABLE public.library_primitives ADD CONSTRAINT library_primitives_update_available_version_id_fkey FOREIGN KEY (update_available_version_id) REFERENCES library_primitives(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='node_categories_account_id_fkey') THEN ALTER TABLE public.node_categories ADD CONSTRAINT node_categories_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='node_categories_organisation_id_fkey') THEN ALTER TABLE public.node_categories ADD CONSTRAINT node_categories_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parameter_schemas_account_id_fkey') THEN ALTER TABLE public.parameter_schemas ADD CONSTRAINT parameter_schemas_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parameter_schemas_organisation_id_fkey') THEN ALTER TABLE public.parameter_schemas ADD CONSTRAINT parameter_schemas_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parameter_sets_account_id_fkey') THEN ALTER TABLE public.parameter_sets ADD CONSTRAINT parameter_sets_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parameter_sets_organisation_id_fkey') THEN ALTER TABLE public.parameter_sets ADD CONSTRAINT parameter_sets_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parameter_sets_parameter_schema_id_fkey') THEN ALTER TABLE public.parameter_sets ADD CONSTRAINT parameter_sets_parameter_schema_id_fkey FOREIGN KEY (parameter_schema_id) REFERENCES parameter_schemas(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_abuse_reports_organisation_id_fkey') THEN ALTER TABLE public.primitive_abuse_reports ADD CONSTRAINT primitive_abuse_reports_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_abuse_reports_primitive_id_fkey') THEN ALTER TABLE public.primitive_abuse_reports ADD CONSTRAINT primitive_abuse_reports_primitive_id_fkey FOREIGN KEY (primitive_id) REFERENCES library_primitives(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_abuse_reports_rating_id_fkey') THEN ALTER TABLE public.primitive_abuse_reports ADD CONSTRAINT primitive_abuse_reports_rating_id_fkey FOREIGN KEY (rating_id) REFERENCES primitive_ratings(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_abuse_reports_reporter_account_id_fkey') THEN ALTER TABLE public.primitive_abuse_reports ADD CONSTRAINT primitive_abuse_reports_reporter_account_id_fkey FOREIGN KEY (reporter_account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_abuse_reports_reviewer_account_id_fkey') THEN ALTER TABLE public.primitive_abuse_reports ADD CONSTRAINT primitive_abuse_reports_reviewer_account_id_fkey FOREIGN KEY (reviewer_account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_ratings_account_id_fkey') THEN ALTER TABLE public.primitive_ratings ADD CONSTRAINT primitive_ratings_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_ratings_organisation_id_fkey') THEN ALTER TABLE public.primitive_ratings ADD CONSTRAINT primitive_ratings_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='primitive_ratings_primitive_id_fkey') THEN ALTER TABLE public.primitive_ratings ADD CONSTRAINT primitive_ratings_primitive_id_fkey FOREIGN KEY (primitive_id) REFERENCES library_primitives(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='publishers_organisation_id_fkey') THEN ALTER TABLE public.publishers ADD CONSTRAINT publishers_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='remy_context_sources_organisation_id_fkey') THEN ALTER TABLE public.remy_context_sources ADD CONSTRAINT remy_context_sources_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='remy_context_sources_user_id_fkey') THEN ALTER TABLE public.remy_context_sources ADD CONSTRAINT remy_context_sources_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='remy_skills_organisation_id_fkey') THEN ALTER TABLE public.remy_skills ADD CONSTRAINT remy_skills_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='remy_skills_user_id_fkey') THEN ALTER TABLE public.remy_skills ADD CONSTRAINT remy_skills_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schema_folders_account_id_fkey') THEN ALTER TABLE public.schema_folders ADD CONSTRAINT schema_folders_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schema_folders_organisation_id_fkey') THEN ALTER TABLE public.schema_folders ADD CONSTRAINT schema_folders_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schema_folders_parent_id_fkey') THEN ALTER TABLE public.schema_folders ADD CONSTRAINT schema_folders_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES schema_folders(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schema_versions_account_id_fkey') THEN ALTER TABLE public.schema_versions ADD CONSTRAINT schema_versions_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schema_versions_organisation_id_fkey') THEN ALTER TABLE public.schema_versions ADD CONSTRAINT schema_versions_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schema_versions_schema_id_fkey') THEN ALTER TABLE public.schema_versions ADD CONSTRAINT schema_versions_schema_id_fkey FOREIGN KEY (schema_id) REFERENCES schemas(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_schemas_folder_id') THEN ALTER TABLE public.schemas ADD CONSTRAINT fk_schemas_folder_id FOREIGN KEY (folder_id) REFERENCES schema_folders(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schemas_account_id_fkey') THEN ALTER TABLE public.schemas ADD CONSTRAINT schemas_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='schemas_organisation_id_fkey') THEN ALTER TABLE public.schemas ADD CONSTRAINT schemas_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )


def downgrade() -> None:
    """Downgrade is a no-op: schema reconciliation is not reversible in general."""

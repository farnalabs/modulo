"""Schema reconciliation — Org & identity schema reconciliation (accounts, organisations, memberships, teams, SSO/OAuth, break-glass, audit, API keys) + shared functions/grants.

Idempotent, data-safe reconciliation that brings any database to the current
schema state for this domain without assuming prior migration history:

- CREATE TABLE/INDEX/SEQUENCE IF NOT EXISTS; ADD COLUMN IF NOT EXISTS
- constraints (PK/FK/UNIQUE/CHECK) added only when absent (pg_constraint guards)
- triggers created only when absent; policies DROP+CREATE (idempotent)
- RLS enablement re-applied; functions CREATE OR REPLACE
- data-safe SET NOT NULL / SET DEFAULT / ALTER TYPE (never over NULL rows)

Safe on fresh databases (after the v2 base) and on existing databases stamped
at the previous revision (no-ops on existing objects; repairs missing ones).

Revision ID: 0108_schema_org_identity
Revises: 0005_v2_features_system
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0108_schema_org_identity"
down_revision: str | None = "0005_v2_features_system"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS modulo_internal;")
    op.execute("DROP TABLE IF EXISTS public.stages CASCADE;")
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.deleted_defaults ( id uuid NOT NULL, organisation_id uuid NOT NULL, signal character varying(100) NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.oauth_consent_states ( state character varying(128) NOT NULL, client_id character varying(64) NOT NULL, redirect_uri character varying(1024) NOT NULL, scopes json NOT NULL, code_challenge character varying(128) NOT NULL, organisation_id uuid NOT NULL, expires_at timestamp with time zone NOT NULL, consumed boolean DEFAULT false NOT NULL, account_id uuid, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.onboarding_progress ( id uuid DEFAULT gen_random_uuid() NOT NULL, organisation_id uuid NOT NULL, completed_actions character varying[] DEFAULT '{}'::character varying[] NOT NULL, skipped_actions character varying[] DEFAULT '{}'::character varying[] NOT NULL, dismissed boolean DEFAULT false NOT NULL, created_at timestamp with time zone DEFAULT now() NOT NULL, updated_at timestamp with time zone DEFAULT now() NOT NULL );"
    )
    op.execute('ALTER TABLE public."alembic_version" ADD COLUMN IF NOT EXISTS "version_num" character varying(255);')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "provider_type" character varying(16);')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "client_id" character varying(1024);')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "client_secret" bytea;')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "discovery_url" character varying(2048);')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "metadata_url" character varying(2048);')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "metadata_xml" text;')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "entity_id" character varying(1024);')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "scopes" text;')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "enabled" boolean DEFAULT true;')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "auto_provision" boolean DEFAULT true;')
    op.execute(
        'ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "default_role" character varying(32) DEFAULT \'runner\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "group_mappings" json DEFAULT \'[]\'::json;'
    )
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."sso_providers" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."oauth_token_families" ADD COLUMN IF NOT EXISTS "family_id" uuid;')
    op.execute('ALTER TABLE public."oauth_token_families" ADD COLUMN IF NOT EXISTS "client_id" character varying(64);')
    op.execute('ALTER TABLE public."oauth_token_families" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."oauth_token_families" ADD COLUMN IF NOT EXISTS "max_sequence" integer;')
    op.execute('ALTER TABLE public."oauth_token_families" ADD COLUMN IF NOT EXISTS "is_blacklisted" boolean;')
    op.execute(
        'ALTER TABLE public."oauth_token_families" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."oauth_token_families" ADD COLUMN IF NOT EXISTS "blacklisted_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "client_id" character varying(64);')
    op.execute(
        'ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "client_secret_hash" character varying(128);'
    )
    op.execute('ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "scopes" text;')
    op.execute('ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "redirect_uris" text;')
    op.execute('ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."oauth_clients" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."team_memberships" ADD COLUMN IF NOT EXISTS "team_id" uuid;')
    op.execute('ALTER TABLE public."team_memberships" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."team_memberships" ADD COLUMN IF NOT EXISTS "role" character varying(20);')
    op.execute('ALTER TABLE public."team_memberships" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."team_memberships" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."team_memberships" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."team_memberships" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "resource_type" character varying(50);')
    op.execute('ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "resource_id" uuid;')
    op.execute('ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "token_hash" character varying(64);')
    op.execute('ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "expires_at" timestamp with time zone;')
    op.execute(
        'ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "completed_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "created_by" uuid;')
    op.execute('ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."mcp_setup_tokens" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."secrets" ADD COLUMN IF NOT EXISTS "key" character varying(255);')
    op.execute('ALTER TABLE public."secrets" ADD COLUMN IF NOT EXISTS "encrypted_value" bytea;')
    op.execute('ALTER TABLE public."secrets" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."secrets" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."secrets" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."secrets" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."system_config" ADD COLUMN IF NOT EXISTS "key" character varying(255);')
    op.execute('ALTER TABLE public."system_config" ADD COLUMN IF NOT EXISTS "value" json;')
    op.execute(
        'ALTER TABLE public."system_config" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."system_config" ADD COLUMN IF NOT EXISTS "updated_by" uuid;')
    op.execute('ALTER TABLE public."system_config" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."onboarding_progress" ADD COLUMN IF NOT EXISTS "id" uuid DEFAULT gen_random_uuid();')
    op.execute('ALTER TABLE public."onboarding_progress" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."onboarding_progress" ADD COLUMN IF NOT EXISTS "completed_actions" character varying[] DEFAULT \'{}\'::character varying[];'
    )
    op.execute(
        'ALTER TABLE public."onboarding_progress" ADD COLUMN IF NOT EXISTS "skipped_actions" character varying[] DEFAULT \'{}\'::character varying[];'
    )
    op.execute('ALTER TABLE public."onboarding_progress" ADD COLUMN IF NOT EXISTS "dismissed" boolean DEFAULT false;')
    op.execute(
        'ALTER TABLE public."onboarding_progress" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT now();'
    )
    op.execute(
        'ALTER TABLE public."onboarding_progress" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT now();'
    )
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "description" text;')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "view_type" character varying(50);')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "filters" json;')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "columns" json;')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "sort_by" character varying(100);')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "sort_order" character varying(10);')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."saved_views" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "code" character varying(128);')
    op.execute(
        'ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "client_id" character varying(64);'
    )
    op.execute('ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "scopes" text;')
    op.execute(
        'ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "redirect_uri" character varying(1024);'
    )
    op.execute(
        'ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "code_challenge" character varying(128);'
    )
    op.execute(
        'ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "expires_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "used" boolean;')
    op.execute(
        'ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute(
        'ALTER TABLE public."oauth_authorization_codes" ADD COLUMN IF NOT EXISTS "code_challenge_method" character varying(8) DEFAULT \'S256\'::character varying;'
    )
    op.execute('ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "state" character varying(128);')
    op.execute('ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "client_id" character varying(64);')
    op.execute(
        'ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "redirect_uri" character varying(1024);'
    )
    op.execute('ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "scopes" json;')
    op.execute(
        'ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "code_challenge" character varying(128);'
    )
    op.execute('ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "expires_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "consumed" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute(
        'ALTER TABLE public."oauth_consent_states" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."token_families" ADD COLUMN IF NOT EXISTS "family_id" uuid;')
    op.execute('ALTER TABLE public."token_families" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."token_families" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."token_families" ADD COLUMN IF NOT EXISTS "max_sequence" integer;')
    op.execute('ALTER TABLE public."token_families" ADD COLUMN IF NOT EXISTS "is_blacklisted" boolean;')
    op.execute(
        'ALTER TABLE public."token_families" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."token_families" ADD COLUMN IF NOT EXISTS "blacklisted_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "lookup_prefix" character varying(8);')
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "hashed_secret" character varying(64);')
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "role" character varying(20);')
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "team_id" uuid;')
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "last_used_at" timestamp with time zone;')
    op.execute(
        'ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "expires_at" timestamp with time zone DEFAULT (now() + \'365 days\'::interval);'
    )
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "revoked_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."org_api_keys" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "email" character varying(320);')
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "display_name" character varying(255);')
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "password_hash" character varying(255);')
    op.execute(
        'ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "auth_provider" character varying(20) DEFAULT \'local\'::character varying;'
    )
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "sso_subject" character varying(512);')
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "active" boolean DEFAULT true;')
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "last_login" timestamp with time zone;')
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "preferences" json DEFAULT \'{}\'::json;')
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "is_system_admin" boolean DEFAULT false;')
    op.execute(
        'ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "is_break_glass" boolean DEFAULT false;')
    op.execute(
        'ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "break_glass_expires_at" timestamp with time zone;'
    )
    op.execute(
        'ALTER TABLE public."accounts" ADD COLUMN IF NOT EXISTS "break_glass_deactivated_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."org_memberships" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute(
        'ALTER TABLE public."org_memberships" ADD COLUMN IF NOT EXISTS "role" character varying(20) DEFAULT \'runner\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."org_memberships" ADD COLUMN IF NOT EXISTS "joined_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."org_memberships" ADD COLUMN IF NOT EXISTS "deactivated_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."org_memberships" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."org_memberships" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."org_memberships" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."org_memberships" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "event_type" character varying(100);')
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "resource_type" character varying(100);')
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "resource_id" uuid;')
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "payload_json" json;')
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "request_id" character varying(255);')
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "previous_hash" text;')
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."audit_chain_heads" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."audit_chain_heads" ADD COLUMN IF NOT EXISTS "last_event_hash" text;')
    op.execute('ALTER TABLE public."audit_chain_heads" ADD COLUMN IF NOT EXISTS "last_event_id" uuid;')
    op.execute('ALTER TABLE public."audit_chain_heads" ADD COLUMN IF NOT EXISTS "event_count" integer;')
    op.execute('ALTER TABLE public."audit_chain_heads" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "description" character varying(2000);')
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "notification_endpoints" json;')
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "settings" json;')
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "daily_spend_limit" numeric(14,6);')
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."teams" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."deleted_defaults" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."deleted_defaults" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."deleted_defaults" ADD COLUMN IF NOT EXISTS "signal" character varying(100);')
    op.execute(
        'ALTER TABLE public."deleted_defaults" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."deleted_defaults" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "slug" character varying(255);')
    op.execute(
        'ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "status" character varying(20) DEFAULT \'active\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "created_by" uuid;')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "settings_json" json;')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "otel_config_json" json;')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "plan_id" character varying(255);')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "daily_spend_limit" numeric(14,6);')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "deletion_token" character varying(128);')
    op.execute(
        'ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "deletion_token_expires_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "export_bundle_json" json;')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "authz_enforce" boolean DEFAULT true;')
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "triggers_paused" boolean DEFAULT false;')
    op.execute(
        'ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "triggers_paused_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "guardrail_pins_json" json;')
    op.execute(
        'ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "guardrails_kill_switch" boolean DEFAULT false;'
    )
    op.execute(
        'ALTER TABLE public."organisations" ADD COLUMN IF NOT EXISTS "guardrails_kill_switch_at" timestamp with time zone;'
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='alembic_version' AND column_name='version_num' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"alembic_version\" WHERE \"version_num\" IS NULL) THEN ALTER TABLE public.\"alembic_version\" ALTER COLUMN \"version_num\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='provider_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"provider_type\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"provider_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='enabled' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"enabled\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"enabled\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='auto_provision' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"auto_provision\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"auto_provision\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='default_role' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"default_role\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"default_role\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='group_mappings' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"group_mappings\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"group_mappings\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"sso_providers\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_token_families' AND column_name='family_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_token_families\" WHERE \"family_id\" IS NULL) THEN ALTER TABLE public.\"oauth_token_families\" ALTER COLUMN \"family_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_token_families' AND column_name='client_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_token_families\" WHERE \"client_id\" IS NULL) THEN ALTER TABLE public.\"oauth_token_families\" ALTER COLUMN \"client_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_token_families' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_token_families\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"oauth_token_families\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_token_families' AND column_name='max_sequence' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_token_families\" WHERE \"max_sequence\" IS NULL) THEN ALTER TABLE public.\"oauth_token_families\" ALTER COLUMN \"max_sequence\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_token_families' AND column_name='is_blacklisted' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_token_families\" WHERE \"is_blacklisted\" IS NULL) THEN ALTER TABLE public.\"oauth_token_families\" ALTER COLUMN \"is_blacklisted\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_token_families' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_token_families\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"oauth_token_families\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='client_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"client_id\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"client_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='client_secret_hash' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"client_secret_hash\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"client_secret_hash\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='scopes' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"scopes\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"scopes\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='redirect_uris' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"redirect_uris\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"redirect_uris\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_clients\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='team_memberships' AND column_name='team_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"team_memberships\" WHERE \"team_id\" IS NULL) THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"team_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='team_memberships' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"team_memberships\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='team_memberships' AND column_name='role' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"team_memberships\" WHERE \"role\" IS NULL) THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"role\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='team_memberships' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"team_memberships\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='team_memberships' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"team_memberships\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='team_memberships' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"team_memberships\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='team_memberships' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"team_memberships\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='resource_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"resource_type\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"resource_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='resource_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"resource_id\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"resource_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='token_hash' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"token_hash\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"token_hash\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"expires_at\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='created_by' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"created_by\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"created_by\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"mcp_setup_tokens\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='secrets' AND column_name='key' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"secrets\" WHERE \"key\" IS NULL) THEN ALTER TABLE public.\"secrets\" ALTER COLUMN \"key\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='secrets' AND column_name='encrypted_value' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"secrets\" WHERE \"encrypted_value\" IS NULL) THEN ALTER TABLE public.\"secrets\" ALTER COLUMN \"encrypted_value\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='secrets' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"secrets\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"secrets\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='secrets' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"secrets\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"secrets\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='secrets' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"secrets\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"secrets\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='secrets' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"secrets\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"secrets\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='system_config' AND column_name='key' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"system_config\" WHERE \"key\" IS NULL) THEN ALTER TABLE public.\"system_config\" ALTER COLUMN \"key\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='system_config' AND column_name='value' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"system_config\" WHERE \"value\" IS NULL) THEN ALTER TABLE public.\"system_config\" ALTER COLUMN \"value\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='system_config' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"system_config\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"system_config\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='system_config' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"system_config\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"system_config\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='onboarding_progress' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"onboarding_progress\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='onboarding_progress' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"onboarding_progress\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='onboarding_progress' AND column_name='completed_actions' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"onboarding_progress\" WHERE \"completed_actions\" IS NULL) THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"completed_actions\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='onboarding_progress' AND column_name='skipped_actions' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"onboarding_progress\" WHERE \"skipped_actions\" IS NULL) THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"skipped_actions\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='onboarding_progress' AND column_name='dismissed' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"onboarding_progress\" WHERE \"dismissed\" IS NULL) THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"dismissed\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='onboarding_progress' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"onboarding_progress\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='onboarding_progress' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"onboarding_progress\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='view_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"view_type\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"view_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='filters' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"filters\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"filters\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='sort_order' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"sort_order\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"sort_order\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"saved_views\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='code' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"code\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"code\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='client_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"client_id\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"client_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='scopes' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"scopes\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"scopes\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='redirect_uri' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"redirect_uri\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"redirect_uri\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"expires_at\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='used' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"used\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"used\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='code_challenge_method' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_authorization_codes\" WHERE \"code_challenge_method\" IS NULL) THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"code_challenge_method\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='state' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"state\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"state\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='client_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"client_id\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"client_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='redirect_uri' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"redirect_uri\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"redirect_uri\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='scopes' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"scopes\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"scopes\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='code_challenge' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"code_challenge\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"code_challenge\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"expires_at\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='consumed' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"consumed\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"consumed\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"oauth_consent_states\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='token_families' AND column_name='family_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"token_families\" WHERE \"family_id\" IS NULL) THEN ALTER TABLE public.\"token_families\" ALTER COLUMN \"family_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='token_families' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"token_families\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"token_families\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='token_families' AND column_name='max_sequence' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"token_families\" WHERE \"max_sequence\" IS NULL) THEN ALTER TABLE public.\"token_families\" ALTER COLUMN \"max_sequence\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='token_families' AND column_name='is_blacklisted' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"token_families\" WHERE \"is_blacklisted\" IS NULL) THEN ALTER TABLE public.\"token_families\" ALTER COLUMN \"is_blacklisted\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='token_families' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"token_families\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"token_families\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='lookup_prefix' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"lookup_prefix\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"lookup_prefix\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='hashed_secret' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"hashed_secret\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"hashed_secret\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='role' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"role\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"role\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"expires_at\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_api_keys\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='email' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"email\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"email\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='display_name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"display_name\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"display_name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='auth_provider' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"auth_provider\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"auth_provider\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='active' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"active\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"active\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='preferences' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"preferences\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"preferences\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='is_system_admin' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"is_system_admin\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"is_system_admin\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='is_break_glass' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"accounts\" WHERE \"is_break_glass\" IS NULL) THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"is_break_glass\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_memberships' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_memberships\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_memberships' AND column_name='role' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_memberships\" WHERE \"role\" IS NULL) THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"role\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_memberships' AND column_name='joined_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_memberships\" WHERE \"joined_at\" IS NULL) THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"joined_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_memberships' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_memberships\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_memberships' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_memberships\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_memberships' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_memberships\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_memberships' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_memberships\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='event_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_events\" WHERE \"event_type\" IS NULL) THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"event_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='payload_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_events\" WHERE \"payload_json\" IS NULL) THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"payload_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_events\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_events\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_events\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_events\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_chain_heads' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_chain_heads\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"audit_chain_heads\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_chain_heads' AND column_name='last_event_hash' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_chain_heads\" WHERE \"last_event_hash\" IS NULL) THEN ALTER TABLE public.\"audit_chain_heads\" ALTER COLUMN \"last_event_hash\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_chain_heads' AND column_name='event_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_chain_heads\" WHERE \"event_count\" IS NULL) THEN ALTER TABLE public.\"audit_chain_heads\" ALTER COLUMN \"event_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_chain_heads' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"audit_chain_heads\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"audit_chain_heads\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"teams\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"teams\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='notification_endpoints' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"teams\" WHERE \"notification_endpoints\" IS NULL) THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"notification_endpoints\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='settings' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"teams\" WHERE \"settings\" IS NULL) THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"settings\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"teams\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"teams\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"teams\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"teams\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='deleted_defaults' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"deleted_defaults\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"deleted_defaults\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='deleted_defaults' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"deleted_defaults\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"deleted_defaults\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='deleted_defaults' AND column_name='signal' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"deleted_defaults\" WHERE \"signal\" IS NULL) THEN ALTER TABLE public.\"deleted_defaults\" ALTER COLUMN \"signal\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='deleted_defaults' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"deleted_defaults\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"deleted_defaults\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='deleted_defaults' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"deleted_defaults\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"deleted_defaults\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='slug' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"slug\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"slug\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='settings_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"settings_json\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"settings_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='otel_config_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"otel_config_json\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"otel_config_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='authz_enforce' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"authz_enforce\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"authz_enforce\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='triggers_paused' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"triggers_paused\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"triggers_paused\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='guardrails_kill_switch' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"organisations\" WHERE \"guardrails_kill_switch\" IS NULL) THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"guardrails_kill_switch\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='accounts' AND a.attname='active' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"active\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='accounts' AND a.attname='auth_provider' AND pg_get_expr(ad.adbin, ad.adrelid) = '''local''::character varying') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"auth_provider\" SET DEFAULT 'local'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='accounts' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='accounts' AND a.attname='is_break_glass' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"is_break_glass\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='accounts' AND a.attname='is_system_admin' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"is_system_admin\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='accounts' AND a.attname='preferences' AND pg_get_expr(ad.adbin, ad.adrelid) = '''{}''::json') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"preferences\" SET DEFAULT '{}'::json; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='accounts' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='audit_events' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='audit_events' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='deleted_defaults' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"deleted_defaults\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='deleted_defaults' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"deleted_defaults\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='mcp_setup_tokens' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='mcp_setup_tokens' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='oauth_authorization_codes' AND a.attname='code_challenge_method' AND pg_get_expr(ad.adbin, ad.adrelid) = '''S256''::character varying') THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"code_challenge_method\" SET DEFAULT 'S256'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='oauth_authorization_codes' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='oauth_clients' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='oauth_clients' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='oauth_consent_states' AND a.attname='consumed' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"consumed\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='oauth_consent_states' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='oauth_token_families' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"oauth_token_families\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='onboarding_progress' AND a.attname='completed_actions' AND pg_get_expr(ad.adbin, ad.adrelid) = '''{}''::character varying[]') THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"completed_actions\" SET DEFAULT '{}'::character varying[]; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='onboarding_progress' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'now()') THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"created_at\" SET DEFAULT now(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='onboarding_progress' AND a.attname='dismissed' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"dismissed\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='onboarding_progress' AND a.attname='id' AND pg_get_expr(ad.adbin, ad.adrelid) = 'gen_random_uuid()') THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"id\" SET DEFAULT gen_random_uuid(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='onboarding_progress' AND a.attname='skipped_actions' AND pg_get_expr(ad.adbin, ad.adrelid) = '''{}''::character varying[]') THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"skipped_actions\" SET DEFAULT '{}'::character varying[]; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='onboarding_progress' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'now()') THEN ALTER TABLE public.\"onboarding_progress\" ALTER COLUMN \"updated_at\" SET DEFAULT now(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_api_keys' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_api_keys' AND a.attname='expires_at' AND pg_get_expr(ad.adbin, ad.adrelid) = '(now() + ''365 days''::interval)') THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"expires_at\" SET DEFAULT (now() + '365 days'::interval); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_api_keys' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_memberships' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_memberships' AND a.attname='joined_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"joined_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_memberships' AND a.attname='role' AND pg_get_expr(ad.adbin, ad.adrelid) = '''runner''::character varying') THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"role\" SET DEFAULT 'runner'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_memberships' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='organisations' AND a.attname='authz_enforce' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"authz_enforce\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='organisations' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='organisations' AND a.attname='guardrails_kill_switch' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"guardrails_kill_switch\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='organisations' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''active''::character varying') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"status\" SET DEFAULT 'active'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='organisations' AND a.attname='triggers_paused' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"triggers_paused\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='saved_views' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='saved_views' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='secrets' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"secrets\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='secrets' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"secrets\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='sso_providers' AND a.attname='auto_provision' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"auto_provision\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='sso_providers' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='sso_providers' AND a.attname='default_role' AND pg_get_expr(ad.adbin, ad.adrelid) = '''runner''::character varying') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"default_role\" SET DEFAULT 'runner'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='sso_providers' AND a.attname='enabled' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"enabled\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='sso_providers' AND a.attname='group_mappings' AND pg_get_expr(ad.adbin, ad.adrelid) = '''[]''::json') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"group_mappings\" SET DEFAULT '[]'::json; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='sso_providers' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='system_config' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"system_config\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='team_memberships' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='team_memberships' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"team_memberships\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='teams' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='teams' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='token_families' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"token_families\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='break_glass_deactivated_at' AND is_nullable='NO') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"break_glass_deactivated_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='break_glass_expires_at' AND is_nullable='NO') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"break_glass_expires_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='last_login' AND is_nullable='NO') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"last_login\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='password_hash' AND is_nullable='NO') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"password_hash\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='accounts' AND column_name='sso_subject' AND is_nullable='NO') THEN ALTER TABLE public.\"accounts\" ALTER COLUMN \"sso_subject\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_chain_heads' AND column_name='last_event_id' AND is_nullable='NO') THEN ALTER TABLE public.\"audit_chain_heads\" ALTER COLUMN \"last_event_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='previous_hash' AND is_nullable='NO') THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"previous_hash\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='request_id' AND is_nullable='NO') THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"request_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='resource_id' AND is_nullable='NO') THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"resource_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='audit_events' AND column_name='resource_type' AND is_nullable='NO') THEN ALTER TABLE public.\"audit_events\" ALTER COLUMN \"resource_type\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp_setup_tokens' AND column_name='completed_at' AND is_nullable='NO') THEN ALTER TABLE public.\"mcp_setup_tokens\" ALTER COLUMN \"completed_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_authorization_codes' AND column_name='code_challenge' AND is_nullable='NO') THEN ALTER TABLE public.\"oauth_authorization_codes\" ALTER COLUMN \"code_challenge\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_clients' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"oauth_clients\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_consent_states' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"oauth_consent_states\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='oauth_token_families' AND column_name='blacklisted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"oauth_token_families\" ALTER COLUMN \"blacklisted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='last_used_at' AND is_nullable='NO') THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"last_used_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='revoked_at' AND is_nullable='NO') THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"revoked_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_api_keys' AND column_name='team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"org_api_keys\" ALTER COLUMN \"team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_memberships' AND column_name='deactivated_at' AND is_nullable='NO') THEN ALTER TABLE public.\"org_memberships\" ALTER COLUMN \"deactivated_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='created_by' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"created_by\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='daily_spend_limit' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"daily_spend_limit\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='deletion_token' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"deletion_token\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='deletion_token_expires_at' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"deletion_token_expires_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='export_bundle_json' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"export_bundle_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='guardrail_pins_json' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"guardrail_pins_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='guardrails_kill_switch_at' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"guardrails_kill_switch_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='plan_id' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"plan_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='triggers_paused_at' AND is_nullable='NO') THEN ALTER TABLE public.\"organisations\" ALTER COLUMN \"triggers_paused_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='columns' AND is_nullable='NO') THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"columns\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='saved_views' AND column_name='sort_by' AND is_nullable='NO') THEN ALTER TABLE public.\"saved_views\" ALTER COLUMN \"sort_by\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='client_id' AND is_nullable='NO') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"client_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='client_secret' AND is_nullable='NO') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"client_secret\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='discovery_url' AND is_nullable='NO') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"discovery_url\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='entity_id' AND is_nullable='NO') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"entity_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='metadata_url' AND is_nullable='NO') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"metadata_url\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='metadata_xml' AND is_nullable='NO') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"metadata_xml\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='sso_providers' AND column_name='scopes' AND is_nullable='NO') THEN ALTER TABLE public.\"sso_providers\" ALTER COLUMN \"scopes\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='system_config' AND column_name='updated_by' AND is_nullable='NO') THEN ALTER TABLE public.\"system_config\" ALTER COLUMN \"updated_by\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='daily_spend_limit' AND is_nullable='NO') THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"daily_spend_limit\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='teams' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"teams\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='token_families' AND column_name='blacklisted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"token_families\" ALTER COLUMN \"blacklisted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='token_families' AND column_name='organisation_id' AND is_nullable='NO') THEN ALTER TABLE public.\"token_families\" ALTER COLUMN \"organisation_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute("ALTER TABLE public.teams DROP CONSTRAINT IF EXISTS uq_teams_organisation_name;")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_chain_heads_organisation_id ON public.audit_chain_heads USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_events_event_type ON public.audit_events USING btree (event_type);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_events_organisation_id ON public.audit_events USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_deleted_defaults_organisation_id ON public.deleted_defaults USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mcp_setup_tokens_organisation_id ON public.mcp_setup_tokens USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mcp_setup_tokens_resource_id ON public.mcp_setup_tokens USING btree (resource_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_authorization_codes_account_id ON public.oauth_authorization_codes USING btree (account_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_authorization_codes_client_id ON public.oauth_authorization_codes USING btree (client_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_authorization_codes_organisation_id ON public.oauth_authorization_codes USING btree (organisation_id);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_clients_client_id ON public.oauth_clients USING btree (client_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_clients_organisation_id ON public.oauth_clients USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_consent_states_organisation_id ON public.oauth_consent_states USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_token_families_client_id ON public.oauth_token_families USING btree (client_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_oauth_token_families_organisation_id ON public.oauth_token_families USING btree (organisation_id);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_onboarding_progress_organisation_id ON public.onboarding_progress USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_org_api_keys_organisation_id ON public.org_api_keys USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_org_memberships_account_id ON public.org_memberships USING btree (account_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_org_memberships_org_account ON public.org_memberships USING btree (organisation_id, account_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_org_memberships_organisation_id ON public.org_memberships USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_saved_views_organisation_id ON public.saved_views USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_saved_views_view_type ON public.saved_views USING btree (view_type);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_secrets_key ON public.secrets USING btree (key);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_secrets_organisation_id ON public.secrets USING btree (organisation_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sso_providers_organisation_id ON public.sso_providers USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_team_memberships_account_id ON public.team_memberships USING btree (account_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_team_memberships_organisation_id ON public.team_memberships USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_team_memberships_team_id ON public.team_memberships USING btree (team_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_teams_organisation_id ON public.teams USING btree (organisation_id);")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_teams_organisation_name ON public.teams USING btree (organisation_id, name) WHERE (deleted_at IS NULL);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_families_account_id ON public.token_families USING btree (account_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_families_organisation_id ON public.token_families USING btree (organisation_id);"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION public.audit_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'audit_events are append-only: DELETE is not permitted'; ELSIF TG_OP = 'UPDATE' THEN RAISE EXCEPTION 'audit_events are append-only: UPDATE is not permitted'; END IF; RETURN NULL; END; $$;"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION public.check_team_privilege_cap() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE _user_org_role TEXT; _org_level INT; _team_level INT; BEGIN SELECT role INTO _user_org_role FROM org_memberships WHERE account_id = NEW.account_id AND organisation_id = NEW.organisation_id; _org_level := CASE _user_org_role WHEN 'viewer' THEN 0 WHEN 'runner' THEN 1 WHEN 'operator' THEN 2 WHEN 'admin' THEN 3 ELSE -1 END; _team_level := CASE NEW.role WHEN 'viewer' THEN 0 WHEN 'runner' THEN 1 WHEN 'operator' THEN 2 ELSE -1 END; IF _team_level > _org_level THEN RAISE EXCEPTION 'Team role \"%\" exceeds org role \"%\" for account %', NEW.role, _user_org_role, NEW.account_id; END IF; RETURN NEW; END; $$;"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION public.deactivate_break_glass(caller_account_id uuid, target_account_id uuid, force_last_admin boolean DEFAULT false) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog', 'public' SET row_security TO 'off' AS $_$ DECLARE tgt_org RECORD; k1 int4; k2 int4; is_operator bool; is_bg_target bool; BEGIN is_operator := session_user = 'modulo_breakglass'; is_bg_target := EXISTS (SELECT 1 FROM public.accounts WHERE id = $2 AND is_break_glass IS TRUE); IF $3 AND NOT is_operator THEN RAISE EXCEPTION 'force_last_admin requires operator' USING ERRCODE = 'M2010'; END IF; IF NOT (is_operator OR EXISTS (SELECT 1 FROM public.org_memberships caller WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin' AND EXISTS (SELECT 1 FROM public.org_memberships tgt WHERE tgt.account_id = $2 AND tgt.organisation_id = caller.organisation_id)) OR (EXISTS (SELECT 1 FROM public.accounts c WHERE c.id = $1 AND c.is_break_glass IS TRUE AND c.active IS TRUE) AND is_bg_target AND EXISTS (SELECT 1 FROM public.org_memberships cm JOIN public.org_memberships tm ON tm.organisation_id = cm.organisation_id WHERE cm.account_id = $1 AND tm.account_id = $2))) THEN RAISE EXCEPTION 'caller not authorized to deactivate target' USING ERRCODE = 'M2010'; END IF;  FOR tgt_org IN SELECT DISTINCT organisation_id FROM public.org_memberships WHERE account_id = $2 AND deactivated_at IS NULL ORDER BY organisation_id LOOP SELECT ('x' || substr(md5(tgt_org.organisation_id::text), 1, 8))::bit(32)::int4, ('x' || substr(md5(tgt_org.organisation_id::text), 9, 8))::bit(32)::int4 INTO k1, k2; PERFORM pg_advisory_xact_lock(k1, k2); END LOOP;  FOR tgt_org IN SELECT DISTINCT organisation_id FROM public.org_memberships WHERE account_id = $2 AND deactivated_at IS NULL LOOP IF NOT $3 AND (SELECT count(*) FROM public.org_memberships om JOIN public.accounts a ON a.id = om.account_id WHERE om.organisation_id = tgt_org.organisation_id AND om.deactivated_at IS NULL AND om.role = 'admin' AND a.active IS TRUE AND a.is_break_glass IS FALSE AND a.id <> $2) = 0 AND EXISTS (SELECT 1 FROM public.org_memberships om2 JOIN public.accounts a2 ON a2.id = om2.account_id WHERE om2.organisation_id = tgt_org.organisation_id AND a2.is_break_glass IS FALSE) THEN RAISE EXCEPTION 'deactivation would orphan org' USING ERRCODE = 'M2020'; END IF; END LOOP;  IF is_operator THEN UPDATE public.token_families SET is_blacklisted = true, blacklisted_at = now() WHERE account_id = $2; UPDATE public.org_api_keys SET revoked_at = now() WHERE account_id = $2 AND revoked_at IS NULL; UPDATE public.org_memberships SET deactivated_at = now() WHERE account_id = $2; ELSE UPDATE public.token_families SET is_blacklisted = true, blacklisted_at = now() WHERE account_id = $2 AND family_id IN (SELECT tf.family_id FROM public.token_families tf JOIN public.org_memberships caller ON caller.organisation_id = tf.organisation_id WHERE tf.account_id = $2 AND caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'); UPDATE public.org_api_keys SET revoked_at = now() WHERE account_id = $2 AND revoked_at IS NULL AND organisation_id IN (SELECT caller.organisation_id FROM public.org_memberships caller WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'); UPDATE public.org_memberships SET deactivated_at = now() WHERE account_id = $2 AND organisation_id IN (SELECT caller.organisation_id FROM public.org_memberships caller WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'); END IF; UPDATE public.accounts SET active = false WHERE id = $2; IF NOT FOUND THEN RAISE EXCEPTION 'target does not exist' USING ERRCODE = 'M2040'; END IF; IF is_bg_target THEN UPDATE public.accounts SET break_glass_expires_at = NULL, break_glass_deactivated_at = now(), password_hash = gen_random_uuid()::text WHERE id = $2; END IF; END $_$;"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION public.enforce_library_fork_provenance() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE parent_source text; BEGIN IF TG_OP = 'UPDATE' AND OLD.forked_from IS DISTINCT FROM NEW.forked_from THEN RAISE EXCEPTION 'library primitive forked_from is immutable' USING ERRCODE = '23514'; END IF; IF NEW.forked_from IS NOT NULL THEN SELECT source INTO parent_source FROM library_primitives WHERE id = NEW.forked_from; IF parent_source IS DISTINCT FROM 'registry' THEN RAISE EXCEPTION 'forked_from must reference a registry primitive' USING ERRCODE = '23514'; END IF; END IF; RETURN NEW; END; $$;"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION public.enforce_same_organisation() RETURNS trigger LANGUAGE plpgsql AS $_$ DECLARE referenced_id uuid; referenced_organisation_id uuid; child_organisation_id uuid; col_exists boolean; col_type text; BEGIN SELECT data_type INTO col_type FROM information_schema.columns WHERE table_name = TG_TABLE_NAME AND column_name = TG_ARGV[1]; IF col_type IS DISTINCT FROM 'uuid' THEN RETURN NEW; END IF; referenced_id := (to_jsonb(NEW) ->> TG_ARGV[1])::uuid; IF referenced_id IS NULL THEN RETURN NEW; END IF; child_organisation_id := (to_jsonb(NEW) ->> 'organisation_id')::uuid; SELECT COUNT(*) > 0 INTO col_exists FROM information_schema.columns WHERE table_name = TG_ARGV[0] AND column_name = 'organisation_id'; IF NOT col_exists THEN RETURN NEW; END IF; EXECUTE format('SELECT organisation_id FROM %I WHERE id = $1', TG_ARGV[0]) INTO referenced_organisation_id USING referenced_id; IF referenced_organisation_id IS NULL OR referenced_organisation_id <> child_organisation_id THEN RAISE EXCEPTION 'cross-organisation reference from %.% to %', TG_TABLE_NAME, TG_ARGV[1], TG_ARGV[0] USING ERRCODE = '23503'; END IF; RETURN NEW; END; $_$;"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION public.error_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'error_events are append-only: DELETE is not permitted'; ELSIF TG_OP = 'UPDATE' THEN RAISE EXCEPTION 'error_events are append-only: UPDATE is not permitted'; END IF; RETURN NULL; END; $$;"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION public.lookup_api_key_org(lookup_prefix_value text) RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'public' AS $$ SELECT organisation_id FROM org_api_keys WHERE lookup_prefix = lookup_prefix_value AND revoked_at IS NULL LIMIT 1 $$;"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_stages_account_id_tenant ON public.stages;")
    op.execute("DROP TRIGGER IF EXISTS trg_stages_owner_team_id_tenant ON public.stages;")
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='audit_events_no_delete') THEN CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.audit_events_append_only(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='audit_events_no_update') THEN CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.audit_events_append_only(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_chain_heads_last_event_id_tenant') THEN CREATE TRIGGER trg_audit_chain_heads_last_event_id_tenant BEFORE INSERT OR UPDATE OF last_event_id, organisation_id ON public.audit_chain_heads FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('audit_events', 'last_event_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_events_account_id_tenant') THEN CREATE TRIGGER trg_audit_events_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_mcp_setup_tokens_created_by_tenant') THEN CREATE TRIGGER trg_mcp_setup_tokens_created_by_tenant BEFORE INSERT OR UPDATE OF created_by, organisation_id ON public.mcp_setup_tokens FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'created_by'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_oauth_clients_account_id_tenant') THEN CREATE TRIGGER trg_oauth_clients_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.oauth_clients FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_org_api_keys_account_id_tenant') THEN CREATE TRIGGER trg_org_api_keys_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.org_api_keys FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_org_api_keys_team_id_tenant') THEN CREATE TRIGGER trg_org_api_keys_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.org_api_keys FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_org_memberships_account_id_tenant') THEN CREATE TRIGGER trg_org_memberships_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.org_memberships FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_saved_views_account_id_tenant') THEN CREATE TRIGGER trg_saved_views_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.saved_views FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_team_memberships_account_id_tenant') THEN CREATE TRIGGER trg_team_memberships_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.team_memberships FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_team_memberships_team_id_tenant') THEN CREATE TRIGGER trg_team_memberships_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.team_memberships FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_team_privilege_cap') THEN CREATE TRIGGER trg_team_privilege_cap BEFORE INSERT OR UPDATE ON public.team_memberships FOR EACH ROW EXECUTE FUNCTION public.check_team_privilege_cap(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_teams_account_id_tenant') THEN CREATE TRIGGER trg_teams_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.teams FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_token_families_account_id_tenant') THEN CREATE TRIGGER trg_token_families_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.token_families FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute("ALTER TABLE public.audit_chain_heads ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.deleted_defaults ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.mcp_setup_tokens ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.oauth_authorization_codes ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.oauth_clients ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.oauth_consent_states ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.oauth_token_families ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.onboarding_progress ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.org_api_keys ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.org_memberships ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.saved_views ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.secrets ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.sso_providers ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.team_memberships ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.teams ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.token_families ENABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.audit_chain_heads;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.audit_chain_heads USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.audit_events;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.audit_events USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.deleted_defaults;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.deleted_defaults USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.mcp_setup_tokens;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.mcp_setup_tokens USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.oauth_authorization_codes;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.oauth_authorization_codes USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.oauth_clients;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.oauth_clients USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.oauth_consent_states;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.oauth_consent_states USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.oauth_token_families;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.oauth_token_families USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.onboarding_progress;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.onboarding_progress USING ((organisation_id = (current_setting('app.organisation_id'::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.org_api_keys;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.org_api_keys USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.org_memberships;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.org_memberships USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.saved_views;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.saved_views USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.secrets;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.secrets USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.sso_providers;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.sso_providers USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.team_memberships;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.team_memberships USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.teams;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.teams USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.token_families;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.token_families USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_org_memberships_role' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((role)::text=ANY((ARRAY[''admin''::charactervarying,''operator''::charactervarying,''runner''::charactervarying,''viewer''::charactervarying])::text[])))') THEN ALTER TABLE public.org_memberships DROP CONSTRAINT IF EXISTS ck_org_memberships_role; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='accounts_pkey') THEN ALTER TABLE public.accounts ADD CONSTRAINT accounts_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='alembic_version_pkey') THEN ALTER TABLE public.alembic_version ADD CONSTRAINT alembic_version_pkey PRIMARY KEY (version_num); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='audit_chain_heads_pkey') THEN ALTER TABLE public.audit_chain_heads ADD CONSTRAINT audit_chain_heads_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='audit_events_pkey') THEN ALTER TABLE public.audit_events ADD CONSTRAINT audit_events_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='deleted_defaults_pkey') THEN ALTER TABLE public.deleted_defaults ADD CONSTRAINT deleted_defaults_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='mcp_setup_tokens_pkey') THEN ALTER TABLE public.mcp_setup_tokens ADD CONSTRAINT mcp_setup_tokens_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_authorization_codes_pkey') THEN ALTER TABLE public.oauth_authorization_codes ADD CONSTRAINT oauth_authorization_codes_pkey PRIMARY KEY (code); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_clients_pkey') THEN ALTER TABLE public.oauth_clients ADD CONSTRAINT oauth_clients_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_consent_states_pkey') THEN ALTER TABLE public.oauth_consent_states ADD CONSTRAINT oauth_consent_states_pkey PRIMARY KEY (state); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_token_families_pkey') THEN ALTER TABLE public.oauth_token_families ADD CONSTRAINT oauth_token_families_pkey PRIMARY KEY (family_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='onboarding_progress_pkey') THEN ALTER TABLE public.onboarding_progress ADD CONSTRAINT onboarding_progress_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_api_keys_pkey') THEN ALTER TABLE public.org_api_keys ADD CONSTRAINT org_api_keys_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_memberships_pkey') THEN ALTER TABLE public.org_memberships ADD CONSTRAINT org_memberships_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='organisations_pkey') THEN ALTER TABLE public.organisations ADD CONSTRAINT organisations_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='saved_views_pkey') THEN ALTER TABLE public.saved_views ADD CONSTRAINT saved_views_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='secrets_pkey') THEN ALTER TABLE public.secrets ADD CONSTRAINT secrets_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sso_providers_pkey') THEN ALTER TABLE public.sso_providers ADD CONSTRAINT sso_providers_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='system_config_pkey') THEN ALTER TABLE public.system_config ADD CONSTRAINT system_config_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='team_memberships_pkey') THEN ALTER TABLE public.team_memberships ADD CONSTRAINT team_memberships_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='teams_pkey') THEN ALTER TABLE public.teams ADD CONSTRAINT teams_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='token_families_pkey') THEN ALTER TABLE public.token_families ADD CONSTRAINT token_families_pkey PRIMARY KEY (family_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='accounts_email_key') THEN ALTER TABLE public.accounts ADD CONSTRAINT accounts_email_key UNIQUE (email); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_deleted_defaults_org_signal') THEN ALTER TABLE public.deleted_defaults ADD CONSTRAINT uq_deleted_defaults_org_signal UNIQUE (organisation_id, signal); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='mcp_setup_tokens_token_hash_key') THEN ALTER TABLE public.mcp_setup_tokens ADD CONSTRAINT mcp_setup_tokens_token_hash_key UNIQUE (token_hash); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_onboarding_progress_org') THEN ALTER TABLE public.onboarding_progress ADD CONSTRAINT uq_onboarding_progress_org UNIQUE (organisation_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_org_api_keys_lookup_prefix') THEN ALTER TABLE public.org_api_keys ADD CONSTRAINT uq_org_api_keys_lookup_prefix UNIQUE (lookup_prefix); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_org_memberships_account_org') THEN ALTER TABLE public.org_memberships ADD CONSTRAINT uq_org_memberships_account_org UNIQUE (account_id, organisation_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='organisations_slug_key') THEN ALTER TABLE public.organisations ADD CONSTRAINT organisations_slug_key UNIQUE (slug); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_secrets_org_key') THEN ALTER TABLE public.secrets ADD CONSTRAINT uq_secrets_org_key UNIQUE (organisation_id, key); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='system_config_key_key') THEN ALTER TABLE public.system_config ADD CONSTRAINT system_config_key_key UNIQUE (key); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_team_memberships_team_account') THEN ALTER TABLE public.team_memberships ADD CONSTRAINT uq_team_memberships_team_account UNIQUE (team_id, account_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_accounts_auth_provider') THEN ALTER TABLE public.accounts ADD CONSTRAINT ck_accounts_auth_provider CHECK (((auth_provider)::text = ANY ((ARRAY['local'::character varying, 'oidc'::character varying, 'saml'::character varying, 'scim'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_accounts_break_glass_expiry') THEN ALTER TABLE public.accounts ADD CONSTRAINT ck_accounts_break_glass_expiry CHECK (((NOT is_break_glass) OR (break_glass_expires_at IS NOT NULL) OR (break_glass_deactivated_at IS NOT NULL))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_org_api_keys_role') THEN ALTER TABLE public.org_api_keys ADD CONSTRAINT ck_org_api_keys_role CHECK (((role)::text = ANY ((ARRAY['operator'::character varying, 'runner'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_org_memberships_role') THEN ALTER TABLE public.org_memberships ADD CONSTRAINT ck_org_memberships_role CHECK (((role)::text = ANY ((ARRAY['admin'::character varying, 'operator'::character varying, 'runner'::character varying, 'viewer'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_organisations_guardrails_kill_switch_at') THEN ALTER TABLE public.organisations ADD CONSTRAINT ck_organisations_guardrails_kill_switch_at CHECK (((NOT guardrails_kill_switch) OR (guardrails_kill_switch_at IS NOT NULL))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_organisations_status') THEN ALTER TABLE public.organisations ADD CONSTRAINT ck_organisations_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'suspended'::character varying, 'deleted'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_organisations_triggers_paused_at') THEN ALTER TABLE public.organisations ADD CONSTRAINT ck_organisations_triggers_paused_at CHECK (((NOT triggers_paused) OR (triggers_paused_at IS NOT NULL))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_saved_views_type') THEN ALTER TABLE public.saved_views ADD CONSTRAINT ck_saved_views_type CHECK (((view_type)::text = ANY ((ARRAY['run_list'::character varying, 'pipeline_list'::character varying, 'audit_log'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_team_memberships_role') THEN ALTER TABLE public.team_memberships ADD CONSTRAINT ck_team_memberships_role CHECK (((role)::text = ANY ((ARRAY['viewer'::character varying, 'runner'::character varying, 'operator'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='audit_chain_heads_last_event_id_fkey') THEN ALTER TABLE public.audit_chain_heads ADD CONSTRAINT audit_chain_heads_last_event_id_fkey FOREIGN KEY (last_event_id) REFERENCES audit_events(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='audit_chain_heads_organisation_id_fkey') THEN ALTER TABLE public.audit_chain_heads ADD CONSTRAINT audit_chain_heads_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='audit_events_account_id_fkey') THEN ALTER TABLE public.audit_events ADD CONSTRAINT audit_events_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='audit_events_organisation_id_fkey') THEN ALTER TABLE public.audit_events ADD CONSTRAINT audit_events_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='deleted_defaults_organisation_id_fkey') THEN ALTER TABLE public.deleted_defaults ADD CONSTRAINT deleted_defaults_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_mcp_setup_tokens_created_by') THEN ALTER TABLE public.mcp_setup_tokens ADD CONSTRAINT fk_mcp_setup_tokens_created_by FOREIGN KEY (created_by) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='mcp_setup_tokens_organisation_id_fkey') THEN ALTER TABLE public.mcp_setup_tokens ADD CONSTRAINT mcp_setup_tokens_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_oauth_authorization_codes_account_id') THEN ALTER TABLE public.oauth_authorization_codes ADD CONSTRAINT fk_oauth_authorization_codes_account_id FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_authorization_codes_organisation_id_fkey') THEN ALTER TABLE public.oauth_authorization_codes ADD CONSTRAINT oauth_authorization_codes_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_clients_account_id_fkey') THEN ALTER TABLE public.oauth_clients ADD CONSTRAINT oauth_clients_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_clients_organisation_id_fkey') THEN ALTER TABLE public.oauth_clients ADD CONSTRAINT oauth_clients_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_consent_states_account_id_fkey') THEN ALTER TABLE public.oauth_consent_states ADD CONSTRAINT oauth_consent_states_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_consent_states_organisation_id_fkey') THEN ALTER TABLE public.oauth_consent_states ADD CONSTRAINT oauth_consent_states_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='oauth_token_families_organisation_id_fkey') THEN ALTER TABLE public.oauth_token_families ADD CONSTRAINT oauth_token_families_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='onboarding_progress_organisation_id_fkey') THEN ALTER TABLE public.onboarding_progress ADD CONSTRAINT onboarding_progress_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_api_keys_account_id_fkey') THEN ALTER TABLE public.org_api_keys ADD CONSTRAINT org_api_keys_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_api_keys_organisation_id_fkey') THEN ALTER TABLE public.org_api_keys ADD CONSTRAINT org_api_keys_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_api_keys_team_id_fkey') THEN ALTER TABLE public.org_api_keys ADD CONSTRAINT org_api_keys_team_id_fkey FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_memberships_account_id_fkey') THEN ALTER TABLE public.org_memberships ADD CONSTRAINT org_memberships_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_memberships_organisation_id_fkey') THEN ALTER TABLE public.org_memberships ADD CONSTRAINT org_memberships_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='saved_views_account_id_fkey') THEN ALTER TABLE public.saved_views ADD CONSTRAINT saved_views_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='saved_views_organisation_id_fkey') THEN ALTER TABLE public.saved_views ADD CONSTRAINT saved_views_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='secrets_organisation_id_fkey') THEN ALTER TABLE public.secrets ADD CONSTRAINT secrets_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sso_providers_organisation_id_fkey') THEN ALTER TABLE public.sso_providers ADD CONSTRAINT sso_providers_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_system_config_updated_by') THEN ALTER TABLE public.system_config ADD CONSTRAINT fk_system_config_updated_by FOREIGN KEY (updated_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='team_memberships_account_id_fkey') THEN ALTER TABLE public.team_memberships ADD CONSTRAINT team_memberships_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='team_memberships_organisation_id_fkey') THEN ALTER TABLE public.team_memberships ADD CONSTRAINT team_memberships_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='team_memberships_team_id_fkey') THEN ALTER TABLE public.team_memberships ADD CONSTRAINT team_memberships_team_id_fkey FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='teams_account_id_fkey') THEN ALTER TABLE public.teams ADD CONSTRAINT teams_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='teams_organisation_id_fkey') THEN ALTER TABLE public.teams ADD CONSTRAINT teams_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='token_families_account_id_fkey') THEN ALTER TABLE public.token_families ADD CONSTRAINT token_families_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='token_families_organisation_id_fkey') THEN ALTER TABLE public.token_families ADD CONSTRAINT token_families_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='organisations' AND column_name='otel_config_json' AND column_default IS NULL) THEN ALTER TABLE public.organisations ALTER COLUMN otel_config_json SET DEFAULT '{}'::json; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='modulo_migrate') THEN ALTER TABLE public.accounts OWNER TO modulo_migrate; ALTER TABLE public.org_memberships OWNER TO modulo_migrate; ALTER TABLE public.token_families OWNER TO modulo_migrate; ALTER TABLE public.org_api_keys OWNER TO modulo_migrate; ALTER FUNCTION public.lookup_api_key_org(text) OWNER TO modulo_migrate; ALTER FUNCTION public.deactivate_break_glass(uuid, uuid, boolean) OWNER TO modulo_migrate; END IF; END $$;"
    )


def downgrade() -> None:
    """Downgrade is a no-op: schema reconciliation is not reversible in general."""

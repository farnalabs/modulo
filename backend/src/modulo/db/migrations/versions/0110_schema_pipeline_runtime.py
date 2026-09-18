"""Schema reconciliation — Pipeline runtime & features reconciliation (pipelines, runs, agents, triggers, environments, analytics facts, journeys, guardrails, notifications).

Idempotent, data-safe reconciliation that brings any database to the current
schema state for this domain without assuming prior migration history:

- CREATE TABLE/INDEX/SEQUENCE IF NOT EXISTS; ADD COLUMN IF NOT EXISTS
- constraints (PK/FK/UNIQUE/CHECK) added only when absent (pg_constraint guards)
- triggers created only when absent; policies DROP+CREATE (idempotent)
- RLS enablement re-applied; functions CREATE OR REPLACE
- data-safe SET NOT NULL / SET DEFAULT / ALTER TYPE (never over NULL rows)

Safe on fresh databases (after the v2 base) and on existing databases stamped
at the previous revision (no-ops on existing objects; repairs missing ones).

Revision ID: 0110_schema_pipeline_runtime
Revises: 0109_schema_teams_library
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0110_schema_pipeline_runtime"
down_revision: str | None = "0109_schema_teams_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.cost_components ( id uuid NOT NULL, organisation_id uuid NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, deleted_at timestamp with time zone, name character varying(64) NOT NULL, display_name character varying(128) NOT NULL, kind character varying(20) NOT NULL, rate_usd numeric(18,6), rate_fallback character varying(32), formula character varying(256), report_key character varying(64), enabled boolean DEFAULT true NOT NULL, sort_order integer DEFAULT 0 NOT NULL, CONSTRAINT ck_cost_components_kind CHECK (((kind)::text = ANY ((ARRAY['calculated'::character varying, 'self_reported'::character varying])::text[]))) );"
    )
    op.execute(
        'CREATE TABLE IF NOT EXISTS public.journeys ( id uuid NOT NULL, organisation_id uuid NOT NULL, owner_team_id uuid, kind character varying(64) NOT NULL, ref character varying(255) NOT NULL, canonical_work_item_id uuid NOT NULL, latest_terminal_run_id uuid, map_id uuid, map_version integer, stage_id character varying(255), stage_name character varying(255), "position" integer, latest_status character varying(30), latest_provenance character varying(30), run_count integer DEFAULT 0 NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL );'
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.lifecycle_map_stages ( id uuid NOT NULL, organisation_id uuid NOT NULL, map_id uuid NOT NULL, version integer DEFAULT 1 NOT NULL, stage_id character varying(255) NOT NULL, stage_name character varying(255) NOT NULL, \"position\" integer DEFAULT 0 NOT NULL, stage_type character varying(20) NOT NULL, pipeline_id uuid, account_id uuid NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, CONSTRAINT ck_lifecycle_map_stages_type CHECK (((stage_type)::text = ANY ((ARRAY['modulo'::character varying, 'external'::character varying, 'manual'::character varying, 'placeholder'::character varying])::text[]))) );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.modulo_journey_facts ( id uuid NOT NULL, organisation_id uuid NOT NULL, run_id uuid NOT NULL, writer character varying(30) NOT NULL, parse_failures integer DEFAULT 0 NOT NULL, finalise_attempts integer DEFAULT 0 NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.pipeline_folders ( id uuid NOT NULL, organisation_id uuid NOT NULL, name character varying(255) NOT NULL, parent_id uuid, sort_order integer DEFAULT 0 NOT NULL, account_id uuid NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.run_daily_facts ( id uuid NOT NULL, organisation_id uuid NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, run_id uuid NOT NULL, run_date date NOT NULL, team_id uuid, team_name character varying(255), pipeline_id uuid, pipeline_name character varying(255), folder_id uuid, trigger_type character varying(20) NOT NULL, status character varying(30) NOT NULL, total_cost_usd numeric(14,6), total_tokens integer, duration_ms bigint, error_code character varying(255), claim_count integer, queue_wait_ms bigint, final_idle_ms bigint, cancellation_requested boolean, dispatcher character varying(20), node_count integer, sandbox_agent_node_count integer, max_node_timeout_seconds integer, parent_run_id uuid, snapshot_id uuid, run_number integer, output_bytes bigint, rate_limited boolean, dispatched_at timestamp with time zone, started_at timestamp with time zone, completed_at timestamp with time zone, total_queue_wait_ms bigint, telemetry_bytes bigint );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.run_evidence ( run_id uuid NOT NULL, node_id character varying(255) NOT NULL, evidence_state character varying(20) NOT NULL, evidence_detail character varying(2000), evidence_written_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.run_number_counters ( organisation_id uuid NOT NULL, next_run_number integer DEFAULT 1 NOT NULL );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.snapshot_schema_pins ( id uuid NOT NULL, organisation_id uuid NOT NULL, snapshot_id uuid NOT NULL, node_id uuid NOT NULL, direction character varying(10) NOT NULL, schema_id uuid NOT NULL, schema_version character varying(50) NOT NULL, created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, CONSTRAINT ck_snapshot_schema_pins_direction CHECK (((direction)::text = ANY ((ARRAY['input'::character varying, 'output'::character varying])::text[]))) );"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.web_vital_events ( id uuid NOT NULL, organisation_id uuid NOT NULL, metric_name character varying(50) NOT NULL, metric_value double precision NOT NULL, metric_rating character varying(20), route_path character varying(500), page_url character varying(2000), navigation_type character varying(50), created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL, recorded_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL );"
    )
    op.execute('ALTER TABLE public."dismissals" ADD COLUMN IF NOT EXISTS "notification_id" uuid;')
    op.execute('ALTER TABLE public."dismissals" ADD COLUMN IF NOT EXISTS "dismissed_by_user_id" uuid;')
    op.execute('ALTER TABLE public."dismissals" ADD COLUMN IF NOT EXISTS "dismiss_scope" character varying(20);')
    op.execute(
        'ALTER TABLE public."dismissals" ADD COLUMN IF NOT EXISTS "dismissed_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."dismissals" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "connector_type_id" character varying(255);'
    )
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "owner_team_id" uuid;')
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "visibility" character varying(10) DEFAULT \'org\'::character varying;'
    )
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "credentials_ciphertext" bytea;')
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "config_json" json;')
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "allowed_operations" json;')
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "status" character varying(30) DEFAULT \'active\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "last_health_check_at" timestamp with time zone;'
    )
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "last_health_check_error" character varying(2000);'
    )
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "tier" character varying(20) DEFAULT \'native\'::character varying;'
    )
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."connector_instances" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "display_name" character varying(255);')
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "provider" character varying(30);')
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "model_id" character varying(255);')
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "credentials_ciphertext" bytea;')
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "default_params" json;')
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "cost_tracking" character varying(10) DEFAULT \'enabled\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "currency" character varying(3) DEFAULT \'USD\'::character varying;'
    )
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "owner_team_id" uuid;')
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "visibility" character varying(10) DEFAULT \'org\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "status" character varying(30) DEFAULT \'active\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "last_health_check_at" timestamp with time zone;'
    )
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "last_health_check_error" character varying(2000);'
    )
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "fallback_backend_ids" json;')
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "tier" character varying(20) DEFAULT \'native\'::character varying;'
    )
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."model_backends" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "source_node_id" uuid;')
    op.execute('ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "target_node_id" uuid;')
    op.execute(
        'ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "edge_type" character varying(15) DEFAULT \'normal\'::character varying;'
    )
    op.execute('ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "hitl_gate_config" json;')
    op.execute(
        'ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "condition_expression" character varying(500);'
    )
    op.execute('ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."pipeline_edges" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "description" text;')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "parent_node_id" uuid;')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "timeout_seconds" integer DEFAULT 300;')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "retry_count" integer;')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "retry_delay_seconds" integer;')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."nodes" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."webhook_dedup_hashes" ADD COLUMN IF NOT EXISTS "trigger_id" uuid;')
    op.execute(
        'ALTER TABLE public."webhook_dedup_hashes" ADD COLUMN IF NOT EXISTS "payload_hash" character varying(64);'
    )
    op.execute(
        'ALTER TABLE public."webhook_dedup_hashes" ADD COLUMN IF NOT EXISTS "expires_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."webhook_dedup_hashes" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."webhook_dedup_hashes" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."webhook_dedup_hashes" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."webhook_dedup_hashes" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."node_observations" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."node_observations" ADD COLUMN IF NOT EXISTS "node_id" character varying(255);')
    op.execute('ALTER TABLE public."node_observations" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute(
        'ALTER TABLE public."node_observations" ADD COLUMN IF NOT EXISTS "human_observed_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."node_observations" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."node_observations" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."node_observations" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."node_observations" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "gate_id" character varying(255);')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "rejection_reason" text;')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "rejected_output" json;')
    op.execute(
        'ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "producing_node_id" character varying(255);'
    )
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "producing_agent_id" uuid;')
    op.execute(
        'ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "feedback_status" character varying(20) DEFAULT \'pending\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "feedback_handler_type" character varying(40) DEFAULT \'human\'::character varying;'
    )
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "correction_run_id" uuid;')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "eval_gap" boolean;')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "needs_human_review" boolean;')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "annotation" text;')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."feedback_records" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "event_type" character varying(100);'
    )
    op.execute('ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "endpoint_id" uuid;')
    op.execute('ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute(
        'ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "status" character varying(20) DEFAULT \'delivered\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "attempt_count" integer DEFAULT 0;'
    )
    op.execute('ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "response_code" integer;')
    op.execute(
        'ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "last_error" character varying(2000);'
    )
    op.execute(
        'ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "failed_at" timestamp with time zone;'
    )
    op.execute(
        'ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "response_body" character varying(2000);'
    )
    op.execute('ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "payload_ciphertext" bytea;')
    op.execute('ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."notification_delivery_log" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "scope" character varying(20);')
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "target_user_id" uuid;')
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "level" character varying(20);')
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "category" character varying(100);')
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "title" character varying(500);')
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "body" text;')
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "action_url" character varying(2048);')
    op.execute(
        'ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "dismiss_strategy" character varying(20) DEFAULT \'user_only\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "dismissible_at_scope" boolean DEFAULT false;'
    )
    op.execute(
        'ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "expires_at" timestamp with time zone DEFAULT (now() + \'90 days\'::interval);'
    )
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."notifications" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."webhook_payloads" ADD COLUMN IF NOT EXISTS "trigger_event_id" uuid;')
    op.execute('ALTER TABLE public."webhook_payloads" ADD COLUMN IF NOT EXISTS "raw_body" bytea;')
    op.execute('ALTER TABLE public."webhook_payloads" ADD COLUMN IF NOT EXISTS "raw_payload" json;')
    op.execute('ALTER TABLE public."webhook_payloads" ADD COLUMN IF NOT EXISTS "expires_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."webhook_payloads" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."webhook_payloads" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."webhook_payloads" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."webhook_payloads" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "session_id" uuid;')
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "role" character varying(20);')
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "content" text;')
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "tool_calls_json" json;')
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "tool_results_json" json;')
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "token_count" integer;')
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "parent_id" uuid;')
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."chat_messages" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "user_id" uuid;')
    op.execute('ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "provider" character varying(50);')
    op.execute('ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "model" character varying(100);')
    op.execute('ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "context_window_tokens" integer;')
    op.execute(
        'ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "system_prompt_hash" character varying(64);'
    )
    op.execute('ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "session_number" integer;')
    op.execute('ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."chat_sessions" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "fingerprint" character varying(64);')
    op.execute(
        'ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "status" character varying(20) DEFAULT \'new\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "first_seen" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "last_seen" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "count" integer DEFAULT 1;')
    op.execute(
        'ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "level_peak" character varying(20) DEFAULT \'error\'::character varying;'
    )
    op.execute('ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "sample_event_id" uuid;')
    op.execute('ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "assigned_to" uuid;')
    op.execute('ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "resolved_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."error_groups" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "report_type" character varying(50);')
    op.execute(
        'ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "cron_expression" character varying(100);'
    )
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "config_json" json;')
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "recipient_config" json;')
    op.execute(
        'ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "last_sent_at" timestamp with time zone;'
    )
    op.execute(
        'ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "next_send_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "active" boolean;')
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "created_by" uuid;')
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."scheduled_reports" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "anomaly_date" date;')
    op.execute('ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "amount" numeric(14,6);')
    op.execute('ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "baseline" numeric(14,6);')
    op.execute('ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "percent_above" numeric(8,2);')
    op.execute('ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "dismissed" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."spend_anomalies" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "environment_profile_id" uuid;')
    op.execute('ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "provider_ref" character varying(255);')
    op.execute(
        'ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "status" character varying(30) DEFAULT \'pending\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "repository_url" character varying(1000);'
    )
    op.execute(
        'ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "repository_ref" character varying(255);'
    )
    op.execute(
        'ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "lease_started_at" timestamp with time zone;'
    )
    op.execute(
        'ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "lease_expires_at" timestamp with time zone DEFAULT (now() + \'00:30:00\'::interval);'
    )
    op.execute('ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "resource_usage_json" json;')
    op.execute('ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "output_artifact_refs_json" json;')
    op.execute('ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "error_message" text;')
    op.execute('ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."workspace_leases" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "snapshot_version" integer;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "environment_profile_id" uuid;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "graph_json" json;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "connector_bindings_json" json;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "schema_pins_json" json;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "prompt_pins_json" json;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "model_backend_pins_json" json;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "composite_bindings_json" json;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "tag" character varying(100);')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "notes" character varying(2000);')
    op.execute(
        'ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "default_autonomy_level" character varying(30);'
    )
    op.execute(
        'ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "config_json" json DEFAULT \'{}\'::json;'
    )
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "run_context_defaults" json;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "parameter_bindings_json" json;')
    op.execute('ALTER TABLE public."pipeline_snapshots" ADD COLUMN IF NOT EXISTS "guardrail_pins_json" json;')
    op.execute('ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "metric_name" character varying(50);')
    op.execute('ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "metric_value" double precision;')
    op.execute('ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "metric_rating" character varying(20);')
    op.execute('ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "route_path" character varying(500);')
    op.execute('ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "page_url" character varying(2000);')
    op.execute(
        'ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "navigation_type" character varying(50);'
    )
    op.execute(
        'ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."web_vital_events" ADD COLUMN IF NOT EXISTS "recorded_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "description" text;')
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "provider_type" character varying(50) DEFAULT \'local_docker\'::character varying;'
    )
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "image_ref" character varying(500);')
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "capabilities_json" json;')
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "config_json" json;')
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "network_policy" character varying(20) DEFAULT \'outbound\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "initialisation_strategy" character varying(30) DEFAULT \'git_clone\'::character varying;'
    )
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "secret_refs_json" json;')
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "persistence_policy" character varying(20) DEFAULT \'ephemeral\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "status" character varying(30) DEFAULT \'active\'::character varying;'
    )
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "owner_team_id" uuid;')
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "visibility" character varying(10) DEFAULT \'org\'::character varying;'
    )
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."environment_profiles" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "description" character varying(2000);')
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "variants" json;')
    op.execute(
        'ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "selection_strategy" character varying(20) DEFAULT \'weighted\'::character varying;'
    )
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "run_count" integer DEFAULT 0;')
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "max_concurrent_runs" integer DEFAULT 5;')
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "degraded_evals" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."variant_groups" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "url" character varying(2048);')
    op.execute('ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "secret_ciphertext" bytea;')
    op.execute(
        'ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "events" json DEFAULT \'[]\'::json;'
    )
    op.execute(
        'ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "description" character varying(500);'
    )
    op.execute(
        'ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "consecutive_dead_letter_count" integer DEFAULT 0;'
    )
    op.execute(
        'ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "auto_disabled" boolean DEFAULT false;'
    )
    op.execute(
        'ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "disabled_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "team_id" uuid;')
    op.execute('ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."notification_endpoints" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;'
    )
    op.execute(
        'ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "forwarder_type" character varying(50);'
    )
    op.execute('ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "enabled" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "config_json" json;')
    op.execute(
        'ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "last_test_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "last_test_ok" boolean;')
    op.execute('ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."error_forwarder_configs" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "is_executable" boolean;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "prompt_always_visible" boolean;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "description" character varying(2000);')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "input_schema_id" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "input_schema_version" character varying(50);')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "output_schema_id" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "output_schema_version" character varying(50);')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "prompt_template" text;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "prompt_version_history" json;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "model_backend_id" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "connector_type_refs" json;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "required_environment_capabilities" json;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "evals" json;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "retry_policy" json;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "max_input_length" integer;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "token_budget" integer;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "library_id" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "parameter_schema_id" uuid;')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "template_id" character varying(255);')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "agent_command" character varying(500);')
    op.execute('ALTER TABLE public."agents" ADD COLUMN IF NOT EXISTS "agent_commands" json;')
    op.execute('ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "snapshot_id" uuid;')
    op.execute('ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "node_id" uuid;')
    op.execute('ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "direction" character varying(10);')
    op.execute('ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "schema_id" uuid;')
    op.execute(
        'ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "schema_version" character varying(50);'
    )
    op.execute(
        'ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."snapshot_schema_pins" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "run_date" date;')
    op.execute('ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "team_id" uuid;')
    op.execute('ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "run_count" integer DEFAULT 0;')
    op.execute(
        'ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "total_spend_usd" numeric(14,6) DEFAULT \'0\'::numeric;'
    )
    op.execute('ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "clamped" boolean DEFAULT false;')
    op.execute(
        'ALTER TABLE public."org_daily_run_counts" ADD COLUMN IF NOT EXISTS "refused_spend_usd" numeric(14,6) DEFAULT \'0\'::numeric;'
    )
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "name" character varying(64);')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "display_name" character varying(128);')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "kind" character varying(20);')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "rate_usd" numeric(18,6);')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "rate_fallback" character varying(32);')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "formula" character varying(256);')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "report_key" character varying(64);')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "enabled" boolean DEFAULT true;')
    op.execute('ALTER TABLE public."cost_components" ADD COLUMN IF NOT EXISTS "sort_order" integer DEFAULT 0;')
    op.execute('ALTER TABLE public."pipeline_folders" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."pipeline_folders" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."pipeline_folders" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."pipeline_folders" ADD COLUMN IF NOT EXISTS "parent_id" uuid;')
    op.execute('ALTER TABLE public."pipeline_folders" ADD COLUMN IF NOT EXISTS "sort_order" integer DEFAULT 0;')
    op.execute('ALTER TABLE public."pipeline_folders" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute(
        'ALTER TABLE public."pipeline_folders" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."pipeline_folders" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "run_date" date;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "team_id" uuid;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "team_name" character varying(255);')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "pipeline_name" character varying(255);')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "folder_id" uuid;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "trigger_type" character varying(20);')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "status" character varying(30);')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "total_cost_usd" numeric(14,6);')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "total_tokens" integer;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "duration_ms" bigint;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "error_code" character varying(255);')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "claim_count" integer;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "queue_wait_ms" bigint;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "final_idle_ms" bigint;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "cancellation_requested" boolean;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "dispatcher" character varying(20);')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "node_count" integer;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "sandbox_agent_node_count" integer;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "max_node_timeout_seconds" integer;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "parent_run_id" uuid;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "snapshot_id" uuid;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "run_number" integer;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "output_bytes" bigint;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "rate_limited" boolean;')
    op.execute(
        'ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "dispatched_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "started_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "completed_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "total_queue_wait_ms" bigint;')
    op.execute('ALTER TABLE public."run_daily_facts" ADD COLUMN IF NOT EXISTS "telemetry_bytes" bigint;')
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "map_id" uuid;')
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "version" integer DEFAULT 1;')
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "stage_id" character varying(255);')
    op.execute(
        'ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "stage_name" character varying(255);'
    )
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "position" integer DEFAULT 0;')
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "stage_type" character varying(20);')
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute(
        'ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."lifecycle_map_stages" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "owner_team_id" uuid;')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "kind" character varying(64);')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "ref" character varying(255);')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "canonical_work_item_id" uuid;')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "latest_terminal_run_id" uuid;')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "map_id" uuid;')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "map_version" integer;')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "stage_id" character varying(255);')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "stage_name" character varying(255);')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "position" integer;')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "latest_status" character varying(30);')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "latest_provenance" character varying(30);')
    op.execute('ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "run_count" integer DEFAULT 0;')
    op.execute(
        'ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."journeys" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."modulo_journey_facts" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."modulo_journey_facts" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."modulo_journey_facts" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."modulo_journey_facts" ADD COLUMN IF NOT EXISTS "writer" character varying(30);')
    op.execute('ALTER TABLE public."modulo_journey_facts" ADD COLUMN IF NOT EXISTS "parse_failures" integer DEFAULT 0;')
    op.execute(
        'ALTER TABLE public."modulo_journey_facts" ADD COLUMN IF NOT EXISTS "finalise_attempts" integer DEFAULT 0;'
    )
    op.execute(
        'ALTER TABLE public."modulo_journey_facts" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "fingerprint" character varying(64);')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "level" character varying(20);')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "message" text;')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "stacktrace" text;')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "context_json" json;')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "source" character varying(20);')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "environment" character varying(50);')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "version" character varying(50);')
    op.execute(
        'ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "status" character varying(20) DEFAULT \'new\'::character varying;'
    )
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "resolved_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."error_events" ADD COLUMN IF NOT EXISTS "signal" character varying(100);')
    op.execute('ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "name" character varying(100);')
    op.execute('ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "enabled" boolean DEFAULT true;')
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "condition_level" character varying(20) DEFAULT \'error\'::character varying;'
    )
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "condition_min_count" integer DEFAULT 1;'
    )
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "condition_window_seconds" integer DEFAULT 300;'
    )
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "action_type" character varying(20) DEFAULT \'in_app\'::character varying;'
    )
    op.execute('ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "webhook_url" text;')
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "cooldown_seconds" integer DEFAULT 300;'
    )
    op.execute('ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "signal" character varying(100);'
    )
    op.execute(
        'ALTER TABLE public."error_notification_rules" ADD COLUMN IF NOT EXISTS "is_default" boolean DEFAULT false;'
    )
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "description" character varying(2000);')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "owner_team_id" uuid;')
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "visibility" character varying(10) DEFAULT \'org\'::character varying;'
    )
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "max_concurrent_runs" integer DEFAULT 5;')
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "lock_wait_timeout_seconds" integer DEFAULT 300;'
    )
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "node_timeout_seconds" integer DEFAULT 300;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "max_duration_seconds" integer DEFAULT 3600;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "max_steps" integer;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "token_budget" integer;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "archived_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "run_context_defaults" json;')
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "default_autonomy_level" character varying(30) DEFAULT \'manual_approval\'::character varying;'
    )
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "graph_nodes_json" json DEFAULT \'[]\'::json;')
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "default_feedback_handler" character varying(50);'
    )
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "folder_id" uuid;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "rate_limit_config" json;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "stale_run_timeout_minutes" integer DEFAULT 30;'
    )
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "retry_policy" json DEFAULT \'{}\'::json;')
    op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "circuit_breaker_threshold" numeric(14,6);')
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "circuit_breaker_tripped" boolean DEFAULT false;'
    )
    op.execute(
        'ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "circuit_breaker_tripped_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."run_evidence" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."run_evidence" ADD COLUMN IF NOT EXISTS "node_id" character varying(255);')
    op.execute('ALTER TABLE public."run_evidence" ADD COLUMN IF NOT EXISTS "evidence_state" character varying(20);')
    op.execute('ALTER TABLE public."run_evidence" ADD COLUMN IF NOT EXISTS "evidence_detail" character varying(2000);')
    op.execute(
        'ALTER TABLE public."run_evidence" ADD COLUMN IF NOT EXISTS "evidence_written_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."run_number_counters" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute('ALTER TABLE public."run_number_counters" ADD COLUMN IF NOT EXISTS "next_run_number" integer DEFAULT 1;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "required_team_id" uuid;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "gate_id" character varying(255);')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "claimed_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "claim_token" text;')
    op.execute(
        'ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "expires_at" timestamp with time zone DEFAULT (now() + \'00:15:00\'::interval);'
    )
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "decision" character varying(20);')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "decision_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "delivered_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "decision_payload" jsonb;')
    op.execute(
        'ALTER TABLE public."hitl_claims" ADD COLUMN IF NOT EXISTS "overdue_notified_at" timestamp with time zone;'
    )
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "snapshot_id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "trigger_id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "trigger_type" character varying(20);')
    op.execute(
        'ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "status" character varying(30) DEFAULT \'pending\'::character varying;'
    )
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "parent_run_id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "run_number" integer DEFAULT 0;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "owner_team_id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "input_hash" character varying(64);')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "started_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "completed_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "cancellation_requested" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "total_tokens" integer;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "total_cost_usd" numeric(14,6);')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "node_token_usage" json;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "error_detail" character varying(5000);')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "error_code" character varying(255);')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "langgraph_thread_id" character varying(512);')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "input_payload" json;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "outputs_json" json;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "rate_limit_key" character varying(512);')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "heartbeat_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "dispatched_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "claim_count" integer DEFAULT 0;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "claimed_by" character varying(64);')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "dispatcher" character varying(20);')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "saq_job_id" character varying(255);')
    op.execute(
        'ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "claim_token" character varying(128) DEFAULT (gen_random_uuid())::text;'
    )
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "cost_breakdown" json;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "ledger_written" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "ledger_refused_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "node_attempt_count" integer DEFAULT 0;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "node_telemetry_json" json;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "enqueue_failed_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "sandbox_dispatch_state" text;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "sandbox_id" text;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "work_item_id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "work_item_refs" jsonb;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "is_replay" boolean;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "variant_group_id" uuid;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "work_intact" boolean;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "raw_output_markers" jsonb;')
    op.execute('ALTER TABLE public."runs" ADD COLUMN IF NOT EXISTS "run_classification" jsonb;')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "node_id" uuid;')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "eval_type" character varying(30);')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "config_json" json;')
    op.execute(
        'ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "failure_behaviour" character varying(10) DEFAULT \'warn\'::character varying;'
    )
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "pass_threshold" numeric(8,4);')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "suite_id" character varying(255);')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."eval_definitions" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "node_id" uuid;')
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "eval_id" uuid;')
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "passed" boolean;')
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "score" double precision;')
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "detail" character varying(2000);')
    op.execute(
        'ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "evaluated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."eval_results" ADD COLUMN IF NOT EXISTS "observed" boolean DEFAULT false;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "pipeline_id" uuid;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "trigger_type" character varying(20);')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "active" boolean DEFAULT true;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "max_concurrent_runs" integer DEFAULT 1;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "daily_spend_limit" numeric(12,4);')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "config_json" json;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "cron_expression" character varying(100);')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "cron_timezone" character varying(50);')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "last_fired_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "next_fire_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute(
        'ALTER TABLE public."triggers" ADD COLUMN IF NOT EXISTS "streak_epoch" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "name" character varying(255);')
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "description" character varying(2000);')
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "owner_team_id" uuid;')
    op.execute(
        'ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "visibility" character varying(10) DEFAULT \'org\'::character varying;'
    )
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "version" integer DEFAULT 1;')
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "content_json" json;')
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "archived_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "account_id" uuid;')
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "deleted_at" timestamp with time zone;')
    op.execute('ALTER TABLE public."lifecycle_maps" ADD COLUMN IF NOT EXISTS "updated_by" uuid;')
    op.execute('ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "trigger_id" uuid;')
    op.execute('ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "trigger_type" character varying(20);')
    op.execute('ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "raw_payload_hash" character varying(64);')
    op.execute(
        'ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "received_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "validation_result" character varying(50);'
    )
    op.execute('ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "run_id" uuid;')
    op.execute('ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "error_detail" character varying(2000);')
    op.execute('ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "id" uuid;')
    op.execute('ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "organisation_id" uuid;')
    op.execute(
        'ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute(
        'ALTER TABLE public."trigger_events" ADD COLUMN IF NOT EXISTS "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP;'
    )
    op.execute('ALTER TABLE public."pipelines" DROP COLUMN IF EXISTS "stage_id" CASCADE;')
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='dismissals' AND column_name='notification_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"dismissals\" WHERE \"notification_id\" IS NULL) THEN ALTER TABLE public.\"dismissals\" ALTER COLUMN \"notification_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='dismissals' AND column_name='dismissed_by_user_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"dismissals\" WHERE \"dismissed_by_user_id\" IS NULL) THEN ALTER TABLE public.\"dismissals\" ALTER COLUMN \"dismissed_by_user_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='dismissals' AND column_name='dismiss_scope' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"dismissals\" WHERE \"dismiss_scope\" IS NULL) THEN ALTER TABLE public.\"dismissals\" ALTER COLUMN \"dismiss_scope\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='dismissals' AND column_name='dismissed_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"dismissals\" WHERE \"dismissed_at\" IS NULL) THEN ALTER TABLE public.\"dismissals\" ALTER COLUMN \"dismissed_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='dismissals' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"dismissals\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"dismissals\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='connector_type_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"connector_type_id\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"connector_type_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='visibility' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"visibility\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"visibility\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='credentials_ciphertext' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"credentials_ciphertext\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"credentials_ciphertext\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='config_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"config_json\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"config_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='allowed_operations' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"allowed_operations\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"allowed_operations\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='tier' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"tier\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"tier\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"connector_instances\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='display_name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"display_name\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"display_name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='provider' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"provider\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"provider\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='model_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"model_id\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"model_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='credentials_ciphertext' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"credentials_ciphertext\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"credentials_ciphertext\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='default_params' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"default_params\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"default_params\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='cost_tracking' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"cost_tracking\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"cost_tracking\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='currency' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"currency\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"currency\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='visibility' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"visibility\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"visibility\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='tier' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"tier\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"tier\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"model_backends\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='pipeline_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_edges\" WHERE \"pipeline_id\" IS NULL) THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"pipeline_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='source_node_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_edges\" WHERE \"source_node_id\" IS NULL) THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"source_node_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='target_node_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_edges\" WHERE \"target_node_id\" IS NULL) THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"target_node_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='edge_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_edges\" WHERE \"edge_type\" IS NULL) THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"edge_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_edges\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_edges\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_edges\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_edges\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='pipeline_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"nodes\" WHERE \"pipeline_id\" IS NULL) THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"pipeline_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"nodes\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='timeout_seconds' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"nodes\" WHERE \"timeout_seconds\" IS NULL) THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"timeout_seconds\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"nodes\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"nodes\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"nodes\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"nodes\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"nodes\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_dedup_hashes' AND column_name='trigger_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_dedup_hashes\" WHERE \"trigger_id\" IS NULL) THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"trigger_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_dedup_hashes' AND column_name='payload_hash' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_dedup_hashes\" WHERE \"payload_hash\" IS NULL) THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"payload_hash\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_dedup_hashes' AND column_name='expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_dedup_hashes\" WHERE \"expires_at\" IS NULL) THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_dedup_hashes' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_dedup_hashes\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_dedup_hashes' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_dedup_hashes\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_dedup_hashes' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_dedup_hashes\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_dedup_hashes' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_dedup_hashes\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_observations' AND column_name='run_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_observations\" WHERE \"run_id\" IS NULL) THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"run_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_observations' AND column_name='node_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_observations\" WHERE \"node_id\" IS NULL) THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"node_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_observations' AND column_name='human_observed_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_observations\" WHERE \"human_observed_at\" IS NULL) THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"human_observed_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_observations' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_observations\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_observations' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_observations\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_observations' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_observations\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_observations' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"node_observations\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='run_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"run_id\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"run_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='gate_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"gate_id\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"gate_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='rejection_reason' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"rejection_reason\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"rejection_reason\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='rejected_output' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"rejected_output\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"rejected_output\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='producing_node_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"producing_node_id\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"producing_node_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='feedback_status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"feedback_status\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"feedback_status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='feedback_handler_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"feedback_handler_type\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"feedback_handler_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"feedback_records\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='event_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_delivery_log\" WHERE \"event_type\" IS NULL) THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"event_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_delivery_log\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='attempt_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_delivery_log\" WHERE \"attempt_count\" IS NULL) THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"attempt_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_delivery_log\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_delivery_log\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_delivery_log\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_delivery_log\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='scope' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"scope\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"scope\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='level' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"level\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"level\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='category' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"category\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"category\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='title' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"title\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"title\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='body' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"body\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"body\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='dismiss_strategy' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"dismiss_strategy\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"dismiss_strategy\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='dismissible_at_scope' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"dismissible_at_scope\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"dismissible_at_scope\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"expires_at\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notifications\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_payloads' AND column_name='raw_body' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_payloads\" WHERE \"raw_body\" IS NULL) THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"raw_body\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_payloads' AND column_name='raw_payload' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_payloads\" WHERE \"raw_payload\" IS NULL) THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"raw_payload\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_payloads' AND column_name='expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_payloads\" WHERE \"expires_at\" IS NULL) THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_payloads' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_payloads\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_payloads' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_payloads\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_payloads' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_payloads\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_payloads' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"webhook_payloads\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='session_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_messages\" WHERE \"session_id\" IS NULL) THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"session_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='role' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_messages\" WHERE \"role\" IS NULL) THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"role\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_messages\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_messages\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_messages\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='user_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"user_id\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"user_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='provider' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"provider\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"provider\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='model' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"model\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"model\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='context_window_tokens' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"context_window_tokens\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"context_window_tokens\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='session_number' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"session_number\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"session_number\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"chat_sessions\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='fingerprint' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"fingerprint\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"fingerprint\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='first_seen' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"first_seen\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"first_seen\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='last_seen' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"last_seen\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"last_seen\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"count\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='level_peak' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"level_peak\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"level_peak\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_groups\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"scheduled_reports\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='report_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"scheduled_reports\" WHERE \"report_type\" IS NULL) THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"report_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='cron_expression' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"scheduled_reports\" WHERE \"cron_expression\" IS NULL) THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"cron_expression\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='active' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"scheduled_reports\" WHERE \"active\" IS NULL) THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"active\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"scheduled_reports\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"scheduled_reports\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"scheduled_reports\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"scheduled_reports\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='anomaly_date' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"anomaly_date\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"anomaly_date\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='amount' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"amount\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"amount\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='baseline' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"baseline\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"baseline\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='percent_above' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"percent_above\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"percent_above\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='dismissed' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"dismissed\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"dismissed\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"spend_anomalies\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='environment_profile_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"environment_profile_id\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"environment_profile_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='run_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"run_id\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"run_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='provider_ref' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"provider_ref\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"provider_ref\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='lease_expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"lease_expires_at\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"lease_expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"workspace_leases\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='pipeline_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"pipeline_id\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"pipeline_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='snapshot_version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"snapshot_version\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"snapshot_version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='graph_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"graph_json\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"graph_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='connector_bindings_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"connector_bindings_json\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"connector_bindings_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='schema_pins_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"schema_pins_json\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"schema_pins_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='prompt_pins_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"prompt_pins_json\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"prompt_pins_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='model_backend_pins_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"model_backend_pins_json\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"model_backend_pins_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='config_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"config_json\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"config_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='run_context_defaults' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"run_context_defaults\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"run_context_defaults\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_snapshots\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"web_vital_events\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"web_vital_events\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='metric_name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"web_vital_events\" WHERE \"metric_name\" IS NULL) THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"metric_name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='metric_value' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"web_vital_events\" WHERE \"metric_value\" IS NULL) THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"metric_value\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"web_vital_events\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"web_vital_events\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='recorded_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"web_vital_events\" WHERE \"recorded_at\" IS NULL) THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"recorded_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='provider_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"provider_type\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"provider_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='capabilities_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"capabilities_json\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"capabilities_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='config_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"config_json\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"config_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='network_policy' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"network_policy\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"network_policy\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='initialisation_strategy' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"initialisation_strategy\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"initialisation_strategy\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='secret_refs_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"secret_refs_json\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"secret_refs_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='persistence_policy' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"persistence_policy\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"persistence_policy\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='visibility' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"visibility\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"visibility\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"environment_profiles\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='pipeline_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"pipeline_id\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"pipeline_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='variants' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"variants\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"variants\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='selection_strategy' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"selection_strategy\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"selection_strategy\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='run_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"run_count\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"run_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='max_concurrent_runs' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"max_concurrent_runs\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"max_concurrent_runs\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='degraded_evals' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"degraded_evals\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"degraded_evals\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"variant_groups\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='url' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_endpoints\" WHERE \"url\" IS NULL) THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"url\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='events' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_endpoints\" WHERE \"events\" IS NULL) THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"events\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='consecutive_dead_letter_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_endpoints\" WHERE \"consecutive_dead_letter_count\" IS NULL) THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"consecutive_dead_letter_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='auto_disabled' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_endpoints\" WHERE \"auto_disabled\" IS NULL) THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"auto_disabled\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_endpoints\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_endpoints\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_endpoints\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"notification_endpoints\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='forwarder_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_forwarder_configs\" WHERE \"forwarder_type\" IS NULL) THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"forwarder_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='enabled' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_forwarder_configs\" WHERE \"enabled\" IS NULL) THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"enabled\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_forwarder_configs\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_forwarder_configs\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_forwarder_configs\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_forwarder_configs\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='is_executable' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"is_executable\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"is_executable\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='prompt_always_visible' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"prompt_always_visible\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"prompt_always_visible\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='prompt_template' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"prompt_template\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"prompt_template\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='prompt_version_history' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"prompt_version_history\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"prompt_version_history\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='connector_type_refs' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"connector_type_refs\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"connector_type_refs\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='required_environment_capabilities' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"required_environment_capabilities\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"required_environment_capabilities\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='retry_policy' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"retry_policy\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"retry_policy\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"agents\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='snapshot_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"snapshot_id\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"snapshot_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='node_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"node_id\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"node_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='direction' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"direction\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"direction\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='schema_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"schema_id\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"schema_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='schema_version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"schema_version\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"schema_version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='snapshot_schema_pins' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"snapshot_schema_pins\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='run_date' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"run_date\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"run_date\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='run_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"run_count\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"run_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='total_spend_usd' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"total_spend_usd\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"total_spend_usd\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='clamped' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"clamped\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"clamped\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='refused_spend_usd' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"org_daily_run_counts\" WHERE \"refused_spend_usd\" IS NULL) THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"refused_spend_usd\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='display_name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"display_name\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"display_name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='kind' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"kind\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"kind\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='enabled' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"enabled\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"enabled\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='sort_order' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"cost_components\" WHERE \"sort_order\" IS NULL) THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"sort_order\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_folders' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_folders\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_folders' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_folders\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_folders' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_folders\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_folders' AND column_name='sort_order' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_folders\" WHERE \"sort_order\" IS NULL) THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"sort_order\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_folders' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_folders\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_folders' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_folders\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_folders' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipeline_folders\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_daily_facts\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_daily_facts\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_daily_facts\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_daily_facts\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='run_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_daily_facts\" WHERE \"run_id\" IS NULL) THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"run_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='run_date' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_daily_facts\" WHERE \"run_date\" IS NULL) THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"run_date\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='trigger_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_daily_facts\" WHERE \"trigger_type\" IS NULL) THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"trigger_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_daily_facts\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='map_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"map_id\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"map_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"version\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='stage_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"stage_id\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"stage_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='stage_name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"stage_name\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"stage_name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='position' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"position\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"position\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='stage_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"stage_type\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"stage_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_map_stages\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"journeys\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"journeys\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='kind' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"journeys\" WHERE \"kind\" IS NULL) THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"kind\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='ref' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"journeys\" WHERE \"ref\" IS NULL) THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"ref\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='canonical_work_item_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"journeys\" WHERE \"canonical_work_item_id\" IS NULL) THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"canonical_work_item_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='run_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"journeys\" WHERE \"run_count\" IS NULL) THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"run_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"journeys\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"journeys\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='modulo_journey_facts' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"modulo_journey_facts\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='modulo_journey_facts' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"modulo_journey_facts\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='modulo_journey_facts' AND column_name='run_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"modulo_journey_facts\" WHERE \"run_id\" IS NULL) THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"run_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='modulo_journey_facts' AND column_name='writer' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"modulo_journey_facts\" WHERE \"writer\" IS NULL) THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"writer\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='modulo_journey_facts' AND column_name='parse_failures' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"modulo_journey_facts\" WHERE \"parse_failures\" IS NULL) THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"parse_failures\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='modulo_journey_facts' AND column_name='finalise_attempts' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"modulo_journey_facts\" WHERE \"finalise_attempts\" IS NULL) THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"finalise_attempts\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='modulo_journey_facts' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"modulo_journey_facts\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='fingerprint' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"fingerprint\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"fingerprint\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='level' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"level\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"level\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='message' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"message\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"message\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='source' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"source\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"source\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_events\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='enabled' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"enabled\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"enabled\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='condition_level' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"condition_level\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"condition_level\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='condition_min_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"condition_min_count\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"condition_min_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='condition_window_seconds' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"condition_window_seconds\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"condition_window_seconds\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='action_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"action_type\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"action_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='cooldown_seconds' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"cooldown_seconds\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"cooldown_seconds\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='is_default' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"error_notification_rules\" WHERE \"is_default\" IS NULL) THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"is_default\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='visibility' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"visibility\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"visibility\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='max_concurrent_runs' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"max_concurrent_runs\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"max_concurrent_runs\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='lock_wait_timeout_seconds' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"lock_wait_timeout_seconds\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"lock_wait_timeout_seconds\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='node_timeout_seconds' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"node_timeout_seconds\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"node_timeout_seconds\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='max_duration_seconds' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"max_duration_seconds\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"max_duration_seconds\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='run_context_defaults' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"run_context_defaults\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"run_context_defaults\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='graph_nodes_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"graph_nodes_json\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"graph_nodes_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='stale_run_timeout_minutes' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"stale_run_timeout_minutes\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"stale_run_timeout_minutes\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='retry_policy' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"retry_policy\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"retry_policy\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='circuit_breaker_tripped' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"pipelines\" WHERE \"circuit_breaker_tripped\" IS NULL) THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"circuit_breaker_tripped\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_evidence' AND column_name='run_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_evidence\" WHERE \"run_id\" IS NULL) THEN ALTER TABLE public.\"run_evidence\" ALTER COLUMN \"run_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_evidence' AND column_name='node_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_evidence\" WHERE \"node_id\" IS NULL) THEN ALTER TABLE public.\"run_evidence\" ALTER COLUMN \"node_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_evidence' AND column_name='evidence_state' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_evidence\" WHERE \"evidence_state\" IS NULL) THEN ALTER TABLE public.\"run_evidence\" ALTER COLUMN \"evidence_state\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_evidence' AND column_name='evidence_written_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_evidence\" WHERE \"evidence_written_at\" IS NULL) THEN ALTER TABLE public.\"run_evidence\" ALTER COLUMN \"evidence_written_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_number_counters' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_number_counters\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"run_number_counters\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_number_counters' AND column_name='next_run_number' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"run_number_counters\" WHERE \"next_run_number\" IS NULL) THEN ALTER TABLE public.\"run_number_counters\" ALTER COLUMN \"next_run_number\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='run_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"hitl_claims\" WHERE \"run_id\" IS NULL) THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"run_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='gate_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"hitl_claims\" WHERE \"gate_id\" IS NULL) THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"gate_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='pipeline_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"hitl_claims\" WHERE \"pipeline_id\" IS NULL) THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"pipeline_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='expires_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"hitl_claims\" WHERE \"expires_at\" IS NULL) THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"expires_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"hitl_claims\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"hitl_claims\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"hitl_claims\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"hitl_claims\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='pipeline_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"pipeline_id\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"pipeline_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='snapshot_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"snapshot_id\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"snapshot_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='trigger_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"trigger_type\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"trigger_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='status' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"status\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"status\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='run_number' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"run_number\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"run_number\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='input_hash' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"input_hash\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"input_hash\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='cancellation_requested' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"cancellation_requested\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"cancellation_requested\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='langgraph_thread_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"langgraph_thread_id\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"langgraph_thread_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='claim_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"claim_count\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"claim_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='claim_token' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"claim_token\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"claim_token\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='ledger_written' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"ledger_written\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"ledger_written\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='node_attempt_count' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"runs\" WHERE \"node_attempt_count\" IS NULL) THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"node_attempt_count\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='pipeline_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"pipeline_id\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"pipeline_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='eval_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"eval_type\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"eval_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='config_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"config_json\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"config_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='failure_behaviour' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"failure_behaviour\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"failure_behaviour\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='pass_threshold' AND data_type <> 'numeric') THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"pass_threshold\" TYPE numeric(8,4) USING \"pass_threshold\"::numeric; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_definitions\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='run_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"run_id\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"run_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='eval_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"eval_id\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"eval_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='passed' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"passed\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"passed\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='evaluated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"evaluated_at\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"evaluated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='observed' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"eval_results\" WHERE \"observed\" IS NULL) THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"observed\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='pipeline_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"pipeline_id\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"pipeline_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='trigger_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"trigger_type\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"trigger_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='active' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"active\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"active\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='max_concurrent_runs' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"max_concurrent_runs\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"max_concurrent_runs\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='config_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"config_json\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"config_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"triggers\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='name' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"name\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"name\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='visibility' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"visibility\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"visibility\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='version' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"version\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"version\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='content_json' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"content_json\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"content_json\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='account_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"account_id\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"account_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"lifecycle_maps\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='trigger_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"trigger_id\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"trigger_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='trigger_type' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"trigger_type\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"trigger_type\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='raw_payload_hash' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"raw_payload_hash\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"raw_payload_hash\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='received_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"received_at\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"received_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='validation_result' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"validation_result\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"validation_result\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"id\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='organisation_id' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"organisation_id\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"organisation_id\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='created_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"created_at\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"created_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='updated_at' AND is_nullable='YES') AND NOT EXISTS (SELECT 1 FROM \"trigger_events\" WHERE \"updated_at\" IS NULL) THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"updated_at\" SET NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='agents' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='agents' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='chat_messages' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='chat_sessions' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='chat_sessions' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='connector_instances' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='connector_instances' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''active''::character varying') THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"status\" SET DEFAULT 'active'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='connector_instances' AND a.attname='tier' AND pg_get_expr(ad.adbin, ad.adrelid) = '''native''::character varying') THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"tier\" SET DEFAULT 'native'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='connector_instances' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='connector_instances' AND a.attname='visibility' AND pg_get_expr(ad.adbin, ad.adrelid) = '''org''::character varying') THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"visibility\" SET DEFAULT 'org'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='cost_components' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='cost_components' AND a.attname='enabled' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"enabled\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='cost_components' AND a.attname='sort_order' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"sort_order\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='cost_components' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='dismissals' AND a.attname='dismissed_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"dismissals\" ALTER COLUMN \"dismissed_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='environment_profiles' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='environment_profiles' AND a.attname='initialisation_strategy' AND pg_get_expr(ad.adbin, ad.adrelid) = '''git_clone''::character varying') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"initialisation_strategy\" SET DEFAULT 'git_clone'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='environment_profiles' AND a.attname='network_policy' AND pg_get_expr(ad.adbin, ad.adrelid) = '''outbound''::character varying') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"network_policy\" SET DEFAULT 'outbound'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='environment_profiles' AND a.attname='persistence_policy' AND pg_get_expr(ad.adbin, ad.adrelid) = '''ephemeral''::character varying') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"persistence_policy\" SET DEFAULT 'ephemeral'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='environment_profiles' AND a.attname='provider_type' AND pg_get_expr(ad.adbin, ad.adrelid) = '''local_docker''::character varying') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"provider_type\" SET DEFAULT 'local_docker'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='environment_profiles' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''active''::character varying') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"status\" SET DEFAULT 'active'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='environment_profiles' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='environment_profiles' AND a.attname='visibility' AND pg_get_expr(ad.adbin, ad.adrelid) = '''org''::character varying') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"visibility\" SET DEFAULT 'org'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_events' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_events' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''new''::character varying') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"status\" SET DEFAULT 'new'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_events' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_forwarder_configs' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_forwarder_configs' AND a.attname='enabled' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"enabled\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_forwarder_configs' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_groups' AND a.attname='count' AND pg_get_expr(ad.adbin, ad.adrelid) = '1') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"count\" SET DEFAULT 1; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_groups' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_groups' AND a.attname='first_seen' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"first_seen\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_groups' AND a.attname='last_seen' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"last_seen\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_groups' AND a.attname='level_peak' AND pg_get_expr(ad.adbin, ad.adrelid) = '''error''::character varying') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"level_peak\" SET DEFAULT 'error'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_groups' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''new''::character varying') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"status\" SET DEFAULT 'new'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_groups' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='action_type' AND pg_get_expr(ad.adbin, ad.adrelid) = '''in_app''::character varying') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"action_type\" SET DEFAULT 'in_app'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='condition_level' AND pg_get_expr(ad.adbin, ad.adrelid) = '''error''::character varying') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"condition_level\" SET DEFAULT 'error'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='condition_min_count' AND pg_get_expr(ad.adbin, ad.adrelid) = '1') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"condition_min_count\" SET DEFAULT 1; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='condition_window_seconds' AND pg_get_expr(ad.adbin, ad.adrelid) = '300') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"condition_window_seconds\" SET DEFAULT 300; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='cooldown_seconds' AND pg_get_expr(ad.adbin, ad.adrelid) = '300') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"cooldown_seconds\" SET DEFAULT 300; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='enabled' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"enabled\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='is_default' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"is_default\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='error_notification_rules' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='eval_definitions' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='eval_definitions' AND a.attname='failure_behaviour' AND pg_get_expr(ad.adbin, ad.adrelid) = '''warn''::character varying') THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"failure_behaviour\" SET DEFAULT 'warn'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='eval_definitions' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='eval_results' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='eval_results' AND a.attname='evaluated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"evaluated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='eval_results' AND a.attname='observed' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"observed\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='eval_results' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='feedback_records' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='feedback_records' AND a.attname='feedback_handler_type' AND pg_get_expr(ad.adbin, ad.adrelid) = '''human''::character varying') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"feedback_handler_type\" SET DEFAULT 'human'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='feedback_records' AND a.attname='feedback_status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''pending''::character varying') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"feedback_status\" SET DEFAULT 'pending'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='feedback_records' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='hitl_claims' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='hitl_claims' AND a.attname='expires_at' AND pg_get_expr(ad.adbin, ad.adrelid) = '(now() + ''00:15:00''::interval)') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"expires_at\" SET DEFAULT (now() + '00:15:00'::interval); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='hitl_claims' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='journeys' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='journeys' AND a.attname='run_count' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"run_count\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='journeys' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='lifecycle_map_stages' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='lifecycle_map_stages' AND a.attname='position' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"position\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='lifecycle_map_stages' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='lifecycle_map_stages' AND a.attname='version' AND pg_get_expr(ad.adbin, ad.adrelid) = '1') THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"version\" SET DEFAULT 1; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='lifecycle_maps' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='lifecycle_maps' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='lifecycle_maps' AND a.attname='version' AND pg_get_expr(ad.adbin, ad.adrelid) = '1') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"version\" SET DEFAULT 1; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='lifecycle_maps' AND a.attname='visibility' AND pg_get_expr(ad.adbin, ad.adrelid) = '''org''::character varying') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"visibility\" SET DEFAULT 'org'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='model_backends' AND a.attname='cost_tracking' AND pg_get_expr(ad.adbin, ad.adrelid) = '''enabled''::character varying') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"cost_tracking\" SET DEFAULT 'enabled'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='model_backends' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='model_backends' AND a.attname='currency' AND pg_get_expr(ad.adbin, ad.adrelid) = '''USD''::character varying') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"currency\" SET DEFAULT 'USD'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='model_backends' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''active''::character varying') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"status\" SET DEFAULT 'active'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='model_backends' AND a.attname='tier' AND pg_get_expr(ad.adbin, ad.adrelid) = '''native''::character varying') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"tier\" SET DEFAULT 'native'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='model_backends' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='model_backends' AND a.attname='visibility' AND pg_get_expr(ad.adbin, ad.adrelid) = '''org''::character varying') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"visibility\" SET DEFAULT 'org'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='modulo_journey_facts' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='modulo_journey_facts' AND a.attname='finalise_attempts' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"finalise_attempts\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='modulo_journey_facts' AND a.attname='parse_failures' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"modulo_journey_facts\" ALTER COLUMN \"parse_failures\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='node_observations' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='node_observations' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='nodes' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='nodes' AND a.attname='timeout_seconds' AND pg_get_expr(ad.adbin, ad.adrelid) = '300') THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"timeout_seconds\" SET DEFAULT 300; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='nodes' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_delivery_log' AND a.attname='attempt_count' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"attempt_count\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_delivery_log' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_delivery_log' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''delivered''::character varying') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"status\" SET DEFAULT 'delivered'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_delivery_log' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_endpoints' AND a.attname='auto_disabled' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"auto_disabled\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_endpoints' AND a.attname='consecutive_dead_letter_count' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"consecutive_dead_letter_count\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_endpoints' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_endpoints' AND a.attname='events' AND pg_get_expr(ad.adbin, ad.adrelid) = '''[]''::json') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"events\" SET DEFAULT '[]'::json; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notification_endpoints' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notifications' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notifications' AND a.attname='dismiss_strategy' AND pg_get_expr(ad.adbin, ad.adrelid) = '''user_only''::character varying') THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"dismiss_strategy\" SET DEFAULT 'user_only'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notifications' AND a.attname='dismissible_at_scope' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"dismissible_at_scope\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notifications' AND a.attname='expires_at' AND pg_get_expr(ad.adbin, ad.adrelid) = '(now() + ''90 days''::interval)') THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"expires_at\" SET DEFAULT (now() + '90 days'::interval); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='notifications' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_daily_run_counts' AND a.attname='clamped' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"clamped\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_daily_run_counts' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_daily_run_counts' AND a.attname='refused_spend_usd' AND pg_get_expr(ad.adbin, ad.adrelid) = '''0''::numeric') THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"refused_spend_usd\" SET DEFAULT '0'::numeric; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_daily_run_counts' AND a.attname='run_count' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"run_count\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_daily_run_counts' AND a.attname='total_spend_usd' AND pg_get_expr(ad.adbin, ad.adrelid) = '''0''::numeric') THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"total_spend_usd\" SET DEFAULT '0'::numeric; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='org_daily_run_counts' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_edges' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_edges' AND a.attname='edge_type' AND pg_get_expr(ad.adbin, ad.adrelid) = '''normal''::character varying') THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"edge_type\" SET DEFAULT 'normal'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_edges' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_folders' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_folders' AND a.attname='sort_order' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"sort_order\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_folders' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_snapshots' AND a.attname='config_json' AND pg_get_expr(ad.adbin, ad.adrelid) = '''{}''::json') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"config_json\" SET DEFAULT '{}'::json; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_snapshots' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipeline_snapshots' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='circuit_breaker_tripped' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"circuit_breaker_tripped\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='default_autonomy_level' AND pg_get_expr(ad.adbin, ad.adrelid) = '''manual_approval''::character varying') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"default_autonomy_level\" SET DEFAULT 'manual_approval'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='graph_nodes_json' AND pg_get_expr(ad.adbin, ad.adrelid) = '''[]''::json') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"graph_nodes_json\" SET DEFAULT '[]'::json; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='lock_wait_timeout_seconds' AND pg_get_expr(ad.adbin, ad.adrelid) = '300') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"lock_wait_timeout_seconds\" SET DEFAULT 300; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='max_concurrent_runs' AND pg_get_expr(ad.adbin, ad.adrelid) = '5') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"max_concurrent_runs\" SET DEFAULT 5; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='max_duration_seconds' AND pg_get_expr(ad.adbin, ad.adrelid) = '3600') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"max_duration_seconds\" SET DEFAULT 3600; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='node_timeout_seconds' AND pg_get_expr(ad.adbin, ad.adrelid) = '300') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"node_timeout_seconds\" SET DEFAULT 300; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='retry_policy' AND pg_get_expr(ad.adbin, ad.adrelid) = '''{}''::json') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"retry_policy\" SET DEFAULT '{}'::json; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='stale_run_timeout_minutes' AND pg_get_expr(ad.adbin, ad.adrelid) = '30') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"stale_run_timeout_minutes\" SET DEFAULT 30; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='pipelines' AND a.attname='visibility' AND pg_get_expr(ad.adbin, ad.adrelid) = '''org''::character varying') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"visibility\" SET DEFAULT 'org'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='run_daily_facts' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='run_daily_facts' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='run_evidence' AND a.attname='evidence_written_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"run_evidence\" ALTER COLUMN \"evidence_written_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='run_number_counters' AND a.attname='next_run_number' AND pg_get_expr(ad.adbin, ad.adrelid) = '1') THEN ALTER TABLE public.\"run_number_counters\" ALTER COLUMN \"next_run_number\" SET DEFAULT 1; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='cancellation_requested' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"cancellation_requested\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='claim_count' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"claim_count\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='claim_token' AND pg_get_expr(ad.adbin, ad.adrelid) = '(gen_random_uuid())::text') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"claim_token\" SET DEFAULT (gen_random_uuid())::text; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='ledger_written' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"ledger_written\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='node_attempt_count' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"node_attempt_count\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='run_number' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"run_number\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''pending''::character varying') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"status\" SET DEFAULT 'pending'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='runs' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='scheduled_reports' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='scheduled_reports' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='snapshot_schema_pins' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='snapshot_schema_pins' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"snapshot_schema_pins\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='spend_anomalies' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='spend_anomalies' AND a.attname='dismissed' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"dismissed\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='spend_anomalies' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='trigger_events' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='trigger_events' AND a.attname='received_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"received_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='trigger_events' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='triggers' AND a.attname='active' AND pg_get_expr(ad.adbin, ad.adrelid) = 'true') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"active\" SET DEFAULT true; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='triggers' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='triggers' AND a.attname='max_concurrent_runs' AND pg_get_expr(ad.adbin, ad.adrelid) = '1') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"max_concurrent_runs\" SET DEFAULT 1; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='triggers' AND a.attname='streak_epoch' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"streak_epoch\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='triggers' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='variant_groups' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='variant_groups' AND a.attname='degraded_evals' AND pg_get_expr(ad.adbin, ad.adrelid) = 'false') THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"degraded_evals\" SET DEFAULT false; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='variant_groups' AND a.attname='max_concurrent_runs' AND pg_get_expr(ad.adbin, ad.adrelid) = '5') THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"max_concurrent_runs\" SET DEFAULT 5; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='variant_groups' AND a.attname='run_count' AND pg_get_expr(ad.adbin, ad.adrelid) = '0') THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"run_count\" SET DEFAULT 0; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='variant_groups' AND a.attname='selection_strategy' AND pg_get_expr(ad.adbin, ad.adrelid) = '''weighted''::character varying') THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"selection_strategy\" SET DEFAULT 'weighted'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='variant_groups' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='web_vital_events' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='web_vital_events' AND a.attname='recorded_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"recorded_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='web_vital_events' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='webhook_dedup_hashes' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='webhook_dedup_hashes' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"webhook_dedup_hashes\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='webhook_payloads' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='webhook_payloads' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='workspace_leases' AND a.attname='created_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"created_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='workspace_leases' AND a.attname='lease_expires_at' AND pg_get_expr(ad.adbin, ad.adrelid) = '(now() + ''00:30:00''::interval)') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"lease_expires_at\" SET DEFAULT (now() + '00:30:00'::interval); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='workspace_leases' AND a.attname='status' AND pg_get_expr(ad.adbin, ad.adrelid) = '''pending''::character varying') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"status\" SET DEFAULT 'pending'::character varying; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attrdef ad JOIN pg_class c ON c.oid=ad.adrelid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ad.adnum WHERE c.relname='workspace_leases' AND a.attname='updated_at' AND pg_get_expr(ad.adbin, ad.adrelid) = 'CURRENT_TIMESTAMP') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"updated_at\" SET DEFAULT CURRENT_TIMESTAMP; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='agent_command' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"agent_command\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='agent_commands' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"agent_commands\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='evals' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"evals\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='input_schema_id' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"input_schema_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='input_schema_version' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"input_schema_version\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='library_id' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"library_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='max_input_length' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"max_input_length\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='model_backend_id' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"model_backend_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='output_schema_id' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"output_schema_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='output_schema_version' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"output_schema_version\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='parameter_schema_id' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"parameter_schema_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='template_id' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"template_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='agents' AND column_name='token_budget' AND is_nullable='NO') THEN ALTER TABLE public.\"agents\" ALTER COLUMN \"token_budget\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='content' AND is_nullable='NO') THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"content\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='parent_id' AND is_nullable='NO') THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"parent_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='token_count' AND is_nullable='NO') THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"token_count\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='tool_calls_json' AND is_nullable='NO') THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"tool_calls_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_messages' AND column_name='tool_results_json' AND is_nullable='NO') THEN ALTER TABLE public.\"chat_messages\" ALTER COLUMN \"tool_results_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='name' AND is_nullable='NO') THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"name\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chat_sessions' AND column_name='system_prompt_hash' AND is_nullable='NO') THEN ALTER TABLE public.\"chat_sessions\" ALTER COLUMN \"system_prompt_hash\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='last_health_check_at' AND is_nullable='NO') THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"last_health_check_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='last_health_check_error' AND is_nullable='NO') THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"last_health_check_error\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='connector_instances' AND column_name='owner_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"connector_instances\" ALTER COLUMN \"owner_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='formula' AND is_nullable='NO') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"formula\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='rate_fallback' AND is_nullable='NO') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"rate_fallback\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='rate_usd' AND is_nullable='NO') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"rate_usd\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='cost_components' AND column_name='report_key' AND is_nullable='NO') THEN ALTER TABLE public.\"cost_components\" ALTER COLUMN \"report_key\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='image_ref' AND is_nullable='NO') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"image_ref\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='environment_profiles' AND column_name='owner_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"environment_profiles\" ALTER COLUMN \"owner_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='context_json' AND is_nullable='NO') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"context_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='environment' AND is_nullable='NO') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"environment\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='resolved_at' AND is_nullable='NO') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"resolved_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='signal' AND is_nullable='NO') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"signal\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='stacktrace' AND is_nullable='NO') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"stacktrace\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_events' AND column_name='version' AND is_nullable='NO') THEN ALTER TABLE public.\"error_events\" ALTER COLUMN \"version\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='config_json' AND is_nullable='NO') THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"config_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='last_test_at' AND is_nullable='NO') THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"last_test_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_forwarder_configs' AND column_name='last_test_ok' AND is_nullable='NO') THEN ALTER TABLE public.\"error_forwarder_configs\" ALTER COLUMN \"last_test_ok\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='assigned_to' AND is_nullable='NO') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"assigned_to\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='resolved_at' AND is_nullable='NO') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"resolved_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_groups' AND column_name='sample_event_id' AND is_nullable='NO') THEN ALTER TABLE public.\"error_groups\" ALTER COLUMN \"sample_event_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='signal' AND is_nullable='NO') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"signal\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='error_notification_rules' AND column_name='webhook_url' AND is_nullable='NO') THEN ALTER TABLE public.\"error_notification_rules\" ALTER COLUMN \"webhook_url\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='node_id' AND is_nullable='NO') THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"node_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='pass_threshold' AND is_nullable='NO') THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"pass_threshold\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_definitions' AND column_name='suite_id' AND is_nullable='NO') THEN ALTER TABLE public.\"eval_definitions\" ALTER COLUMN \"suite_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='detail' AND is_nullable='NO') THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"detail\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='node_id' AND is_nullable='NO') THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"node_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='eval_results' AND column_name='score' AND is_nullable='NO') THEN ALTER TABLE public.\"eval_results\" ALTER COLUMN \"score\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='annotation' AND is_nullable='NO') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"annotation\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='correction_run_id' AND is_nullable='NO') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"correction_run_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='eval_gap' AND is_nullable='NO') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"eval_gap\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='needs_human_review' AND is_nullable='NO') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"needs_human_review\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='feedback_records' AND column_name='producing_agent_id' AND is_nullable='NO') THEN ALTER TABLE public.\"feedback_records\" ALTER COLUMN \"producing_agent_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='claim_token' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"claim_token\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='claimed_at' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"claimed_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='decision' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"decision\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='decision_at' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"decision_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='decision_payload' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"decision_payload\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='delivered_at' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"delivered_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='overdue_notified_at' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"overdue_notified_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='hitl_claims' AND column_name='required_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"hitl_claims\" ALTER COLUMN \"required_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='latest_provenance' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"latest_provenance\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='latest_status' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"latest_status\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='latest_terminal_run_id' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"latest_terminal_run_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='map_id' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"map_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='map_version' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"map_version\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='owner_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"owner_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='position' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"position\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='stage_id' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"stage_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='journeys' AND column_name='stage_name' AND is_nullable='NO') THEN ALTER TABLE public.\"journeys\" ALTER COLUMN \"stage_name\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_map_stages' AND column_name='pipeline_id' AND is_nullable='NO') THEN ALTER TABLE public.\"lifecycle_map_stages\" ALTER COLUMN \"pipeline_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='archived_at' AND is_nullable='NO') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"archived_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='owner_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"owner_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='lifecycle_maps' AND column_name='updated_by' AND is_nullable='NO') THEN ALTER TABLE public.\"lifecycle_maps\" ALTER COLUMN \"updated_by\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='fallback_backend_ids' AND is_nullable='NO') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"fallback_backend_ids\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='last_health_check_at' AND is_nullable='NO') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"last_health_check_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='last_health_check_error' AND is_nullable='NO') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"last_health_check_error\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='model_backends' AND column_name='owner_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"model_backends\" ALTER COLUMN \"owner_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='node_observations' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"node_observations\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='parent_node_id' AND is_nullable='NO') THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"parent_node_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='retry_count' AND is_nullable='NO') THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"retry_count\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='nodes' AND column_name='retry_delay_seconds' AND is_nullable='NO') THEN ALTER TABLE public.\"nodes\" ALTER COLUMN \"retry_delay_seconds\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='endpoint_id' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"endpoint_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='failed_at' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"failed_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='last_error' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"last_error\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='payload_ciphertext' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"payload_ciphertext\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='response_body' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"response_body\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='response_code' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"response_code\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_delivery_log' AND column_name='run_id' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_delivery_log\" ALTER COLUMN \"run_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='disabled_at' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"disabled_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='secret_ciphertext' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"secret_ciphertext\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notification_endpoints' AND column_name='team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"notification_endpoints\" ALTER COLUMN \"team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='action_url' AND is_nullable='NO') THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"action_url\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='notifications' AND column_name='target_user_id' AND is_nullable='NO') THEN ALTER TABLE public.\"notifications\" ALTER COLUMN \"target_user_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='org_daily_run_counts' AND column_name='team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"org_daily_run_counts\" ALTER COLUMN \"team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='condition_expression' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"condition_expression\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_edges' AND column_name='hitl_gate_config' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_edges\" ALTER COLUMN \"hitl_gate_config\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_folders' AND column_name='parent_id' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_folders\" ALTER COLUMN \"parent_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='composite_bindings_json' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"composite_bindings_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='default_autonomy_level' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"default_autonomy_level\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='environment_profile_id' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"environment_profile_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='guardrail_pins_json' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"guardrail_pins_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='notes' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"notes\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='parameter_bindings_json' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"parameter_bindings_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_snapshots' AND column_name='tag' AND is_nullable='NO') THEN ALTER TABLE public.\"pipeline_snapshots\" ALTER COLUMN \"tag\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='archived_at' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"archived_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='circuit_breaker_threshold' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"circuit_breaker_threshold\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='circuit_breaker_tripped_at' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"circuit_breaker_tripped_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='default_autonomy_level' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"default_autonomy_level\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='default_feedback_handler' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"default_feedback_handler\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='folder_id' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"folder_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='max_steps' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"max_steps\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='owner_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"owner_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='rate_limit_config' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"rate_limit_config\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipelines' AND column_name='token_budget' AND is_nullable='NO') THEN ALTER TABLE public.\"pipelines\" ALTER COLUMN \"token_budget\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='cancellation_requested' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"cancellation_requested\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='claim_count' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"claim_count\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='completed_at' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"completed_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='dispatched_at' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"dispatched_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='dispatcher' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"dispatcher\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='duration_ms' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"duration_ms\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='error_code' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"error_code\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='final_idle_ms' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"final_idle_ms\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='folder_id' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"folder_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='max_node_timeout_seconds' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"max_node_timeout_seconds\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='node_count' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"node_count\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='output_bytes' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"output_bytes\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='parent_run_id' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"parent_run_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='pipeline_id' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"pipeline_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='pipeline_name' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"pipeline_name\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='queue_wait_ms' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"queue_wait_ms\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='rate_limited' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"rate_limited\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='run_number' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"run_number\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='sandbox_agent_node_count' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"sandbox_agent_node_count\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='snapshot_id' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"snapshot_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='started_at' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"started_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='team_name' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"team_name\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='telemetry_bytes' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"telemetry_bytes\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='total_cost_usd' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"total_cost_usd\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='total_queue_wait_ms' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"total_queue_wait_ms\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_daily_facts' AND column_name='total_tokens' AND is_nullable='NO') THEN ALTER TABLE public.\"run_daily_facts\" ALTER COLUMN \"total_tokens\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='run_evidence' AND column_name='evidence_detail' AND is_nullable='NO') THEN ALTER TABLE public.\"run_evidence\" ALTER COLUMN \"evidence_detail\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='account_id' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"account_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='claimed_by' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"claimed_by\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='completed_at' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"completed_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='cost_breakdown' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"cost_breakdown\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='dispatched_at' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"dispatched_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='dispatcher' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"dispatcher\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='enqueue_failed_at' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"enqueue_failed_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='error_code' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"error_code\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='error_detail' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"error_detail\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='heartbeat_at' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"heartbeat_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='input_payload' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"input_payload\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='is_replay' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"is_replay\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='ledger_refused_at' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"ledger_refused_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='node_telemetry_json' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"node_telemetry_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='node_token_usage' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"node_token_usage\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='outputs_json' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"outputs_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='owner_team_id' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"owner_team_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='parent_run_id' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"parent_run_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='rate_limit_key' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"rate_limit_key\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='raw_output_markers' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"raw_output_markers\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='run_classification' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"run_classification\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='sandbox_dispatch_state' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"sandbox_dispatch_state\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='sandbox_id' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"sandbox_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='saq_job_id' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"saq_job_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='started_at' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"started_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='total_cost_usd' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"total_cost_usd\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='total_tokens' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"total_tokens\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='trigger_id' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"trigger_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='variant_group_id' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"variant_group_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='work_intact' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"work_intact\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='work_item_id' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"work_item_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='runs' AND column_name='work_item_refs' AND is_nullable='NO') THEN ALTER TABLE public.\"runs\" ALTER COLUMN \"work_item_refs\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='config_json' AND is_nullable='NO') THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"config_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='created_by' AND is_nullable='NO') THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"created_by\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='last_sent_at' AND is_nullable='NO') THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"last_sent_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='next_send_at' AND is_nullable='NO') THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"next_send_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='scheduled_reports' AND column_name='recipient_config' AND is_nullable='NO') THEN ALTER TABLE public.\"scheduled_reports\" ALTER COLUMN \"recipient_config\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='spend_anomalies' AND column_name='pipeline_id' AND is_nullable='NO') THEN ALTER TABLE public.\"spend_anomalies\" ALTER COLUMN \"pipeline_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='error_detail' AND is_nullable='NO') THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"error_detail\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='trigger_events' AND column_name='run_id' AND is_nullable='NO') THEN ALTER TABLE public.\"trigger_events\" ALTER COLUMN \"run_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='cron_expression' AND is_nullable='NO') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"cron_expression\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='cron_timezone' AND is_nullable='NO') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"cron_timezone\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='daily_spend_limit' AND is_nullable='NO') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"daily_spend_limit\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='last_fired_at' AND is_nullable='NO') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"last_fired_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='next_fire_at' AND is_nullable='NO') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"next_fire_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='triggers' AND column_name='streak_epoch' AND is_nullable='NO') THEN ALTER TABLE public.\"triggers\" ALTER COLUMN \"streak_epoch\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='deleted_at' AND is_nullable='NO') THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"deleted_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='variant_groups' AND column_name='description' AND is_nullable='NO') THEN ALTER TABLE public.\"variant_groups\" ALTER COLUMN \"description\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='metric_rating' AND is_nullable='NO') THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"metric_rating\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='navigation_type' AND is_nullable='NO') THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"navigation_type\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='page_url' AND is_nullable='NO') THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"page_url\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='web_vital_events' AND column_name='route_path' AND is_nullable='NO') THEN ALTER TABLE public.\"web_vital_events\" ALTER COLUMN \"route_path\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='webhook_payloads' AND column_name='trigger_event_id' AND is_nullable='NO') THEN ALTER TABLE public.\"webhook_payloads\" ALTER COLUMN \"trigger_event_id\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='error_message' AND is_nullable='NO') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"error_message\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='lease_started_at' AND is_nullable='NO') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"lease_started_at\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='output_artifact_refs_json' AND is_nullable='NO') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"output_artifact_refs_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='repository_ref' AND is_nullable='NO') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"repository_ref\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='repository_url' AND is_nullable='NO') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"repository_url\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='workspace_leases' AND column_name='resource_usage_json' AND is_nullable='NO') THEN ALTER TABLE public.\"workspace_leases\" ALTER COLUMN \"resource_usage_json\" DROP NOT NULL; END IF; END $$;"
    )
    op.execute(
        "ALTER TABLE public.org_daily_run_counts DROP CONSTRAINT IF EXISTS uq_org_daily_run_counts_org_team_date;"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_account_id ON public.agents USING btree (account_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agents_organisation_id ON public.agents USING btree (organisation_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_organisation_id ON public.chat_messages USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_session_id ON public.chat_messages USING btree (session_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_organisation_id ON public.chat_sessions USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_id ON public.chat_sessions USING btree (user_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_instances_account_id ON public.connector_instances USING btree (account_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_instances_connector_type_id ON public.connector_instances USING btree (connector_type_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_instances_organisation_id ON public.connector_instances USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cost_components_org_enabled_sort ON public.cost_components USING btree (organisation_id, enabled, sort_order);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cost_components_organisation_id ON public.cost_components USING btree (organisation_id);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_components_org_name_active ON public.cost_components USING btree (organisation_id, name) WHERE (deleted_at IS NULL);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_components_org_report_key_self ON public.cost_components USING btree (organisation_id, report_key) WHERE (((kind)::text = 'self_reported'::text) AND (deleted_at IS NULL));"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dismissals_notification_id ON public.dismissals USING btree (notification_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_environment_profiles_organisation_id ON public.environment_profiles USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_error_events_org_fingerprint_created_at ON public.error_events USING btree (organisation_id, fingerprint, created_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_error_events_organisation_id ON public.error_events USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_error_forwarder_configs_organisation_id ON public.error_forwarder_configs USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_error_groups_organisation_id ON public.error_groups USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_error_notification_rules_organisation_id ON public.error_notification_rules USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_definitions_organisation_id ON public.eval_definitions USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_definitions_pipeline_id ON public.eval_definitions USING btree (pipeline_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_results_organisation_id ON public.eval_results USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_eval_results_run_id ON public.eval_results USING btree (run_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_feedback_records_account_id ON public.feedback_records USING btree (account_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_feedback_records_organisation_id ON public.feedback_records USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_feedback_records_run_id ON public.feedback_records USING btree (run_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hitl_claims_organisation_id ON public.hitl_claims USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_hitl_claims_pipeline_id ON public.hitl_claims USING btree (pipeline_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hitl_claims_run_id ON public.hitl_claims USING btree (run_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_journeys_canonical_work_item_id ON public.journeys USING btree (canonical_work_item_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_journeys_organisation_id ON public.journeys USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_lifecycle_map_stages_map_id ON public.lifecycle_map_stages USING btree (map_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_lifecycle_map_stages_organisation_id ON public.lifecycle_map_stages USING btree (organisation_id);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_lifecycle_map_stages_active_pipeline ON public.lifecycle_map_stages USING btree (organisation_id, pipeline_id) WHERE (pipeline_id IS NOT NULL);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_lifecycle_maps_account_id ON public.lifecycle_maps USING btree (account_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_lifecycle_maps_organisation_id ON public.lifecycle_maps USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_model_backends_organisation_id ON public.model_backends USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_modulo_journey_facts_org_created ON public.modulo_journey_facts USING btree (organisation_id, created_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_modulo_journey_facts_organisation_id ON public.modulo_journey_facts USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_node_observations_account_id ON public.node_observations USING btree (account_id) WHERE (account_id IS NOT NULL);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_node_observations_organisation_id ON public.node_observations USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_node_observations_run_id ON public.node_observations USING btree (run_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_nodes_organisation_id ON public.nodes USING btree (organisation_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_nodes_parent_node_id ON public.nodes USING btree (parent_node_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_nodes_pipeline_id ON public.nodes USING btree (pipeline_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notification_delivery_log_endpoint_id ON public.notification_delivery_log USING btree (endpoint_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notification_delivery_log_event_type ON public.notification_delivery_log USING btree (event_type);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notification_delivery_log_organisation_id ON public.notification_delivery_log USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notification_delivery_log_run_id ON public.notification_delivery_log USING btree (run_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notification_endpoints_organisation_id ON public.notification_endpoints USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_organisation_id ON public.notifications USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_target_user_id ON public.notifications USING btree (target_user_id) WHERE (target_user_id IS NOT NULL);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_org_daily_run_counts_org_date ON public.org_daily_run_counts USING btree (organisation_id, run_date);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_org_daily_run_counts_organisation_id ON public.org_daily_run_counts USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_org_daily_run_counts_run_date ON public.org_daily_run_counts USING btree (run_date);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_daily_run_counts ON public.org_daily_run_counts USING btree (organisation_id, team_id, run_date) NULLS NOT DISTINCT;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipeline_edges_organisation_id ON public.pipeline_edges USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipeline_edges_pipeline_id ON public.pipeline_edges USING btree (pipeline_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipeline_folders_organisation_id ON public.pipeline_folders USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipeline_folders_parent_id ON public.pipeline_folders USING btree (parent_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipeline_snapshots_account_id ON public.pipeline_snapshots USING btree (account_id) WHERE (account_id IS NOT NULL);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipeline_snapshots_environment_profile_id ON public.pipeline_snapshots USING btree (environment_profile_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipeline_snapshots_organisation_id ON public.pipeline_snapshots USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipeline_snapshots_pipeline_id ON public.pipeline_snapshots USING btree (pipeline_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_pipelines_folder_id ON public.pipelines USING btree (folder_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pipelines_organisation_id ON public.pipelines USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_run_daily_facts_org_date ON public.run_daily_facts USING btree (organisation_id, run_date);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_daily_facts_run_id ON public.run_daily_facts USING btree (run_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_dispatcher ON public.runs USING btree (dispatcher);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_org_work_item_id ON public.runs USING btree (organisation_id, work_item_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_organisation_id ON public.runs USING btree (organisation_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_parent_run_id ON public.runs USING btree (parent_run_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_pipeline_id ON public.runs USING btree (pipeline_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_probe ON public.runs USING btree (organisation_id, started_at);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_rate_limit_key ON public.runs USING btree (rate_limit_key);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_refusal ON public.runs USING btree (organisation_id, created_at);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_streak_engine ON public.runs USING btree (trigger_id, completed_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_trigger_id_created_at ON public.runs USING btree (trigger_id, created_at);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_trigger_id_status ON public.runs USING btree (trigger_id, status);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_unclassified_terminal ON public.runs USING btree (status, completed_at DESC) WHERE (run_classification IS NULL);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_work_item_refs_gin ON public.runs USING gin (work_item_refs) WHERE (jsonb_array_length(work_item_refs) > 0);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_scheduled_reports_created_by ON public.scheduled_reports USING btree (created_by) WHERE (created_by IS NOT NULL);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_scheduled_reports_organisation_id ON public.scheduled_reports USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_scheduled_reports_report_type ON public.scheduled_reports USING btree (report_type);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ssp_schema ON public.snapshot_schema_pins USING btree (schema_id, schema_version);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_ssp_snapshot ON public.snapshot_schema_pins USING btree (snapshot_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_snapshot_schema_pins_organisation_id ON public.snapshot_schema_pins USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_spend_anomalies_anomaly_date ON public.spend_anomalies USING btree (anomaly_date);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_spend_anomalies_organisation_id ON public.spend_anomalies USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trigger_events_organisation_id ON public.trigger_events USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trigger_events_received_at ON public.trigger_events USING btree (received_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trigger_events_trigger_id ON public.trigger_events USING btree (trigger_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_triggers_next_fire_at ON public.triggers USING btree (next_fire_at);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_triggers_organisation_id ON public.triggers USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_triggers_pipeline_id ON public.triggers USING btree (pipeline_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_variant_groups_degraded_evals ON public.variant_groups USING btree (degraded_evals);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_variant_groups_organisation_id ON public.variant_groups USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_variant_groups_pipeline_id ON public.variant_groups USING btree (pipeline_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_web_vital_events_metric_name ON public.web_vital_events USING btree (metric_name);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_web_vital_events_metric_name_recorded ON public.web_vital_events USING btree (metric_name, recorded_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_web_vital_events_organisation_id ON public.web_vital_events USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webhook_dedup_hashes_expires_at ON public.webhook_dedup_hashes USING btree (expires_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webhook_dedup_hashes_organisation_id ON public.webhook_dedup_hashes USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webhook_dedup_hashes_trigger_id ON public.webhook_dedup_hashes USING btree (trigger_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webhook_payloads_expires_at ON public.webhook_payloads USING btree (expires_at);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webhook_payloads_organisation_id ON public.webhook_payloads USING btree (organisation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_leases_environment_profile_id ON public.workspace_leases USING btree (environment_profile_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_leases_organisation_id ON public.workspace_leases USING btree (organisation_id);"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_workspace_leases_run_id ON public.workspace_leases USING btree (run_id);")
    op.execute("DROP TRIGGER IF EXISTS trg_pipelines_stage_id_tenant ON public.pipelines;")
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='error_events_no_delete') THEN CREATE TRIGGER error_events_no_delete BEFORE DELETE ON public.error_events FOR EACH ROW EXECUTE FUNCTION public.error_events_append_only(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='error_events_no_update') THEN CREATE TRIGGER error_events_no_update BEFORE UPDATE ON public.error_events FOR EACH ROW EXECUTE FUNCTION public.error_events_append_only(); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_agents_account_id_tenant') THEN CREATE TRIGGER trg_agents_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_agents_input_schema_id_tenant') THEN CREATE TRIGGER trg_agents_input_schema_id_tenant BEFORE INSERT OR UPDATE OF input_schema_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('schemas', 'input_schema_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_agents_library_id_tenant') THEN CREATE TRIGGER trg_agents_library_id_tenant BEFORE INSERT OR UPDATE OF library_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'library_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_agents_model_backend_id_tenant') THEN CREATE TRIGGER trg_agents_model_backend_id_tenant BEFORE INSERT OR UPDATE OF model_backend_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('model_backends', 'model_backend_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_agents_output_schema_id_tenant') THEN CREATE TRIGGER trg_agents_output_schema_id_tenant BEFORE INSERT OR UPDATE OF output_schema_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('schemas', 'output_schema_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_chat_messages_parent_id_tenant') THEN CREATE TRIGGER trg_chat_messages_parent_id_tenant BEFORE INSERT OR UPDATE OF parent_id, organisation_id ON public.chat_messages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('chat_messages', 'parent_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_chat_messages_session_id_tenant') THEN CREATE TRIGGER trg_chat_messages_session_id_tenant BEFORE INSERT OR UPDATE OF session_id, organisation_id ON public.chat_messages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('chat_sessions', 'session_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_chat_sessions_user_id_tenant') THEN CREATE TRIGGER trg_chat_sessions_user_id_tenant BEFORE INSERT OR UPDATE OF user_id, organisation_id ON public.chat_sessions FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'user_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_connector_instances_account_id_tenant') THEN CREATE TRIGGER trg_connector_instances_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.connector_instances FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_connector_instances_owner_team_id_tenant') THEN CREATE TRIGGER trg_connector_instances_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.connector_instances FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_environment_profiles_account_id_tenant') THEN CREATE TRIGGER trg_environment_profiles_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.environment_profiles FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_environment_profiles_owner_team_id_tenant') THEN CREATE TRIGGER trg_environment_profiles_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.environment_profiles FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_error_groups_assigned_to_tenant') THEN CREATE TRIGGER trg_error_groups_assigned_to_tenant BEFORE INSERT OR UPDATE OF assigned_to, organisation_id ON public.error_groups FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'assigned_to'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_error_groups_sample_event_id_tenant') THEN CREATE TRIGGER trg_error_groups_sample_event_id_tenant BEFORE INSERT OR UPDATE OF sample_event_id, organisation_id ON public.error_groups FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('error_events', 'sample_event_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_eval_definitions_account_id_tenant') THEN CREATE TRIGGER trg_eval_definitions_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.eval_definitions FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_eval_definitions_pipeline_id_tenant') THEN CREATE TRIGGER trg_eval_definitions_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.eval_definitions FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_eval_results_eval_id_tenant') THEN CREATE TRIGGER trg_eval_results_eval_id_tenant BEFORE INSERT OR UPDATE OF eval_id, organisation_id ON public.eval_results FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('eval_definitions', 'eval_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_eval_results_run_id_tenant') THEN CREATE TRIGGER trg_eval_results_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.eval_results FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_feedback_records_account_id_tenant') THEN CREATE TRIGGER trg_feedback_records_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.feedback_records FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_feedback_records_correction_run_id_tenant') THEN CREATE TRIGGER trg_feedback_records_correction_run_id_tenant BEFORE INSERT OR UPDATE OF correction_run_id, organisation_id ON public.feedback_records FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'correction_run_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_feedback_records_producing_agent_id_tenant') THEN CREATE TRIGGER trg_feedback_records_producing_agent_id_tenant BEFORE INSERT OR UPDATE OF producing_agent_id, organisation_id ON public.feedback_records FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('agents', 'producing_agent_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_feedback_records_run_id_tenant') THEN CREATE TRIGGER trg_feedback_records_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.feedback_records FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_hitl_claims_account_id_tenant') THEN CREATE TRIGGER trg_hitl_claims_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.hitl_claims FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_hitl_claims_pipeline_id_tenant') THEN CREATE TRIGGER trg_hitl_claims_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.hitl_claims FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_hitl_claims_required_team_id_tenant') THEN CREATE TRIGGER trg_hitl_claims_required_team_id_tenant BEFORE INSERT OR UPDATE OF required_team_id, organisation_id ON public.hitl_claims FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'required_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_hitl_claims_run_id_tenant') THEN CREATE TRIGGER trg_hitl_claims_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.hitl_claims FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_journeys_owner_team_id_tenant') THEN CREATE TRIGGER trg_journeys_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.journeys FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_lifecycle_map_stages_account_id_tenant') THEN CREATE TRIGGER trg_lifecycle_map_stages_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.lifecycle_map_stages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_lifecycle_map_stages_map_id_tenant') THEN CREATE TRIGGER trg_lifecycle_map_stages_map_id_tenant BEFORE INSERT OR UPDATE OF map_id, organisation_id ON public.lifecycle_map_stages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('lifecycle_maps', 'map_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_lifecycle_map_stages_pipeline_id_tenant') THEN CREATE TRIGGER trg_lifecycle_map_stages_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.lifecycle_map_stages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_lifecycle_maps_account_id_tenant') THEN CREATE TRIGGER trg_lifecycle_maps_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.lifecycle_maps FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_lifecycle_maps_owner_team_id_tenant') THEN CREATE TRIGGER trg_lifecycle_maps_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.lifecycle_maps FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_model_backends_account_id_tenant') THEN CREATE TRIGGER trg_model_backends_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.model_backends FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_model_backends_owner_team_id_tenant') THEN CREATE TRIGGER trg_model_backends_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.model_backends FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_node_observations_account_id_tenant') THEN CREATE TRIGGER trg_node_observations_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.node_observations FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_node_observations_run_id_tenant') THEN CREATE TRIGGER trg_node_observations_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.node_observations FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_nodes_account_id_tenant') THEN CREATE TRIGGER trg_nodes_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_nodes_parent_node_id_tenant') THEN CREATE TRIGGER trg_nodes_parent_node_id_tenant BEFORE INSERT OR UPDATE OF parent_node_id, organisation_id ON public.nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('nodes', 'parent_node_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_nodes_pipeline_id_tenant') THEN CREATE TRIGGER trg_nodes_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_notification_delivery_log_endpoint_id_tenant') THEN CREATE TRIGGER trg_notification_delivery_log_endpoint_id_tenant BEFORE INSERT OR UPDATE OF endpoint_id, organisation_id ON public.notification_delivery_log FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('notification_endpoints', 'endpoint_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_notification_delivery_log_run_id_tenant') THEN CREATE TRIGGER trg_notification_delivery_log_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.notification_delivery_log FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_notification_endpoints_account_id_tenant') THEN CREATE TRIGGER trg_notification_endpoints_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.notification_endpoints FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_notification_endpoints_team_id_tenant') THEN CREATE TRIGGER trg_notification_endpoints_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.notification_endpoints FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_notifications_target_user_id_tenant') THEN CREATE TRIGGER trg_notifications_target_user_id_tenant BEFORE INSERT OR UPDATE OF target_user_id, organisation_id ON public.notifications FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'target_user_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_org_daily_run_counts_team_id_tenant') THEN CREATE TRIGGER trg_org_daily_run_counts_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.org_daily_run_counts FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_pipeline_edges_pipeline_id_tenant') THEN CREATE TRIGGER trg_pipeline_edges_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.pipeline_edges FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_pipeline_snapshots_account_id_tenant') THEN CREATE TRIGGER trg_pipeline_snapshots_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.pipeline_snapshots FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_pipeline_snapshots_environment_profile_id_tenant') THEN CREATE TRIGGER trg_pipeline_snapshots_environment_profile_id_tenant BEFORE INSERT OR UPDATE OF environment_profile_id, organisation_id ON public.pipeline_snapshots FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('environment_profiles', 'environment_profile_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_pipeline_snapshots_pipeline_id_tenant') THEN CREATE TRIGGER trg_pipeline_snapshots_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.pipeline_snapshots FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_pipelines_account_id_tenant') THEN CREATE TRIGGER trg_pipelines_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.pipelines FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_pipelines_owner_team_id_tenant') THEN CREATE TRIGGER trg_pipelines_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.pipelines FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_runs_account_id_tenant') THEN CREATE TRIGGER trg_runs_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_runs_owner_team_id_tenant') THEN CREATE TRIGGER trg_runs_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_runs_parent_run_id_tenant') THEN CREATE TRIGGER trg_runs_parent_run_id_tenant BEFORE INSERT OR UPDATE OF parent_run_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'parent_run_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_runs_pipeline_id_tenant') THEN CREATE TRIGGER trg_runs_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_runs_snapshot_id_tenant') THEN CREATE TRIGGER trg_runs_snapshot_id_tenant BEFORE INSERT OR UPDATE OF snapshot_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipeline_snapshots', 'snapshot_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_runs_trigger_id_tenant') THEN CREATE TRIGGER trg_runs_trigger_id_tenant BEFORE INSERT OR UPDATE OF trigger_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('triggers', 'trigger_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_scheduled_reports_created_by_tenant') THEN CREATE TRIGGER trg_scheduled_reports_created_by_tenant BEFORE INSERT OR UPDATE OF created_by, organisation_id ON public.scheduled_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'created_by'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_spend_anomalies_pipeline_id_tenant') THEN CREATE TRIGGER trg_spend_anomalies_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.spend_anomalies FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_trigger_events_run_id_tenant') THEN CREATE TRIGGER trg_trigger_events_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.trigger_events FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_trigger_events_trigger_id_tenant') THEN CREATE TRIGGER trg_trigger_events_trigger_id_tenant BEFORE INSERT OR UPDATE OF trigger_id, organisation_id ON public.trigger_events FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('triggers', 'trigger_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_triggers_account_id_tenant') THEN CREATE TRIGGER trg_triggers_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.triggers FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_triggers_pipeline_id_tenant') THEN CREATE TRIGGER trg_triggers_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.triggers FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_variant_groups_pipeline_id_tenant') THEN CREATE TRIGGER trg_variant_groups_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.variant_groups FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_webhook_dedup_hashes_trigger_id_tenant') THEN CREATE TRIGGER trg_webhook_dedup_hashes_trigger_id_tenant BEFORE INSERT OR UPDATE OF trigger_id, organisation_id ON public.webhook_dedup_hashes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('triggers', 'trigger_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_webhook_payloads_trigger_event_id_tenant') THEN CREATE TRIGGER trg_webhook_payloads_trigger_event_id_tenant BEFORE INSERT OR UPDATE OF trigger_event_id, organisation_id ON public.webhook_payloads FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('trigger_events', 'trigger_event_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_workspace_leases_environment_profile_id_tenant') THEN CREATE TRIGGER trg_workspace_leases_environment_profile_id_tenant BEFORE INSERT OR UPDATE OF environment_profile_id, organisation_id ON public.workspace_leases FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('environment_profiles', 'environment_profile_id'); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_workspace_leases_run_id_tenant') THEN CREATE TRIGGER trg_workspace_leases_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.workspace_leases FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id'); END IF; END $$;"
    )
    op.execute("ALTER TABLE public.agents ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.chat_sessions ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.connector_instances ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.cost_components ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.environment_profiles ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.error_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.error_forwarder_configs ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.error_groups ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.error_notification_rules ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.eval_definitions ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.eval_results ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.feedback_records ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.hitl_claims ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.journeys ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.lifecycle_map_stages ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.lifecycle_maps ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.model_backends ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.modulo_journey_facts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.node_observations ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.nodes ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.notification_delivery_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.notification_endpoints ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.notifications ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.org_daily_run_counts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.pipeline_edges ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.pipeline_folders ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.pipeline_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.pipelines ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.run_daily_facts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.run_number_counters ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.runs ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.scheduled_reports ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.snapshot_schema_pins ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.spend_anomalies ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.trigger_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.triggers ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.variant_groups ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.web_vital_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.webhook_dedup_hashes ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.webhook_payloads ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE public.workspace_leases ENABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.agents;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.agents USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.chat_messages;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.chat_messages USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.chat_sessions;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.chat_sessions USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.connector_instances;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.connector_instances USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.cost_components;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.cost_components USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.environment_profiles;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.environment_profiles USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.error_events;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.error_events USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.error_forwarder_configs;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.error_forwarder_configs USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.error_groups;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.error_groups USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.error_notification_rules;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.error_notification_rules USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.eval_definitions;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.eval_definitions USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.eval_results;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.eval_results USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.feedback_records;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.feedback_records USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.hitl_claims;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.hitl_claims USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.journeys;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.journeys USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.lifecycle_map_stages;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.lifecycle_map_stages USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.lifecycle_maps;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.lifecycle_maps USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.model_backends;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.model_backends USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.modulo_journey_facts;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.modulo_journey_facts USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.node_observations;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.node_observations USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.nodes;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.nodes USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.notification_delivery_log;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.notification_delivery_log USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.notification_endpoints;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.notification_endpoints USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.notifications;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.notifications USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.org_daily_run_counts;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.org_daily_run_counts USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.pipeline_edges;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.pipeline_edges USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.pipeline_folders;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.pipeline_folders USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.pipeline_snapshots;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.pipeline_snapshots USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.pipelines;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.pipelines USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.run_daily_facts;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.run_daily_facts USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.run_number_counters;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.run_number_counters USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.runs;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.runs USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.scheduled_reports;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.scheduled_reports USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.snapshot_schema_pins;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.snapshot_schema_pins USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.spend_anomalies;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.spend_anomalies USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.trigger_events;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.trigger_events USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.triggers;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.triggers USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.variant_groups;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.variant_groups USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.web_vital_events;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.web_vital_events USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.webhook_dedup_hashes;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.webhook_dedup_hashes USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.webhook_payloads;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.webhook_payloads USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.workspace_leases;")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.workspace_leases USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"
    )
    op.execute("DROP POLICY IF EXISTS rls_team_isolation ON public.connector_instances;")
    op.execute(
        "CREATE POLICY rls_team_isolation ON public.connector_instances USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id FROM public.team_memberships WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text))));"
    )
    op.execute("DROP POLICY IF EXISTS rls_team_isolation ON public.environment_profiles;")
    op.execute(
        "CREATE POLICY rls_team_isolation ON public.environment_profiles USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id FROM public.team_memberships WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text))));"
    )
    op.execute("DROP POLICY IF EXISTS rls_team_isolation ON public.model_backends;")
    op.execute(
        "CREATE POLICY rls_team_isolation ON public.model_backends USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id FROM public.team_memberships WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text))));"
    )
    op.execute("DROP POLICY IF EXISTS rls_team_isolation ON public.pipelines;")
    op.execute(
        "CREATE POLICY rls_team_isolation ON public.pipelines USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id FROM public.team_memberships WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text))));"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_env_profiles_provider_type' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((provider_type)::text=ANY((ARRAY[''local_docker''::charactervarying,''e2b''::charactervarying,''local''::charactervarying])::text[])))') THEN ALTER TABLE public.environment_profiles DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_error_events_source' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((source)::text=ANY((ARRAY[''backend''::charactervarying,''frontend''::charactervarying,''celery''::charactervarying,''saq''::charactervarying])::text[])))') THEN ALTER TABLE public.error_events DROP CONSTRAINT IF EXISTS ck_error_events_source; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_eval_definitions_type' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((eval_type)::text=ANY((ARRAY[''llm_judge''::charactervarying,''regex''::charactervarying,''json_schema''::charactervarying,''custom_function''::charactervarying,''guardrail''::charactervarying])::text[])))') THEN ALTER TABLE public.eval_definitions DROP CONSTRAINT IF EXISTS ck_eval_definitions_type; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,''awaiting_human''::charactervarying,''claimed''::charactervarying,''complete''::charactervarying,''failed''::charactervarying,''cancelled''::charactervarying,''eval_failed''::charactervarying,''stalled''::charactervarying,''budget_exceeded''::charactervarying,''router_no_match''::charactervarying])::text[])))') THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_trigger_type' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((trigger_type)::text=ANY((ARRAY[''manual''::charactervarying,''webhook''::charactervarying,''cron''::charactervarying,''polling''::charactervarying,''agent_signal''::charactervarying,''ongoing''::charactervarying,''correction''::charactervarying,''slack_app_mention''::charactervarying])::text[])))') THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_trigger_type; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_trigger_events_validation_result' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((validation_result)::text=ANY((ARRAY[''accepted''::charactervarying,''passed''::charactervarying,''hmac_failed''::charactervarying,''schema_validation_failed''::charactervarying,''deduplicated''::charactervarying,''concurrency_limit_reached''::charactervarying,''flood_rejected''::charactervarying,''timestamp_expired''::charactervarying,''validation_failed''::charactervarying,''rate_limited''::charactervarying,''no_match''::charactervarying,''condition_met''::charactervarying,''poll_error''::charactervarying,''signal_fired''::charactervarying,''event_type_not_accepted''::charactervarying,''spend_limit_reached''::charactervarying,''no_pipeline''::charactervarying,''test''::charactervarying,''paused''::charactervarying,''auto_deactivated''::charactervarying,''guardrail_blocked''::charactervarying])::text[])))') THEN ALTER TABLE public.trigger_events DROP CONSTRAINT IF EXISTS ck_trigger_events_validation_result; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_triggers_type' AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> 'CHECK(((trigger_type)::text=ANY((ARRAY[''manual''::charactervarying,''webhook''::charactervarying,''cron''::charactervarying,''polling''::charactervarying,''agent_signal''::charactervarying,''ongoing''::charactervarying,''slack_app_mention''::charactervarying])::text[])))') THEN ALTER TABLE public.triggers DROP CONSTRAINT IF EXISTS ck_triggers_type; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='agents_pkey') THEN ALTER TABLE public.agents ADD CONSTRAINT agents_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chat_messages_pkey') THEN ALTER TABLE public.chat_messages ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chat_sessions_pkey') THEN ALTER TABLE public.chat_sessions ADD CONSTRAINT chat_sessions_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='connector_instances_pkey') THEN ALTER TABLE public.connector_instances ADD CONSTRAINT connector_instances_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cost_components_pkey') THEN ALTER TABLE public.cost_components ADD CONSTRAINT cost_components_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='dismissals_pkey') THEN ALTER TABLE public.dismissals ADD CONSTRAINT dismissals_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='environment_profiles_pkey') THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT environment_profiles_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_events_pkey') THEN ALTER TABLE public.error_events ADD CONSTRAINT error_events_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_forwarder_configs_pkey') THEN ALTER TABLE public.error_forwarder_configs ADD CONSTRAINT error_forwarder_configs_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_groups_pkey') THEN ALTER TABLE public.error_groups ADD CONSTRAINT error_groups_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_notification_rules_pkey') THEN ALTER TABLE public.error_notification_rules ADD CONSTRAINT error_notification_rules_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='eval_definitions_pkey') THEN ALTER TABLE public.eval_definitions ADD CONSTRAINT eval_definitions_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='eval_results_pkey') THEN ALTER TABLE public.eval_results ADD CONSTRAINT eval_results_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='feedback_records_pkey') THEN ALTER TABLE public.feedback_records ADD CONSTRAINT feedback_records_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hitl_claims_pkey') THEN ALTER TABLE public.hitl_claims ADD CONSTRAINT hitl_claims_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='journeys_pkey') THEN ALTER TABLE public.journeys ADD CONSTRAINT journeys_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_map_stages_pkey') THEN ALTER TABLE public.lifecycle_map_stages ADD CONSTRAINT lifecycle_map_stages_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_maps_pkey') THEN ALTER TABLE public.lifecycle_maps ADD CONSTRAINT lifecycle_maps_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='model_backends_pkey') THEN ALTER TABLE public.model_backends ADD CONSTRAINT model_backends_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='modulo_journey_facts_pkey') THEN ALTER TABLE public.modulo_journey_facts ADD CONSTRAINT modulo_journey_facts_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='node_observations_pkey') THEN ALTER TABLE public.node_observations ADD CONSTRAINT node_observations_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='nodes_pkey') THEN ALTER TABLE public.nodes ADD CONSTRAINT nodes_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notification_delivery_log_pkey') THEN ALTER TABLE public.notification_delivery_log ADD CONSTRAINT notification_delivery_log_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notification_endpoints_pkey') THEN ALTER TABLE public.notification_endpoints ADD CONSTRAINT notification_endpoints_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notifications_pkey') THEN ALTER TABLE public.notifications ADD CONSTRAINT notifications_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_daily_run_counts_pkey') THEN ALTER TABLE public.org_daily_run_counts ADD CONSTRAINT org_daily_run_counts_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_edges_pkey') THEN ALTER TABLE public.pipeline_edges ADD CONSTRAINT pipeline_edges_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_folders_pkey') THEN ALTER TABLE public.pipeline_folders ADD CONSTRAINT pipeline_folders_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_snapshots_pkey') THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT pipeline_snapshots_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipelines_pkey') THEN ALTER TABLE public.pipelines ADD CONSTRAINT pipelines_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='run_daily_facts_pkey') THEN ALTER TABLE public.run_daily_facts ADD CONSTRAINT run_daily_facts_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pk_run_evidence_run_node') THEN ALTER TABLE public.run_evidence ADD CONSTRAINT pk_run_evidence_run_node PRIMARY KEY (run_id, node_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='run_number_counters_pkey') THEN ALTER TABLE public.run_number_counters ADD CONSTRAINT run_number_counters_pkey PRIMARY KEY (organisation_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_pkey') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='scheduled_reports_pkey') THEN ALTER TABLE public.scheduled_reports ADD CONSTRAINT scheduled_reports_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='snapshot_schema_pins_pkey') THEN ALTER TABLE public.snapshot_schema_pins ADD CONSTRAINT snapshot_schema_pins_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='spend_anomalies_pkey') THEN ALTER TABLE public.spend_anomalies ADD CONSTRAINT spend_anomalies_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='trigger_events_pkey') THEN ALTER TABLE public.trigger_events ADD CONSTRAINT trigger_events_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='triggers_pkey') THEN ALTER TABLE public.triggers ADD CONSTRAINT triggers_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='variant_groups_pkey') THEN ALTER TABLE public.variant_groups ADD CONSTRAINT variant_groups_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='web_vital_events_pkey') THEN ALTER TABLE public.web_vital_events ADD CONSTRAINT web_vital_events_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='webhook_dedup_hashes_pkey') THEN ALTER TABLE public.webhook_dedup_hashes ADD CONSTRAINT webhook_dedup_hashes_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='webhook_payloads_pkey') THEN ALTER TABLE public.webhook_payloads ADD CONSTRAINT webhook_payloads_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='workspace_leases_pkey') THEN ALTER TABLE public.workspace_leases ADD CONSTRAINT workspace_leases_pkey PRIMARY KEY (id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_chat_sessions_user_session_number') THEN ALTER TABLE public.chat_sessions ADD CONSTRAINT uq_chat_sessions_user_session_number UNIQUE (user_id, session_number); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_dismissal_user_notification') THEN ALTER TABLE public.dismissals ADD CONSTRAINT uq_dismissal_user_notification UNIQUE (notification_id, dismissed_by_user_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_org_forwarder_type') THEN ALTER TABLE public.error_forwarder_configs ADD CONSTRAINT uq_org_forwarder_type UNIQUE (organisation_id, forwarder_type); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_error_groups_org_fingerprint') THEN ALTER TABLE public.error_groups ADD CONSTRAINT uq_error_groups_org_fingerprint UNIQUE (organisation_id, fingerprint); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_hitl_claims_run_gate') THEN ALTER TABLE public.hitl_claims ADD CONSTRAINT uq_hitl_claims_run_gate UNIQUE (run_id, gate_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_journeys_org_kind_ref') THEN ALTER TABLE public.journeys ADD CONSTRAINT uq_journeys_org_kind_ref UNIQUE (organisation_id, kind, ref); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_lifecycle_map_stages_map_version_stage') THEN ALTER TABLE public.lifecycle_map_stages ADD CONSTRAINT uq_lifecycle_map_stages_map_version_stage UNIQUE (map_id, version, stage_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_modulo_journey_facts_run_writer') THEN ALTER TABLE public.modulo_journey_facts ADD CONSTRAINT uq_modulo_journey_facts_run_writer UNIQUE (run_id, writer); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_node_observations_run_node') THEN ALTER TABLE public.node_observations ADD CONSTRAINT uq_node_observations_run_node UNIQUE (run_id, node_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_pipeline_edges_path') THEN ALTER TABLE public.pipeline_edges ADD CONSTRAINT uq_pipeline_edges_path UNIQUE (pipeline_id, source_node_id, target_node_id, edge_type); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_pipeline_snapshot_version') THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT uq_pipeline_snapshot_version UNIQUE (pipeline_id, snapshot_version); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_langgraph_thread_id_key') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_langgraph_thread_id_key UNIQUE (langgraph_thread_id); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_runs_org_run_number') THEN ALTER TABLE public.runs ADD CONSTRAINT uq_runs_org_run_number UNIQUE (organisation_id, run_number); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_webhook_dedup_trigger_hash') THEN ALTER TABLE public.webhook_dedup_hashes ADD CONSTRAINT uq_webhook_dedup_trigger_hash UNIQUE (trigger_id, payload_hash); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_chat_messages_role') THEN ALTER TABLE public.chat_messages ADD CONSTRAINT ck_chat_messages_role CHECK (((role)::text = ANY ((ARRAY['user'::character varying, 'assistant'::character varying, 'tool_use'::character varying, 'tool_result'::character varying, 'summary'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_connector_instances_team_owner') THEN ALTER TABLE public.connector_instances ADD CONSTRAINT ck_connector_instances_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_connector_instances_tier') THEN ALTER TABLE public.connector_instances ADD CONSTRAINT ck_connector_instances_tier CHECK (((tier)::text = ANY ((ARRAY['native'::character varying, 'preview'::character varying, 'in_dev'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_connector_instances_visibility') THEN ALTER TABLE public.connector_instances ADD CONSTRAINT ck_connector_instances_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_cost_components_kind') THEN ALTER TABLE public.cost_components ADD CONSTRAINT ck_cost_components_kind CHECK (((kind)::text = ANY ((ARRAY['calculated'::character varying, 'self_reported'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_dismissals_scope') THEN ALTER TABLE public.dismissals ADD CONSTRAINT ck_dismissals_scope CHECK (((dismiss_scope)::text = ANY ((ARRAY['self'::character varying, 'scope'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_env_profiles_network_policy') THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT ck_env_profiles_network_policy CHECK (((network_policy)::text = ANY ((ARRAY['none'::character varying, 'outbound'::character varying, 'selected'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_env_profiles_persistence_policy') THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT ck_env_profiles_persistence_policy CHECK (((persistence_policy)::text = ANY ((ARRAY['ephemeral'::character varying, 'retained'::character varying, 'cache'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_env_profiles_provider_type') THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT ck_env_profiles_provider_type CHECK (((provider_type)::text = ANY ((ARRAY['local_docker'::character varying, 'e2b'::character varying, 'local'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_env_profiles_visibility') THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT ck_env_profiles_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_error_events_level') THEN ALTER TABLE public.error_events ADD CONSTRAINT ck_error_events_level CHECK (((level)::text = ANY ((ARRAY['error'::character varying, 'warning'::character varying, 'critical'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_error_events_source') THEN ALTER TABLE public.error_events ADD CONSTRAINT ck_error_events_source CHECK (((source)::text = ANY ((ARRAY['backend'::character varying, 'frontend'::character varying, 'celery'::character varying, 'saq'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_error_events_status') THEN ALTER TABLE public.error_events ADD CONSTRAINT ck_error_events_status CHECK (((status)::text = ANY ((ARRAY['new'::character varying, 'acknowledged'::character varying, 'resolved'::character varying, 'archived'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_error_groups_level_peak') THEN ALTER TABLE public.error_groups ADD CONSTRAINT ck_error_groups_level_peak CHECK (((level_peak)::text = ANY ((ARRAY['error'::character varying, 'warning'::character varying, 'critical'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_error_groups_status') THEN ALTER TABLE public.error_groups ADD CONSTRAINT ck_error_groups_status CHECK (((status)::text = ANY ((ARRAY['new'::character varying, 'acknowledged'::character varying, 'resolved'::character varying, 'archived'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_enr_action_type') THEN ALTER TABLE public.error_notification_rules ADD CONSTRAINT ck_enr_action_type CHECK (((action_type)::text = ANY ((ARRAY['in_app'::character varying, 'email'::character varying, 'webhook'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_enr_condition_level') THEN ALTER TABLE public.error_notification_rules ADD CONSTRAINT ck_enr_condition_level CHECK (((condition_level)::text = ANY ((ARRAY['error'::character varying, 'warning'::character varying, 'critical'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_eval_definitions_failure_behaviour') THEN ALTER TABLE public.eval_definitions ADD CONSTRAINT ck_eval_definitions_failure_behaviour CHECK (((failure_behaviour)::text = ANY ((ARRAY['warn'::character varying, 'block'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_eval_definitions_type') THEN ALTER TABLE public.eval_definitions ADD CONSTRAINT ck_eval_definitions_type CHECK (((eval_type)::text = ANY ((ARRAY['llm_judge'::character varying, 'regex'::character varying, 'json_schema'::character varying, 'custom_function'::character varying, 'guardrail'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_feedback_records_handler_type') THEN ALTER TABLE public.feedback_records ADD CONSTRAINT ck_feedback_records_handler_type CHECK (((feedback_handler_type)::text = ANY ((ARRAY['human'::character varying, 'ai_correction'::character varying, 'ai_correction_with_human_review'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_feedback_records_status') THEN ALTER TABLE public.feedback_records ADD CONSTRAINT ck_feedback_records_status CHECK (((feedback_status)::text = ANY ((ARRAY['pending'::character varying, 'routing'::character varying, 'correcting'::character varying, 'resolved'::character varying, 'escalated'::character varying, 'dismissed'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_lifecycle_map_stages_type') THEN ALTER TABLE public.lifecycle_map_stages ADD CONSTRAINT ck_lifecycle_map_stages_type CHECK (((stage_type)::text = ANY ((ARRAY['modulo'::character varying, 'external'::character varying, 'manual'::character varying, 'placeholder'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_lifecycle_maps_team_owner') THEN ALTER TABLE public.lifecycle_maps ADD CONSTRAINT ck_lifecycle_maps_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_lifecycle_maps_version') THEN ALTER TABLE public.lifecycle_maps ADD CONSTRAINT ck_lifecycle_maps_version CHECK ((version > 0)); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_lifecycle_maps_visibility') THEN ALTER TABLE public.lifecycle_maps ADD CONSTRAINT ck_lifecycle_maps_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_model_backends_cost') THEN ALTER TABLE public.model_backends ADD CONSTRAINT ck_model_backends_cost CHECK (((cost_tracking)::text = ANY ((ARRAY['enabled'::character varying, 'disabled'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_model_backends_provider') THEN ALTER TABLE public.model_backends ADD CONSTRAINT ck_model_backends_provider CHECK (((provider)::text = ANY ((ARRAY['ai21'::character varying, 'anthropic'::character varying, 'azure_openai'::character varying, 'bedrock'::character varying, 'cohere'::character varying, 'custom'::character varying, 'deepseek'::character varying, 'fireworks'::character varying, 'gemini'::character varying, 'grok'::character varying, 'groq'::character varying, 'jan'::character varying, 'llamacpp'::character varying, 'lm_studio'::character varying, 'localai'::character varying, 'mistral'::character varying, 'ollama'::character varying, 'openai'::character varying, 'opencode'::character varying, 'openrouter'::character varying, 'perplexity'::character varying, 'qwen'::character varying, 'replicate'::character varying, 'tgi'::character varying, 'togetherai'::character varying, 'vertexai'::character varying, 'vllm'::character varying, 'watsonx'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_model_backends_team_owner') THEN ALTER TABLE public.model_backends ADD CONSTRAINT ck_model_backends_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_model_backends_tier') THEN ALTER TABLE public.model_backends ADD CONSTRAINT ck_model_backends_tier CHECK (((tier)::text = ANY ((ARRAY['native'::character varying, 'preview'::character varying, 'in_dev'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_model_backends_visibility') THEN ALTER TABLE public.model_backends ADD CONSTRAINT ck_model_backends_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_nodes_retry_count') THEN ALTER TABLE public.nodes ADD CONSTRAINT ck_nodes_retry_count CHECK (((retry_count IS NULL) OR (retry_count >= 0))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_nodes_retry_delay_seconds') THEN ALTER TABLE public.nodes ADD CONSTRAINT ck_nodes_retry_delay_seconds CHECK (((retry_delay_seconds IS NULL) OR (retry_delay_seconds >= 0))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_nodes_timeout_seconds') THEN ALTER TABLE public.nodes ADD CONSTRAINT ck_nodes_timeout_seconds CHECK (((timeout_seconds IS NULL) OR (timeout_seconds > 0))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_notifications_dismiss_strategy') THEN ALTER TABLE public.notifications ADD CONSTRAINT ck_notifications_dismiss_strategy CHECK (((dismiss_strategy)::text = ANY ((ARRAY['user_only'::character varying, 'org_admin'::character varying, 'any_scope'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_notifications_level') THEN ALTER TABLE public.notifications ADD CONSTRAINT ck_notifications_level CHECK (((level)::text = ANY ((ARRAY['debug'::character varying, 'info'::character varying, 'warning'::character varying, 'error'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_notifications_scope') THEN ALTER TABLE public.notifications ADD CONSTRAINT ck_notifications_scope CHECK (((scope)::text = ANY ((ARRAY['user'::character varying, 'org'::character varying, 'admin'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipeline_edges_type') THEN ALTER TABLE public.pipeline_edges ADD CONSTRAINT ck_pipeline_edges_type CHECK (((edge_type)::text = ANY ((ARRAY['normal'::character varying, 'reject'::character varying, 'conditional'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_autonomy_level') THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_autonomy_level CHECK (((default_autonomy_level)::text = ANY ((ARRAY['manual_approval'::character varying, 'notify_on_complete'::character varying, 'fully_autonomous'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_lock_wait_timeout') THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_lock_wait_timeout CHECK (((lock_wait_timeout_seconds >= 30) AND (lock_wait_timeout_seconds <= 3600))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_max_concurrent_runs') THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_max_concurrent_runs CHECK ((max_concurrent_runs > 0)); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_node_timeout') THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_node_timeout CHECK ((node_timeout_seconds > 0)); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_team_owner') THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_pipelines_visibility') THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, 'claimed'::character varying, 'complete'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'eval_failed'::character varying, 'stalled'::character varying, 'budget_exceeded'::character varying, 'router_no_match'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_trigger_type') THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_trigger_type CHECK (((trigger_type)::text = ANY ((ARRAY['manual'::character varying, 'webhook'::character varying, 'cron'::character varying, 'polling'::character varying, 'agent_signal'::character varying, 'ongoing'::character varying, 'correction'::character varying, 'slack_app_mention'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_snapshot_schema_pins_direction') THEN ALTER TABLE public.snapshot_schema_pins ADD CONSTRAINT ck_snapshot_schema_pins_direction CHECK (((direction)::text = ANY ((ARRAY['input'::character varying, 'output'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_trigger_events_validation_result') THEN ALTER TABLE public.trigger_events ADD CONSTRAINT ck_trigger_events_validation_result CHECK (((validation_result)::text = ANY ((ARRAY['accepted'::character varying, 'passed'::character varying, 'hmac_failed'::character varying, 'schema_validation_failed'::character varying, 'deduplicated'::character varying, 'concurrency_limit_reached'::character varying, 'flood_rejected'::character varying, 'timestamp_expired'::character varying, 'validation_failed'::character varying, 'rate_limited'::character varying, 'no_match'::character varying, 'condition_met'::character varying, 'poll_error'::character varying, 'signal_fired'::character varying, 'event_type_not_accepted'::character varying, 'spend_limit_reached'::character varying, 'no_pipeline'::character varying, 'test'::character varying, 'paused'::character varying, 'auto_deactivated'::character varying, 'guardrail_blocked'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_triggers_max_concurrent_runs') THEN ALTER TABLE public.triggers ADD CONSTRAINT ck_triggers_max_concurrent_runs CHECK ((max_concurrent_runs > 0)); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_triggers_ongoing_spend_limit') THEN ALTER TABLE public.triggers ADD CONSTRAINT ck_triggers_ongoing_spend_limit CHECK ((((trigger_type)::text <> 'ongoing'::text) OR ((daily_spend_limit IS NOT NULL) AND (daily_spend_limit > (0)::numeric)))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_triggers_ongoing_target_range') THEN ALTER TABLE public.triggers ADD CONSTRAINT ck_triggers_ongoing_target_range CHECK ((((trigger_type)::text <> 'ongoing'::text) OR ((max_concurrent_runs >= 1) AND (max_concurrent_runs <= 20)))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_triggers_type') THEN ALTER TABLE public.triggers ADD CONSTRAINT ck_triggers_type CHECK (((trigger_type)::text = ANY ((ARRAY['manual'::character varying, 'webhook'::character varying, 'cron'::character varying, 'polling'::character varying, 'agent_signal'::character varying, 'ongoing'::character varying, 'slack_app_mention'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_variant_groups_selection_strategy') THEN ALTER TABLE public.variant_groups ADD CONSTRAINT ck_variant_groups_selection_strategy CHECK (((selection_strategy)::text = ANY ((ARRAY['weighted'::character varying, 'single'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_workspace_leases_status') THEN ALTER TABLE public.workspace_leases ADD CONSTRAINT ck_workspace_leases_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'completed'::character varying, 'failed'::character varying, 'expired'::character varying])::text[]))); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='agents_account_id_fkey') THEN ALTER TABLE public.agents ADD CONSTRAINT agents_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='agents_library_id_fkey') THEN ALTER TABLE public.agents ADD CONSTRAINT agents_library_id_fkey FOREIGN KEY (library_id) REFERENCES library_primitives(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='agents_model_backend_id_fkey') THEN ALTER TABLE public.agents ADD CONSTRAINT agents_model_backend_id_fkey FOREIGN KEY (model_backend_id) REFERENCES model_backends(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='agents_organisation_id_fkey') THEN ALTER TABLE public.agents ADD CONSTRAINT agents_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_input_schema_version') THEN ALTER TABLE public.agents ADD CONSTRAINT fk_agents_input_schema_version FOREIGN KEY (input_schema_id, input_schema_version, organisation_id) REFERENCES schema_versions(schema_id, version, organisation_id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_output_schema_version') THEN ALTER TABLE public.agents ADD CONSTRAINT fk_agents_output_schema_version FOREIGN KEY (output_schema_id, output_schema_version, organisation_id) REFERENCES schema_versions(schema_id, version, organisation_id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_agents_parameter_schema_id') THEN ALTER TABLE public.agents ADD CONSTRAINT fk_agents_parameter_schema_id FOREIGN KEY (parameter_schema_id) REFERENCES parameter_schemas(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chat_messages_organisation_id_fkey') THEN ALTER TABLE public.chat_messages ADD CONSTRAINT chat_messages_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chat_messages_parent_id_fkey') THEN ALTER TABLE public.chat_messages ADD CONSTRAINT chat_messages_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES chat_messages(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chat_messages_session_id_fkey') THEN ALTER TABLE public.chat_messages ADD CONSTRAINT chat_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chat_sessions_organisation_id_fkey') THEN ALTER TABLE public.chat_sessions ADD CONSTRAINT chat_sessions_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chat_sessions_user_id_fkey') THEN ALTER TABLE public.chat_sessions ADD CONSTRAINT chat_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='connector_instances_account_id_fkey') THEN ALTER TABLE public.connector_instances ADD CONSTRAINT connector_instances_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='connector_instances_organisation_id_fkey') THEN ALTER TABLE public.connector_instances ADD CONSTRAINT connector_instances_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='connector_instances_owner_team_id_fkey') THEN ALTER TABLE public.connector_instances ADD CONSTRAINT connector_instances_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cost_components_organisation_id_fkey') THEN ALTER TABLE public.cost_components ADD CONSTRAINT cost_components_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='dismissals_dismissed_by_user_id_fkey') THEN ALTER TABLE public.dismissals ADD CONSTRAINT dismissals_dismissed_by_user_id_fkey FOREIGN KEY (dismissed_by_user_id) REFERENCES accounts(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='dismissals_notification_id_fkey') THEN ALTER TABLE public.dismissals ADD CONSTRAINT dismissals_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES notifications(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='environment_profiles_account_id_fkey') THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT environment_profiles_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='environment_profiles_organisation_id_fkey') THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT environment_profiles_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='environment_profiles_owner_team_id_fkey') THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT environment_profiles_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_events_organisation_id_fkey') THEN ALTER TABLE public.error_events ADD CONSTRAINT error_events_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_forwarder_configs_organisation_id_fkey') THEN ALTER TABLE public.error_forwarder_configs ADD CONSTRAINT error_forwarder_configs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_groups_assigned_to_fkey') THEN ALTER TABLE public.error_groups ADD CONSTRAINT error_groups_assigned_to_fkey FOREIGN KEY (assigned_to) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_groups_organisation_id_fkey') THEN ALTER TABLE public.error_groups ADD CONSTRAINT error_groups_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_groups_sample_event_id_fkey') THEN ALTER TABLE public.error_groups ADD CONSTRAINT error_groups_sample_event_id_fkey FOREIGN KEY (sample_event_id) REFERENCES error_events(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='error_notification_rules_organisation_id_fkey') THEN ALTER TABLE public.error_notification_rules ADD CONSTRAINT error_notification_rules_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='eval_definitions_account_id_fkey') THEN ALTER TABLE public.eval_definitions ADD CONSTRAINT eval_definitions_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='eval_definitions_organisation_id_fkey') THEN ALTER TABLE public.eval_definitions ADD CONSTRAINT eval_definitions_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='eval_definitions_pipeline_id_fkey') THEN ALTER TABLE public.eval_definitions ADD CONSTRAINT eval_definitions_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='eval_results_eval_id_fkey') THEN ALTER TABLE public.eval_results ADD CONSTRAINT eval_results_eval_id_fkey FOREIGN KEY (eval_id) REFERENCES eval_definitions(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='eval_results_organisation_id_fkey') THEN ALTER TABLE public.eval_results ADD CONSTRAINT eval_results_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='eval_results_run_id_fkey') THEN ALTER TABLE public.eval_results ADD CONSTRAINT eval_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='feedback_records_account_id_fkey') THEN ALTER TABLE public.feedback_records ADD CONSTRAINT feedback_records_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='feedback_records_correction_run_id_fkey') THEN ALTER TABLE public.feedback_records ADD CONSTRAINT feedback_records_correction_run_id_fkey FOREIGN KEY (correction_run_id) REFERENCES runs(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='feedback_records_organisation_id_fkey') THEN ALTER TABLE public.feedback_records ADD CONSTRAINT feedback_records_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='feedback_records_producing_agent_id_fkey') THEN ALTER TABLE public.feedback_records ADD CONSTRAINT feedback_records_producing_agent_id_fkey FOREIGN KEY (producing_agent_id) REFERENCES agents(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='feedback_records_run_id_fkey') THEN ALTER TABLE public.feedback_records ADD CONSTRAINT feedback_records_run_id_fkey FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hitl_claims_account_id_fkey') THEN ALTER TABLE public.hitl_claims ADD CONSTRAINT hitl_claims_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hitl_claims_organisation_id_fkey') THEN ALTER TABLE public.hitl_claims ADD CONSTRAINT hitl_claims_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hitl_claims_pipeline_id_fkey') THEN ALTER TABLE public.hitl_claims ADD CONSTRAINT hitl_claims_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hitl_claims_required_team_id_fkey') THEN ALTER TABLE public.hitl_claims ADD CONSTRAINT hitl_claims_required_team_id_fkey FOREIGN KEY (required_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hitl_claims_run_id_fkey') THEN ALTER TABLE public.hitl_claims ADD CONSTRAINT hitl_claims_run_id_fkey FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='journeys_organisation_id_fkey') THEN ALTER TABLE public.journeys ADD CONSTRAINT journeys_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='journeys_owner_team_id_fkey') THEN ALTER TABLE public.journeys ADD CONSTRAINT journeys_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_map_stages_account_id_fkey') THEN ALTER TABLE public.lifecycle_map_stages ADD CONSTRAINT lifecycle_map_stages_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_map_stages_map_id_fkey') THEN ALTER TABLE public.lifecycle_map_stages ADD CONSTRAINT lifecycle_map_stages_map_id_fkey FOREIGN KEY (map_id) REFERENCES lifecycle_maps(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_map_stages_organisation_id_fkey') THEN ALTER TABLE public.lifecycle_map_stages ADD CONSTRAINT lifecycle_map_stages_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_map_stages_pipeline_id_fkey') THEN ALTER TABLE public.lifecycle_map_stages ADD CONSTRAINT lifecycle_map_stages_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_maps_account_id_fkey') THEN ALTER TABLE public.lifecycle_maps ADD CONSTRAINT lifecycle_maps_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_maps_organisation_id_fkey') THEN ALTER TABLE public.lifecycle_maps ADD CONSTRAINT lifecycle_maps_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='lifecycle_maps_owner_team_id_fkey') THEN ALTER TABLE public.lifecycle_maps ADD CONSTRAINT lifecycle_maps_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='model_backends_account_id_fkey') THEN ALTER TABLE public.model_backends ADD CONSTRAINT model_backends_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='model_backends_organisation_id_fkey') THEN ALTER TABLE public.model_backends ADD CONSTRAINT model_backends_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='model_backends_owner_team_id_fkey') THEN ALTER TABLE public.model_backends ADD CONSTRAINT model_backends_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='modulo_journey_facts_organisation_id_fkey') THEN ALTER TABLE public.modulo_journey_facts ADD CONSTRAINT modulo_journey_facts_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='node_observations_account_id_fkey') THEN ALTER TABLE public.node_observations ADD CONSTRAINT node_observations_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='node_observations_organisation_id_fkey') THEN ALTER TABLE public.node_observations ADD CONSTRAINT node_observations_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='node_observations_run_id_fkey') THEN ALTER TABLE public.node_observations ADD CONSTRAINT node_observations_run_id_fkey FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='nodes_account_id_fkey') THEN ALTER TABLE public.nodes ADD CONSTRAINT nodes_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='nodes_organisation_id_fkey') THEN ALTER TABLE public.nodes ADD CONSTRAINT nodes_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='nodes_parent_node_id_fkey') THEN ALTER TABLE public.nodes ADD CONSTRAINT nodes_parent_node_id_fkey FOREIGN KEY (parent_node_id) REFERENCES nodes(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='nodes_pipeline_id_fkey') THEN ALTER TABLE public.nodes ADD CONSTRAINT nodes_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notification_delivery_log_endpoint_id_fkey') THEN ALTER TABLE public.notification_delivery_log ADD CONSTRAINT notification_delivery_log_endpoint_id_fkey FOREIGN KEY (endpoint_id) REFERENCES notification_endpoints(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notification_delivery_log_organisation_id_fkey') THEN ALTER TABLE public.notification_delivery_log ADD CONSTRAINT notification_delivery_log_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notification_delivery_log_run_id_fkey') THEN ALTER TABLE public.notification_delivery_log ADD CONSTRAINT notification_delivery_log_run_id_fkey FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notification_endpoints_account_id_fkey') THEN ALTER TABLE public.notification_endpoints ADD CONSTRAINT notification_endpoints_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notification_endpoints_organisation_id_fkey') THEN ALTER TABLE public.notification_endpoints ADD CONSTRAINT notification_endpoints_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notification_endpoints_team_id_fkey') THEN ALTER TABLE public.notification_endpoints ADD CONSTRAINT notification_endpoints_team_id_fkey FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notifications_organisation_id_fkey') THEN ALTER TABLE public.notifications ADD CONSTRAINT notifications_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='notifications_target_user_id_fkey') THEN ALTER TABLE public.notifications ADD CONSTRAINT notifications_target_user_id_fkey FOREIGN KEY (target_user_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_daily_run_counts_organisation_id_fkey') THEN ALTER TABLE public.org_daily_run_counts ADD CONSTRAINT org_daily_run_counts_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='org_daily_run_counts_team_id_fkey') THEN ALTER TABLE public.org_daily_run_counts ADD CONSTRAINT org_daily_run_counts_team_id_fkey FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_edges_organisation_id_fkey') THEN ALTER TABLE public.pipeline_edges ADD CONSTRAINT pipeline_edges_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_edges_pipeline_id_fkey') THEN ALTER TABLE public.pipeline_edges ADD CONSTRAINT pipeline_edges_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_folders_account_id_fkey') THEN ALTER TABLE public.pipeline_folders ADD CONSTRAINT pipeline_folders_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_folders_organisation_id_fkey') THEN ALTER TABLE public.pipeline_folders ADD CONSTRAINT pipeline_folders_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_folders_parent_id_fkey') THEN ALTER TABLE public.pipeline_folders ADD CONSTRAINT pipeline_folders_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES pipeline_folders(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_snapshots_account_id_fkey') THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT pipeline_snapshots_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_snapshots_environment_profile_id_fkey') THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT pipeline_snapshots_environment_profile_id_fkey FOREIGN KEY (environment_profile_id) REFERENCES environment_profiles(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_snapshots_organisation_id_fkey') THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT pipeline_snapshots_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipeline_snapshots_pipeline_id_fkey') THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT pipeline_snapshots_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_pipelines_folder_id') THEN ALTER TABLE public.pipelines ADD CONSTRAINT fk_pipelines_folder_id FOREIGN KEY (folder_id) REFERENCES pipeline_folders(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipelines_account_id_fkey') THEN ALTER TABLE public.pipelines ADD CONSTRAINT pipelines_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipelines_organisation_id_fkey') THEN ALTER TABLE public.pipelines ADD CONSTRAINT pipelines_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pipelines_owner_team_id_fkey') THEN ALTER TABLE public.pipelines ADD CONSTRAINT pipelines_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='run_daily_facts_folder_id_fkey') THEN ALTER TABLE public.run_daily_facts ADD CONSTRAINT run_daily_facts_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES pipeline_folders(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='run_daily_facts_organisation_id_fkey') THEN ALTER TABLE public.run_daily_facts ADD CONSTRAINT run_daily_facts_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='run_daily_facts_pipeline_id_fkey') THEN ALTER TABLE public.run_daily_facts ADD CONSTRAINT run_daily_facts_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='run_daily_facts_team_id_fkey') THEN ALTER TABLE public.run_daily_facts ADD CONSTRAINT run_daily_facts_team_id_fkey FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='run_evidence_run_id_fkey') THEN ALTER TABLE public.run_evidence ADD CONSTRAINT run_evidence_run_id_fkey FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='run_number_counters_organisation_id_fkey') THEN ALTER TABLE public.run_number_counters ADD CONSTRAINT run_number_counters_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_account_id_fkey') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_organisation_id_fkey') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_owner_team_id_fkey') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES teams(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_parent_run_id_fkey') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_parent_run_id_fkey FOREIGN KEY (parent_run_id) REFERENCES runs(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_pipeline_id_fkey') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_snapshot_id_fkey') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES pipeline_snapshots(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='runs_trigger_id_fkey') THEN ALTER TABLE public.runs ADD CONSTRAINT runs_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES triggers(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='scheduled_reports_created_by_fkey') THEN ALTER TABLE public.scheduled_reports ADD CONSTRAINT scheduled_reports_created_by_fkey FOREIGN KEY (created_by) REFERENCES accounts(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='scheduled_reports_organisation_id_fkey') THEN ALTER TABLE public.scheduled_reports ADD CONSTRAINT scheduled_reports_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='snapshot_schema_pins_organisation_id_fkey') THEN ALTER TABLE public.snapshot_schema_pins ADD CONSTRAINT snapshot_schema_pins_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='snapshot_schema_pins_schema_id_fkey') THEN ALTER TABLE public.snapshot_schema_pins ADD CONSTRAINT snapshot_schema_pins_schema_id_fkey FOREIGN KEY (schema_id) REFERENCES schemas(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='snapshot_schema_pins_schema_id_schema_version_organisation_fkey') THEN ALTER TABLE public.snapshot_schema_pins ADD CONSTRAINT snapshot_schema_pins_schema_id_schema_version_organisation_fkey FOREIGN KEY (schema_id, schema_version, organisation_id) REFERENCES schema_versions(schema_id, version, organisation_id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='snapshot_schema_pins_snapshot_id_fkey') THEN ALTER TABLE public.snapshot_schema_pins ADD CONSTRAINT snapshot_schema_pins_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES pipeline_snapshots(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='spend_anomalies_organisation_id_fkey') THEN ALTER TABLE public.spend_anomalies ADD CONSTRAINT spend_anomalies_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='spend_anomalies_pipeline_id_fkey') THEN ALTER TABLE public.spend_anomalies ADD CONSTRAINT spend_anomalies_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='trigger_events_organisation_id_fkey') THEN ALTER TABLE public.trigger_events ADD CONSTRAINT trigger_events_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='trigger_events_run_id_fkey') THEN ALTER TABLE public.trigger_events ADD CONSTRAINT trigger_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='trigger_events_trigger_id_fkey') THEN ALTER TABLE public.trigger_events ADD CONSTRAINT trigger_events_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES triggers(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='triggers_account_id_fkey') THEN ALTER TABLE public.triggers ADD CONSTRAINT triggers_account_id_fkey FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='triggers_organisation_id_fkey') THEN ALTER TABLE public.triggers ADD CONSTRAINT triggers_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='triggers_pipeline_id_fkey') THEN ALTER TABLE public.triggers ADD CONSTRAINT triggers_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='variant_groups_organisation_id_fkey') THEN ALTER TABLE public.variant_groups ADD CONSTRAINT variant_groups_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='variant_groups_pipeline_id_fkey') THEN ALTER TABLE public.variant_groups ADD CONSTRAINT variant_groups_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='web_vital_events_organisation_id_fkey') THEN ALTER TABLE public.web_vital_events ADD CONSTRAINT web_vital_events_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='webhook_dedup_hashes_organisation_id_fkey') THEN ALTER TABLE public.webhook_dedup_hashes ADD CONSTRAINT webhook_dedup_hashes_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='webhook_dedup_hashes_trigger_id_fkey') THEN ALTER TABLE public.webhook_dedup_hashes ADD CONSTRAINT webhook_dedup_hashes_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES triggers(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='webhook_payloads_organisation_id_fkey') THEN ALTER TABLE public.webhook_payloads ADD CONSTRAINT webhook_payloads_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='webhook_payloads_trigger_event_id_fkey') THEN ALTER TABLE public.webhook_payloads ADD CONSTRAINT webhook_payloads_trigger_event_id_fkey FOREIGN KEY (trigger_event_id) REFERENCES trigger_events(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='workspace_leases_environment_profile_id_fkey') THEN ALTER TABLE public.workspace_leases ADD CONSTRAINT workspace_leases_environment_profile_id_fkey FOREIGN KEY (environment_profile_id) REFERENCES environment_profiles(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='workspace_leases_organisation_id_fkey') THEN ALTER TABLE public.workspace_leases ADD CONSTRAINT workspace_leases_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='workspace_leases_run_id_fkey') THEN ALTER TABLE public.workspace_leases ADD CONSTRAINT workspace_leases_run_id_fkey FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE RESTRICT; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='modulo_migrate') THEN ALTER TABLE public.cost_components OWNER TO modulo_migrate; END IF; END $$;"
    )


def downgrade() -> None:
    """Downgrade is a no-op: schema reconciliation is not reversible in general."""

"""Widen env-profile provider_type CHECK with 'kubernetes' (FAR-1051).

Revision ID: 0281_env_profiles_kubernetes
Revises: 0280_runs_node_deadline_watchdog_fired_count
Create Date: 2026-10-04

What this revision changes
--------------------------
FAR-1051 adds the Kubernetes runtime provider (long-lived workspace pods, exec
subresource). The single contract delta is registering the provider TYPE: this
revision widens ``ck_env_profiles_provider_type`` with ``'kubernetes'`` so an
environment profile can select it. The four existing values (``local_docker`` /
``e2b`` / ``local`` / ``runner_docker``) stay valid; nothing is re-pointed on
upgrade. Model parity: ``PROVIDER_TYPES`` in
``modulo.db.models.environment_profile`` gains the same member in the same
change (single source of truth, FAR-595 — the CHECK, the API boundary pattern
and the vocabulary scanner test all derive from that constant).

Guarded operations
------------------
``DROP CONSTRAINT IF EXISTS`` + unconditional re-add makes the widen idempotent
(Alembic runs each revision once, but the guard keeps re-runs and
partially-migrated databases safe), mirroring 0178.

Down-path (tested): rows with ``provider_type = 'kubernetes'`` are re-pointed
to ``local_docker`` BEFORE the CHECK is narrowed (the narrow would otherwise
fail on them), then the CHECK is restored to the four-value vocabulary.

Unlike 0178's ``runner_docker`` -> ``local_docker`` re-point (an alias of the
SAME provider), ``kubernetes`` has no legacy alias — ``local_docker`` is the
historical column default and the 0178 downgrade's own restore target, chosen
here purely so existing rows survive the narrow. The re-point is NOT
semantics-preserving: a downgraded profile keeps its name/config but resolves
to the Docker provider (or raises ProviderNotConfiguredError when no Docker
endpoint is configured). Operators MUST review any profile that carried
``kubernetes`` before downgrading this revision.
"""

from __future__ import annotations

from alembic import op

revision: str = "0281_env_profiles_kubernetes"
down_revision: str | None = "0280_runs_node_deadline_watchdog_fired_count"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Widen the CHECK vocabulary with 'kubernetes'.
    op.execute("ALTER TABLE public.environment_profiles DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type;")
    op.execute(
        "ALTER TABLE public.environment_profiles ADD CONSTRAINT ck_env_profiles_provider_type "
        "CHECK (((provider_type)::text = ANY "
        "((ARRAY['e2b'::character varying, 'kubernetes'::character varying, "
        "'local'::character varying, 'local_docker'::character varying, "
        "'runner_docker'::character varying])::text[])))"
    )


def downgrade() -> None:
    # Re-point kubernetes rows to the historical column default BEFORE
    # narrowing the vocabulary (the CHECK would reject them otherwise).
    # NOT semantics-preserving — see the module docstring.
    op.execute(
        "UPDATE public.environment_profiles SET provider_type = 'local_docker' WHERE provider_type = 'kubernetes';"
    )
    op.execute("ALTER TABLE public.environment_profiles DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type;")
    op.execute(
        r"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_env_profiles_provider_type'
                  AND conrelid = 'public.environment_profiles'::regclass
            ) THEN
                ALTER TABLE public.environment_profiles ADD CONSTRAINT ck_env_profiles_provider_type
                CHECK (((provider_type)::text = ANY
                ((ARRAY['e2b'::character varying, 'local'::character varying,
                'local_docker'::character varying, 'runner_docker'::character varying])::text[])));
            END IF;
        END $$;
        """
    )

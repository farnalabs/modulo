"""Widen env-profile provider_type CHECK with 'runner_docker' and drop the DB default (FAR-587).

Revision ID: 0178_env_profiles_runner_docker
Revises: 0177_invitations
Create Date: 2026-09-03

What this revision changes
--------------------------
D2 of the Agent Execution Tiers plan (ADR 029, FAR-587) renames the Docker
runtime provider's identity to ``runner_docker`` (with ``docker`` and legacy
``local_docker`` kept as explicit aliases in the provider layer, so existing
profiles keep resolving identically). Two single-purpose guarded schema
changes land together because they both describe the ``provider_type``
vocabulary:

1. **CHECK widen** — ``ck_env_profiles_provider_type`` gains
   ``'runner_docker'`` (existing values ``local_docker`` / ``e2b`` / ``local``
   stay valid; nothing is re-pointed).
2. **Default drop** — the column's ``server_default 'local_docker'`` is
   dropped.

Writer-inventory note (the justification for dropping the default, which
deliberately diverges from migration 0141's keep-the-default convention):
after FAR-587 every writer sets ``provider_type`` explicitly — the API request
model makes it required, the CRUD signature has no default, and the only
in-app seed (``api.main._seed_environment_profiles``) passes
``provider_type="local_docker"`` explicitly. The clone/template/import paths
that rely on 0141's defaults do not apply to ``provider_type`` (it is never
cloned server-side). The EnvironmentProfileForm.vue hardcoded
``provider_type: 'local_docker'`` default is removed in this same PR, so
model/migration/form parity holds: nothing defaults the value implicitly any
more — the client must pick (required-select validation on the form).

Guarded operations
------------------
``DROP CONSTRAINT IF EXISTS`` + unconditional re-add makes the widen idempotent
(Alembic runs each revision once, but the guard keeps re-runs and
partially-migrated databases safe). The default drop only fires when the
existing default is the legacy ``'local_docker'`` value, so an operator-set
default is never silently removed.

Down-path (tested): rows with ``provider_type = 'runner_docker'`` are
re-pointed to ``local_docker`` (an explicit alias of the same provider in the
application layer, so the re-point is semantics-preserving), the CHECK is
narrowed back to the original vocabulary, and the ``'local_docker'`` server
default is restored (guarded: only when the column has no default).
"""

from __future__ import annotations

from alembic import op

revision: str = "0178_env_profiles_runner_docker"
down_revision: str | None = "0177_invitations"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. Widen the CHECK vocabulary with 'runner_docker'.
    op.execute("ALTER TABLE public.environment_profiles DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type;")
    op.execute(
        "ALTER TABLE public.environment_profiles ADD CONSTRAINT ck_env_profiles_provider_type "
        "CHECK (((provider_type)::text = ANY "
        "((ARRAY['local_docker'::character varying, 'e2b'::character varying, "
        "'local'::character varying, 'runner_docker'::character varying])::text[])))"
    )

    # 2. Drop the server default (guarded: only when it is the legacy value).
    op.execute(
        r"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_attrdef ad
                JOIN pg_class c ON c.oid = ad.adrelid
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ad.adnum
                WHERE c.relname = 'environment_profiles'
                  AND a.attname = 'provider_type'
                  AND pg_get_expr(ad.adbin, ad.adrelid) = '''local_docker''::character varying'
            ) THEN
                ALTER TABLE public.environment_profiles ALTER COLUMN provider_type DROP DEFAULT;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Re-point runner_docker rows to the legacy alias before narrowing the
    # vocabulary (semantics-preserving: the application layer aliases
    # local_docker onto the same provider).
    op.execute(
        "UPDATE public.environment_profiles SET provider_type = 'local_docker' WHERE provider_type = 'runner_docker';"
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
                ((ARRAY['local_docker'::character varying, 'e2b'::character varying,
                'local'::character varying])::text[])));
            END IF;
        END $$;
        """
    )
    op.execute(
        r"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_attrdef ad
                JOIN pg_class c ON c.oid = ad.adrelid
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ad.adnum
                WHERE c.relname = 'environment_profiles'
                  AND a.attname = 'provider_type'
            ) THEN
                ALTER TABLE public.environment_profiles ALTER COLUMN provider_type
                SET DEFAULT 'local_docker'::character varying;
            END IF;
        END $$;
        """
    )

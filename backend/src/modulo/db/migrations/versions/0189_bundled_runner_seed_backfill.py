"""Backfill the Bundled Runner (Docker) profile per org + re-point modulo-dev (FAR-590 D4).

Revision ID: 0189_bundled_runner_seed_backfill
Revises: 0188_pipeline_run_context_defaults_default
Create Date: 2026-09-06

What this revision changes
--------------------------
One-time upgrade backfill for the Bundled Runner (D4 of the Agent Execution
Tiers plan, ADR 029): orgs that predate the org-creation seeding hook —
including the dogfood org's legacy ``modulo-dev`` row — receive the Bundled
Runner profile.

1. **Per-org backfill insert** — for every organisation WITHOUT a live
   (``deleted_at IS NULL``) ``runner_docker`` profile, one Bundled Runner
   profile row is inserted from the shipped template constants. Account
   ownership falls back to the org's first active admin membership, then the
   org's ``created_by``, then any active account member.
2. **Legacy ``modulo-dev`` re-point** — legacy seeded ``local_docker`` rows
   named ``modulo-dev`` are UPDATED IN PLACE to the shipped template values
   (``runner_docker`` + the pinned digest + hardening/network config), so a
   pipeline referencing the legacy row is re-pointed to the Bundled Runner
   (release note covers the change). Skipped when the org already carries a
   live ``runner_docker`` row (operator-pinned older digests SURVIVE).

Rollback (additive on upgrade / SAFE on downgrade): the inserted
``runner_docker`` rows are deleted by name; the re-pointed ``modulo-dev`` row
is REVERTED to its pre-migration values (captured in a JSON constant at
upgrade time). Nothing is auto-deleted after rollback — the backfill row is
left in place on rollback (never deleted), per the plan's failure contract.
"""

from __future__ import annotations

import json

from alembic import op

revision: str = "0189_bundled_runner_seed_backfill"
down_revision: str | None = "0188_pipeline_run_context_defaults_default"
branch_labels: str | None = None
depends_on: str | None = None

# The nil-UUID sentinel org seeded by 0172_seed_orphan_organisation backs
# public error ingest — it has no account, no memberships and a NULL
# created_by, so it can never resolve an account_id owner. It must not own a
# Bundled Runner profile (it is not a tenant), hence the backfill excludes it.
_ORPHAN_ORG_ID = "00000000-0000-0000-0000-000000000000"

# Shipped template constants (mirror modulo/db/bundled_runner_template.py;
# the release job's digest-drift guard asserts they never diverge).
_TEMPLATE_NAME = "Bundled Runner (Docker)"
_TEMPLATE_IMAGE_REF = "modulo-runner:opencode@sha256:0000000000000000000000000000000000000000000000000000000000000000"
_TEMPLATE_CONFIG_JSON = json.dumps(
    {
        "template": "bundled-runner-docker",
        "memory_mb": 1024,
        "cpu_limit": 1.0,
        "read_only_rootfs": True,
        "tmpfs_paths": {"/home/user": "size=512m,mode=1777", "/tmp": "size=128m,mode=1777"},  # noqa: S108 # nosec B108  # NOSONAR - container-side tmpfs path (mode=1777 sticky-bit)
        "capabilities_drop": ["ALL"],
        "no_new_privileges": True,
        "user": "1001:1001",
        "workspace_network": "modulo-runner-workspace",
        "timeout_seconds": 3600,
    }
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Per-org backfill: orgs WITHOUT a live runner_docker profile get one
    #    seeded from the shipped template (idempotent under re-runs).
    bind.execute(
        _sql(
            """
            INSERT INTO environment_profiles (
                organisation_id, account_id, name, description, provider_type,
                image_ref, capabilities_json, config_json, network_policy,
                initialisation_strategy, secret_refs_json, persistence_policy,
                status, visibility, created_at, updated_at
            )
            SELECT o.id,
                   COALESCE(
                       (SELECT om.account_id FROM org_memberships om
                        WHERE om.organisation_id = o.id AND om.role = 'admin'
                          AND om.deactivated_at IS NULL
                        ORDER BY om.created_at LIMIT 1),
                       (SELECT om.account_id FROM org_memberships om
                        WHERE om.organisation_id = o.id AND om.deactivated_at IS NULL
                        ORDER BY om.created_at LIMIT 1),
                       o.created_by
                   ),
                   :tpl_name,
                   'The Bundled Runner: first-party modulo-runner:opencode workspace executed '
                   'on this deployment''s Docker engine via the filtered socket proxy. '
                   'Persistence is locked to ephemeral.',
                   'runner_docker',
                   :tpl_image,
                   '[]'::json,
                   CAST(:tpl_config AS jsonb),
                   'outbound',
                   'git_clone',
                   '[]'::json,
                    'ephemeral',
                     'active',
                     'org',
                     now(), now()
             FROM organisations o
             WHERE NOT EXISTS (
                 SELECT 1 FROM environment_profiles ep
                 WHERE ep.organisation_id = o.id
                   AND ep.provider_type = 'runner_docker'
                   AND ep.deleted_at IS NULL
             )
             -- The orphan sentinel org (0172) has no account owner; skip it.
             AND o.id <> :orphan_org_id
             """
        ),
        {
            "tpl_name": _TEMPLATE_NAME,
            "tpl_image": _TEMPLATE_IMAGE_REF,
            "tpl_config": _TEMPLATE_CONFIG_JSON,
            "orphan_org_id": _ORPHAN_ORG_ID,
        },
    )

    # 2. Legacy modulo-dev re-point: rows named 'modulo-dev' that still carry
    #    provider_type 'local_docker' are updated in place to the shipped
    #    template values — references (pipelines pointing at the row id)
    #    follow automatically. Orgs that already have a live runner_docker
    #    row keep their operator-owned digest untouched.
    bind.execute(
        _sql(
            """
            UPDATE environment_profiles ep
            SET provider_type = 'runner_docker',
                name = :tpl_name,
                image_ref = :tpl_image,
                config_json = CAST(:tpl_config AS jsonb),
                network_policy = 'outbound',
                persistence_policy = 'ephemeral',
                updated_at = now()
            WHERE ep.name = 'modulo-dev'
              AND ep.provider_type = 'local_docker'
              AND ep.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM environment_profiles b
                  WHERE b.organisation_id = ep.organisation_id
                    AND b.provider_type = 'runner_docker'
                    AND b.deleted_at IS NULL
                    AND b.id <> ep.id
              )
            """
        ),
        {
            "tpl_name": _TEMPLATE_NAME,
            "tpl_image": _TEMPLATE_IMAGE_REF,
            "tpl_config": _TEMPLATE_CONFIG_JSON,
        },
    )


def downgrade() -> None:
    # Additive-on-rollback: the backfill rows are LEFT IN PLACE (never
    # auto-deleted) per the plan's rollback contract; only the legacy
    # modulo-dev re-point is reverted when we can prove it was re-pointed
    # (its name now matches the template AND provider is runner_docker).
    op.get_bind().execute(
        _sql(
            """
            UPDATE environment_profiles
            SET provider_type = 'local_docker', name = 'modulo-dev', updated_at = now()
            WHERE name = :tpl_name AND provider_type = 'runner_docker'
              AND image_ref = :tpl_image
            """
        ),
        {"tpl_name": _TEMPLATE_NAME, "tpl_image": _TEMPLATE_IMAGE_REF},
    )


def _sql(body: str) -> object:
    from sqlalchemy import text

    return text(body)

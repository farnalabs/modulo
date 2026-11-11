"""Farna Control native baseline migration 0004_baseline_seed_data."""

from alembic import op

revision: str = "0004_baseline_seed_data"
down_revision: str | None = "0003_baseline_privileges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""SET search_path TO public;

INSERT INTO public.organisations (id, name, slug, settings_json, otel_config_json)
VALUES
    ('00000000-0000-0000-0000-000000000000', 'Orphan (unattributed errors)', 'orphan-unattributed-errors', '{}'::json, '{}'::json),
    ('00000000-0000-0000-0000-000000000001', 'Modulo', '_modulo_sentinel', '{}'::json, '{}'::json)
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.evidence_redaction_rules (organisation_id, key_pattern, role, redacted_fields)
SELECT o.id, '*', 'viewer', ARRAY['sensitive_output', 'internal_metadata']::text[]
FROM public.organisations AS o
WHERE NOT EXISTS (
    SELECT 1 FROM public.evidence_redaction_rules AS r
    WHERE r.organisation_id = o.id AND r.key_pattern = '*' AND r.role = 'viewer'
)
ON CONFLICT DO NOTHING;

INSERT INTO public.organisation_cost_state (org_id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents)
SELECT id, NULL, NULL, 0
FROM public.organisations
ON CONFLICT (org_id) DO NOTHING;""")


def downgrade() -> None:
    op.execute("""""")

"""FAR-1107 chunk 8: sweep PolicyGate rows for guardrail evals (§5.4).

Creates PolicyGate rows for existing guardrail-type Eval rows with
``config_json.action IN ('warn', 'block')``. Guardrails with action
``observe`` or ``redact`` do not bind to a PolicyGate (§3.1).

The binding validator's guardrail exclusion (exclusion 3) was retired by
this chunk, so the sweep does not need to bypass it — the rows are
created directly via SQL.

Revision ID: 0264_guardrail_policy_gate_sweep
Revises: 0263_evidence_layer
Create Date: 2026-09-29
"""

import json

from alembic import op
from sqlalchemy import text

revision = "0264_guardrail_policy_gate_sweep"
down_revision = "0263_evidence_layer"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()

    # Find guardrail evals with action warn/block that have no live PolicyGate.
    rows = bind.execute(
        text(
            """
            SELECT e.id, e.organisation_id, e.node_id, e.config_json
            FROM evals e
            WHERE e.eval_type = 'guardrail'
              AND e.deleted_at IS NULL
              AND (e.config_json->>'action') IN ('warn', 'block')
              AND e.node_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM policy_gates pg
                  WHERE pg.eval_id = e.id
                    AND pg.organisation_id = e.organisation_id
                    AND pg.deleted_at IS NULL
              )
            """
        )
    ).fetchall()

    created = 0
    skipped = 0
    for row in rows:
        eval_id = row[0]
        org_id = row[1]
        node_id = row[2]
        config_json = row[3]
        if isinstance(config_json, str):
            config_json = json.loads(config_json)
        action = config_json.get("action", "warn")

        # Action mapping: warn->warn, block->block (§3.1).
        if action not in ("warn", "block"):
            skipped += 1
            continue

        bind.execute(
            text(
                """
                INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, action, version, created_at)
                VALUES (gen_random_uuid(), :org_id, :eval_id, :node_id, :action, 1, now())
                ON CONFLICT (eval_id) WHERE deleted_at IS NULL DO NOTHING
                """
            ),
            {"org_id": org_id, "eval_id": eval_id, "node_id": node_id, "action": action},
        )
        created += 1

    op.get_bind().execute(
        text("SELECT pg_notify('alembic_log', :msg)"),
        {"msg": f"guardrail_policy_gate_sweep: created={created} skipped={skipped}"},
    )


def downgrade() -> None:
    # The sweep is additive and non-destructive — downgrade removes the
    # rows created by this migration. Since we cannot distinguish sweep-created
    # rows from author-created rows, downgrade is a no-op (rows are inert
    # without the resolver wiring, which is reverted by the code change).
    pass

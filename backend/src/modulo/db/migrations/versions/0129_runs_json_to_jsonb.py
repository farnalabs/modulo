"""Convert ``runs`` JSON columns to JSONB (dist db-runs-jsonb).

Revision ID: 0129_runs_json_to_jsonb
Revises: 0128_add_fk_lookup_indexes
Create Date: 2026-08-23

The ``runs`` table carries a set of JSON-typed payload columns. Four of them
(``work_item_refs``, ``raw_output_markers``, ``run_classification``,
``blocked_partial_summary``) were already created as Postgres ``jsonb`` in
earlier migrations (0100/0110/0111) so they could carry partial GIN indexes and
benefit from jsonb's binary storage + containment operators. The remaining
seven were created as plain ``json``:

- ``cost_breakdown`` (0066 / 0110 reconciliation)
- ``node_token_usage`` (0003)
- ``input_payload`` (0003)
- ``outputs_json`` (0003)
- ``node_telemetry_json`` (0110 reconciliation)
- ``guardrail_summary_json`` (0113)
- ``variant_config_snapshot`` (0118)

This migration brings them up to the same ``jsonb`` standard. The cast
``USING <col>::jsonb`` is lossless for every existing row (NULL stays NULL;
well-formed ``json`` data re-parses identically as ``jsonb``), so the
migration is data-safe on any populated database.

The ORM model keeps these columns mapped as generic ``sqlalchemy.JSON`` (NOT
``JSONB``) for SQLite/MariaDB parity — the same convention the four
pre-existing jsonb columns follow. Migrations run against Postgres only; the
SQLite unit-test backend uses the ORM model, so the type change lives entirely
in the migration, never in the model.

Downgrade reverts each column to ``json`` (also lossless).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0129_runs_json_to_jsonb"
down_revision: str | None = "0128_add_fk_lookup_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Runs columns currently typed ``json`` that this migration promotes to ``jsonb``.
_JSON_COLUMNS: tuple[str, ...] = (
    "cost_breakdown",
    "node_token_usage",
    "input_payload",
    "outputs_json",
    "node_telemetry_json",
    "guardrail_summary_json",
    "variant_config_snapshot",
)


def upgrade() -> None:
    # Postgres-only: plain ``json`` -> ``jsonb`` cast. SQLite uses the ORM model
    # (generic JSON), so skip on non-Postgres dialects.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for col in _JSON_COLUMNS:
        op.execute(f'ALTER TABLE public."runs" ALTER COLUMN "{col}" TYPE jsonb USING "{col}"::jsonb;')


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for col in _JSON_COLUMNS:
        op.execute(f'ALTER TABLE public."runs" ALTER COLUMN "{col}" TYPE json USING "{col}"::json;')

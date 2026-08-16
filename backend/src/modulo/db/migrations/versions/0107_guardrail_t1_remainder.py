"""Guardrails T1-remainder columns — org kill-switch + snapshot pins (FAR-223).

Revision ID: 0107_guardrail_t1_remainder
Revises: 0106_trigger_event_guardrail_blocked
Create Date: 2026-08-16

FAR-223 PR A adds the shipped-engine residuals:

* ``organisations.guardrails_kill_switch`` (BOOLEAN NOT NULL DEFAULT FALSE) +
  ``guardrails_kill_switch_at`` (TIMESTAMPTZ NULL) with a consistency CHECK
  ``NOT guardrails_kill_switch OR guardrails_kill_switch_at IS NOT NULL`` —
  the org-level kill-switch that downgrades every bound guardrail to observe
  mode at run start (item 9). Mirrors the ``triggers_paused`` precedent
  (migration 0069): a dedicated boolean column (atomic at statement level,
  multi-backend safe), never ``settings_json``.

* ``pipeline_snapshots.guardrail_pins_json`` (JSON NULL) — the guardrail set
  pinned at snapshot creation so replays evaluate the ORIGINAL conditions
  rather than the live rows (item 10). Mirrors the ``composite_bindings_json``
  column shape; generic ``sa.JSON`` keeps SQLite/MariaDB parity.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0107_guardrail_t1_remainder"
down_revision: str | None = "0106_trigger_event_guardrail_blocked"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_CHECK_CONSTRAINT_NAME = "ck_organisations_guardrails_kill_switch_at"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column("guardrails_kill_switch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "organisations",
        sa.Column("guardrails_kill_switch_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    if _is_postgres():
        op.execute(
            f"ALTER TABLE organisations ADD CONSTRAINT {_ORG_CHECK_CONSTRAINT_NAME} "
            "CHECK (NOT guardrails_kill_switch OR guardrails_kill_switch_at IS NOT NULL)"
        )
    else:
        with op.batch_alter_table("organisations") as batch:
            batch.create_check_constraint(
                _ORG_CHECK_CONSTRAINT_NAME,
                "NOT guardrails_kill_switch OR guardrails_kill_switch_at IS NOT NULL",
            )

    op.add_column(
        "pipeline_snapshots",
        sa.Column("guardrail_pins_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pipeline_snapshots", "guardrail_pins_json")
    if _is_postgres():
        op.execute(f"ALTER TABLE organisations DROP CONSTRAINT IF EXISTS {_ORG_CHECK_CONSTRAINT_NAME}")
    else:
        with op.batch_alter_table("organisations") as batch:
            batch.drop_constraint(_ORG_CHECK_CONSTRAINT_NAME, type_="check")
    op.drop_column("organisations", "guardrails_kill_switch_at")
    op.drop_column("organisations", "guardrails_kill_switch")

"""Add ``organisations.guardrail_pins_json`` snapshot pin (FAR-219).

Revision ID: 0105_guardrail_pins
Revises: 0104_trigger_event_auto_deactivated
Create Date: 2026-08-15

FAR-219 T3 (PR-gated guardrail config-as-code) layers a git-style review and
apply workflow on top of the shipped T1 guardrail engine. The org's applied
config snapshot — applied/proposed content hashes, serialized YAML, timestamps
and status — is pinned here, following the PRD's planned ``guardrail_pins_json``
naming. The column is deliberately stored on the ``organisations`` table (the
org-scoped home of org-level settings, matching ``settings_json`` /
``otel_config_json``) rather than a new table.

``sa.JSON`` is used for multi-backend portability (Postgres renders JSON, the
same type as the sibling org columns; SQLite/MariaDB accept it natively). The
column carries no default — absence of the key means "never applied".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0105_guardrail_pins"
down_revision: str | None = "0104_trigger_event_auto_deactivated"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organisations", sa.Column("guardrail_pins_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("organisations", "guardrail_pins_json")

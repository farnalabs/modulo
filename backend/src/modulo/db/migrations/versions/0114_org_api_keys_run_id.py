"""Add ``org_api_keys.run_id`` for per-run runner-role API keys (FAR-296 Phase 3b).

Revision ID: 0114_org_api_keys_run_id
Revises: 0113_guardrail_summary
Create Date: 2026-08-18

FAR-296 Phase 3b mints a short-TTL, runner-role API key per script-mode sandbox
run so the script can authenticate to the Modulo API (trigger/list runs only —
never pipelines/connectors/secrets) without exposing the long-lived host
credentials. The ``run_id`` linkage column lets the executor revoke every key
minted for a run at finalization, and lets the housekeeping sweep backstop
stale keys. Nullable: regular operator/runner keys created via the API-key
management UI carry no run linkage.

The ``ix_org_api_keys_run_id`` index backs the revocation lookup
(``organisation_id + run_id + revoked_at IS NULL``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0114_org_api_keys_run_id"
down_revision: str | None = "0113_guardrail_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org_api_keys", sa.Column("run_id", sa.Uuid(), nullable=True))
    op.create_index("ix_org_api_keys_run_id", "org_api_keys", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_org_api_keys_run_id", table_name="org_api_keys")
    op.drop_column("org_api_keys", "run_id")

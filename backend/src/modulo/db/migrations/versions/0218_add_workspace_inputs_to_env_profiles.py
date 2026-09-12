"""Add workspace_inputs column to environment_profiles (FAR-802 MWI P1).

ADR 033 (Managed Workspace Inputs) declares that environment profiles carry
a list of input descriptors.  This migration adds the ``workspace_inputs``
column — a nullable JSON list — to ``environment_profiles``.

Nullable: existing profiles have no inputs.  The server default ``'[]'``
ensures that raw-SQL inserts that omit the column get an empty list rather
than NULL, consistent with the ORM-level ``default=list``.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0218_add_workspace_inputs_to_env_profiles"
down_revision: str | None = "0217_hitl_claims_decided_by"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "environment_profiles",
        sa.Column(
            "workspace_inputs",
            sa.JSON(),
            nullable=True,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("environment_profiles", "workspace_inputs")

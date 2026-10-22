"""FAR-2054: persist provenance-typed in-loop guardrail coverage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0325_run_guardrail_coverage"
down_revision: str | None = "0324_purge_eval_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("guardrail_coverage_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "guardrail_coverage_json")

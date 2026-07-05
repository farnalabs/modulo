"""Stub migration — error_tracking branch was restructured.

The error_tracking feature was originally branched at migration 0050 but later
rebased onto the main chain at 0075. The staging database still has the original
0051_error_tracking revision in its alembic_version history, but all schema
changes for error_tracking are handled by 0075_error_tracking and later
migrations. This empty pass-through allows Alembic to resolve the chain.
"""

from collections.abc import Sequence
from typing import cast

revision: str = "0051_error_tracking"
down_revision: str | Sequence[str] | None = "0050_composite_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

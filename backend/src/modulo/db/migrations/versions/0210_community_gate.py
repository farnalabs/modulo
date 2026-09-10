"""community execution gate on collection_install (FAR-764).

Revision ID: 0209_community_gate
Revises: 0208_notification_indexes_and_constraint
Create Date: 2026-09-10

Adds ``community_sourced`` and ``agents_granted`` boolean columns to
``collection_install``.  ``community_sourced`` is set at install time when
the collection primitive has ``source != 'local'`` (community or registry
primitives).  ``agents_granted`` starts False and is flipped True by the
operator via the ``POST /library/collections/{id}/installs/{install_id}/grant``
endpoint.  Until granted, community-sourced agents run under a default-deny
tool/connector scope (enforced at execution time in node_runner).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0209_community_gate"
down_revision: str | None = "0208_notification_indexes_and_constraint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLLECTION_INSTALL = "collection_install"


def upgrade() -> None:
    op.add_column(
        _COLLECTION_INSTALL,
        sa.Column(
            "community_sourced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        _COLLECTION_INSTALL,
        sa.Column(
            "agents_granted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_COLLECTION_INSTALL, "agents_granted")
    op.drop_column(_COLLECTION_INSTALL, "community_sourced")

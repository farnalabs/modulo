"""community execution gate on collection_install (FAR-764).

Revision ID: 0210_community_gate
Revises: 0209_collection_install_id_entity_columns
Create Date: 2026-09-10

Adds ``community_sourced`` and ``agents_granted`` boolean columns to
``collection_install``.  ``community_sourced`` is set at install time when
the collection primitive has ``source != 'local'`` (community or registry
primitives).  ``agents_granted`` starts False and is flipped True by the
operator via the ``POST /library/collections/{id}/installs/{install_id}/grant``
endpoint.  Until granted, community-sourced agents run under a default-deny
tool/connector scope (enforced at execution time in node_runner).

Chains on top of main's ``0209_collection_install_id_entity_columns`` (which
adds the denormalised ``collection_install_id`` provenance columns to the
entity tables).  This revision is the single head of the migration chain.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0210_community_gate"
down_revision: str | None = "0209_collection_install_id_entity_columns"
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

"""Drop the duplicate ``collection_install.installed_entities`` JSON column.

Revision ID: 0313_collection_install_drop_installed_entities
Revises: 0312_memberships_active_lookups
Create Date: 2026-10-14

FAR-1754: which schemas/agents/pipelines a collection install wrote was
persisted TWICE — in the ``collection_install_entity`` child table AND in the
parent JSON column ``collection_install.installed_entities`` — with no
mechanism keeping the two copies in sync. ``core/library_service/uninstall.py``
acts on the child table while the install API response read the JSON copy, so
any divergence silently misreported what uninstall would do.

The child table is now the SINGLE source of truth: this migration drops the
JSON column, the model no longer declares it, install no longer writes it, and
the API response derives its ``installed_entities`` list from the child rows
(a query on ``install_id``). The response FIELD is unchanged, so the wire
contract (and the generated frontend schema) is unaffected.

Data note: dropping the column discards the duplicate copy only — every
``collection_install_entity`` row (the source of truth) is untouched. The
``downgrade()`` re-adds the column EMPTY (a downgrade cannot resurrect the
discarded copy; installs made after the upgrade keep no JSON copy at all).

Guards follow the ``0310`` house pattern: an inspector existence check keeps
the upgrade idempotent/re-runnable and stops the downgrade from failing on a
schema that still has the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0313_collection_install_drop_installed_entities"
down_revision: str | None = "0312_memberships_active_lookups"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _column_exists(table: str, column: str) -> bool:
    """Inspector existence check so the DDL stays re-runnable."""
    inspector = sa.inspect(op.get_bind())
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    if _column_exists("collection_install", "installed_entities"):
        op.drop_column("collection_install", "installed_entities")


def downgrade() -> None:
    # Re-add the column EMPTY (see the module docstring's data note). The type
    # mirrors 0207's ``json_type``: jsonb on PostgreSQL, generic JSON elsewhere
    # (the repo-wide multi-backend parity convention).
    if not _column_exists("collection_install", "installed_entities"):
        op.add_column(
            "collection_install",
            sa.Column(
                "installed_entities",
                sa.JSON().with_variant(JSONB(), "postgresql"),
                nullable=True,
            ),
        )

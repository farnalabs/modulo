"""Replace single chat_messages session_id index with a composite lookup index.

Revision ID: 0126_remy_lookup_indexes
Revises: 0125_soft_delete_lookup_indexes
Create Date: 2026-08-23

``chat_messages`` is almost always read as ``WHERE session_id = $1
ORDER BY created_at`` (see backend/src/modulo/api/routes/remy.py). The
existing ``ix_chat_messages_session_id`` index only covers the equality
predicate, so Postgres still has to sort the matched rows by
``created_at``.

This adds a composite index ``(session_id, created_at)`` that satisfies
both the filter and the ordering (index-only scan, no sort), and drops the
now-redundant single-column ``ix_chat_messages_session_id`` index.

Postgres-only concern: ``postgresql_where`` is not used here; on the
deprecated SQLite / MariaDB backends the same btree index is created.
"""

from alembic import op

revision = "0126_remy_lookup_indexes"
down_revision = "0125_soft_delete_lookup_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.create_index(
        "ix_chat_messages_session_id_created_at",
        "chat_messages",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_id_created_at", table_name="chat_messages")
    op.create_index(
        "ix_chat_messages_session_id",
        "chat_messages",
        ["session_id"],
    )

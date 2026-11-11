"""Add missing FK constraints on agents.template_id and agents.collection_install_id.

Revision ID: 0223_agents_add_foreign_keys
Revises: 0222_journey_provenance
Create Date: 2026-09-13

The ``template_id`` column references ``composite_templates.id`` but was
never given a FK constraint.  ``collection_install_id`` references
``collection_install.install_id`` for the same reason.  Both are nullable
and use ON DELETE SET NULL so that removing the target row clears the
stamp rather than cascading or blocking.
"""

from alembic import op

revision = "0223_agents_add_foreign_keys"
down_revision = "0222_journey_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_agents_template_id",
        "agents",
        "composite_templates",
        ["template_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_agents_collection_install_id",
        "agents",
        "collection_install",
        ["collection_install_id"],
        ["install_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agents_collection_install_id", "agents", type_="foreignkey")
    op.drop_constraint("fk_agents_template_id", "agents", type_="foreignkey")

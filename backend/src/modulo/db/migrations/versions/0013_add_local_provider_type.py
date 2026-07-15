"""add local provider_type to environment_profiles constraint

Revision ID: 0013_add_local_provider_type
Revises: 0012_onboarding_progress
Create Date: 2026-07-15 17:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_add_local_provider_type"
down_revision: str | None = "0012_onboarding_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE environment_profiles DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type")
    op.execute(
        "ALTER TABLE environment_profiles ADD CONSTRAINT ck_env_profiles_provider_type "
        "CHECK (provider_type IN ('local_docker', 'e2b', 'local'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE environment_profiles DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type")
    op.execute(
        "ALTER TABLE environment_profiles ADD CONSTRAINT ck_env_profiles_provider_type "
        "CHECK (provider_type IN ('local_docker', 'e2b'))"
    )

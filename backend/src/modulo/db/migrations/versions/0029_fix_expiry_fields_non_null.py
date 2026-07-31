"""Make expiry/lease fields non-nullable with reasonable defaults."""

from alembic import op

revision = "0029_fix_expiry_fields_non_null"
down_revision = "0028_add_claimed_by_to_runs"


def upgrade() -> None:
    # HitlClaim.expires_at
    op.execute("UPDATE hitl_claims SET expires_at = NOW() + INTERVAL '15 minutes' WHERE expires_at IS NULL")
    op.execute("ALTER TABLE hitl_claims ALTER COLUMN expires_at SET DEFAULT NOW() + INTERVAL '15 minutes'")
    op.execute("ALTER TABLE hitl_claims ALTER COLUMN expires_at SET NOT NULL")

    # WorkspaceLease.lease_expires_at
    op.execute(
        "UPDATE workspace_leases SET lease_expires_at = NOW() + INTERVAL '30 minutes' WHERE lease_expires_at IS NULL"
    )
    op.execute(
        "ALTER TABLE workspace_leases ALTER COLUMN lease_expires_at SET DEFAULT NOW() + INTERVAL '30 minutes'"
    )
    op.execute("ALTER TABLE workspace_leases ALTER COLUMN lease_expires_at SET NOT NULL")

    # OrgApiKey.expires_at
    op.execute("UPDATE org_api_keys SET expires_at = NOW() + INTERVAL '365 days' WHERE expires_at IS NULL")
    op.execute("ALTER TABLE org_api_keys ALTER COLUMN expires_at SET DEFAULT NOW() + INTERVAL '365 days'")
    op.execute("ALTER TABLE org_api_keys ALTER COLUMN expires_at SET NOT NULL")

    # Notification.expires_at
    op.execute("UPDATE notifications SET expires_at = NOW() + INTERVAL '90 days' WHERE expires_at IS NULL")
    op.execute("ALTER TABLE notifications ALTER COLUMN expires_at SET DEFAULT NOW() + INTERVAL '90 days'")
    op.execute("ALTER TABLE notifications ALTER COLUMN expires_at SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE hitl_claims ALTER COLUMN expires_at DROP NOT NULL")
    op.execute("ALTER TABLE hitl_claims ALTER COLUMN expires_at DROP DEFAULT")
    op.execute("ALTER TABLE workspace_leases ALTER COLUMN lease_expires_at DROP NOT NULL")
    op.execute("ALTER TABLE workspace_leases ALTER COLUMN lease_expires_at DROP DEFAULT")
    op.execute("ALTER TABLE org_api_keys ALTER COLUMN expires_at DROP NOT NULL")
    op.execute("ALTER TABLE org_api_keys ALTER COLUMN expires_at DROP DEFAULT")
    op.execute("ALTER TABLE notifications ALTER COLUMN expires_at DROP NOT NULL")
    op.execute("ALTER TABLE notifications ALTER COLUMN expires_at DROP DEFAULT")

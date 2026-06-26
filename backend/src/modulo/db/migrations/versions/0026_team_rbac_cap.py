"""Add team RBAC: CHECK constraint on team_memberships.role + privilege cap trigger.

Removes the legacy ``member`` role value, replacing it with ``viewer``,
and adds a trigger that prevents assigning a team role higher than the
user's org role.

Revision ID: 0026_team_rbac_cap
Revises: 0025_team_visibility_rls
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_team_rbac_cap"
down_revision: str | Sequence[str] | None = "0025_team_visibility_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Migrate existing 'member' rows to 'viewer'
    op.execute(sa.text("UPDATE team_memberships SET role = 'viewer' WHERE role = 'member'"))

    # 2. Change column default to 'viewer'
    op.execute(sa.text("ALTER TABLE team_memberships ALTER COLUMN role SET DEFAULT 'viewer'"))

    # 3. Add CHECK constraint restricting to valid team roles
    op.create_check_constraint(
        "ck_team_memberships_role",
        "team_memberships",
        sa.text("role IN ('viewer', 'runner', 'operator', 'admin')"),
    )

    # 4. Create trigger function for privilege cap
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION check_team_privilege_cap()
        RETURNS TRIGGER AS $$
        DECLARE
            _user_org_role TEXT;
            _org_level INT;
            _team_level INT;
        BEGIN
            SELECT org_role INTO _user_org_role
            FROM users
            WHERE id = NEW.user_id;

            _org_level := CASE _user_org_role
                WHEN 'viewer' THEN 0
                WHEN 'runner' THEN 1
                WHEN 'operator' THEN 2
                WHEN 'admin' THEN 3
                ELSE -1
            END;

            _team_level := CASE NEW.role
                WHEN 'viewer' THEN 0
                WHEN 'runner' THEN 1
                WHEN 'operator' THEN 2
                WHEN 'admin' THEN 3
                ELSE -1
            END;

            IF _team_level > _org_level THEN
                RAISE EXCEPTION
                    'Team role "%" exceeds org role "%" for user %',
                    NEW.role, _user_org_role, NEW.user_id;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """)
    )

    # 5. Attach trigger to team_memberships
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_team_privilege_cap
        BEFORE INSERT OR UPDATE ON team_memberships
        FOR EACH ROW
        EXECUTE FUNCTION check_team_privilege_cap()
        """)
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_team_privilege_cap ON team_memberships"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS check_team_privilege_cap()"))
    op.drop_constraint("ck_team_memberships_role", "team_memberships")
    op.execute(sa.text("ALTER TABLE team_memberships ALTER COLUMN role SET DEFAULT 'member'"))

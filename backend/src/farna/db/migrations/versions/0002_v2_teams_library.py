"""v2 — Teams, Library & Schemas.

Creates team management, library primitives with ratings/abuse reports,
and schema versioning tables. Introduces the library fork-provenance
trigger, team privilege-cap trigger, and team-scoped visibility RLS.

Revision ID: 0002_v2_teams_library
Revises: 0001_v2_identity_org
Create Date: 2026-07-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_v2_teams_library"
down_revision: str | None = "0001_v2_identity_org"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STRICT_RLS: tuple[str, ...] = (
    "teams",
    "team_memberships",
    "library_primitives",
    "primitive_ratings",
    "primitive_abuse_reports",
    "schemas",
    "schema_versions",
)

_TEAM_SCOPED_RLS: tuple[str, ...] = ("library_primitives",)

_TENANT_REFS: tuple[tuple[str, str, str], ...] = (
    ("teams", "account_id", "accounts"),
    ("team_memberships", "team_id", "teams"),
    ("team_memberships", "account_id", "accounts"),
    ("library_primitives", "account_id", "accounts"),
    ("library_primitives", "forked_from", "library_primitives"),
    ("library_primitives", "owner_team_id", "teams"),
    ("library_primitives", "update_available_version_id", "library_primitives"),
    ("primitive_ratings", "primitive_id", "library_primitives"),
    ("primitive_ratings", "account_id", "accounts"),
    ("primitive_abuse_reports", "primitive_id", "library_primitives"),
    ("primitive_abuse_reports", "rating_id", "primitive_ratings"),
    ("primitive_abuse_reports", "reporter_account_id", "accounts"),
    ("primitive_abuse_reports", "reviewer_account_id", "accounts"),
    ("schemas", "account_id", "accounts"),
    ("schema_versions", "schema_id", "schemas"),
    ("schema_versions", "account_id", "accounts"),
)


def upgrade() -> None:
    _create_tables()
    _create_trigger_functions()
    _create_triggers()
    _enable_rls()


def _create_tables() -> None:
    op.create_table(
        "teams",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("notification_endpoints", sa.JSON(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("daily_spend_limit", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "name", name="uq_teams_organisation_name"),
    )
    op.create_index(op.f("ix_teams_organisation_id"), "teams", ["organisation_id"], unique=False)
    op.create_table(
        "team_memberships",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("role IN ('viewer', 'runner', 'operator')", name="ck_team_memberships_role"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "account_id", name="uq_team_memberships_team_account"),
    )
    op.create_index(op.f("ix_team_memberships_account_id"), "team_memberships", ["account_id"], unique=False)
    op.create_index(op.f("ix_team_memberships_organisation_id"), "team_memberships", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_team_memberships_team_id"), "team_memberships", ["team_id"], unique=False)
    op.create_table(
        "library_primitives",
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("primitive_type", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=True),
        sa.Column("forked_from", sa.Uuid(), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("ed25519_signature", sa.String(length=256), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=True),
        sa.Column("average_rating", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), server_default="org", nullable=False),
        sa.Column("contribution_status", sa.String(length=20), nullable=True),
        sa.Column("auto_update", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("version_group_id", sa.Uuid(), nullable=True),
        sa.Column("update_available_version_id", sa.Uuid(), nullable=True),
        sa.Column("tier", sa.String(length=20), server_default="native", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "source IN ('local', 'registry', 'modulo', 'community')", name="ck_library_primitives_source"
        ),
        sa.CheckConstraint(
            "primitive_type IN ('schema', 'workflow', 'agent', 'integration', 'test_fixture', 'pipeline_template', 'composite', 'lifecycle_map')",
            name="ck_library_primitives_type",
        ),
        sa.CheckConstraint("visibility IN ('org', 'team', 'community')", name="ck_library_primitives_visibility"),
        sa.CheckConstraint(
            "visibility IN ('org', 'community') OR owner_team_id IS NOT NULL", name="ck_library_primitives_team_owner"
        ),
        sa.CheckConstraint("tier IN ('native', 'preview', 'in_dev')", name="ck_library_primitives_tier"),
        sa.CheckConstraint(
            "contribution_status IN ('draft', 'review_queue', 'published')",
            name="ck_library_primitives_contribution_status",
        ),
        sa.CheckConstraint(
            "average_rating IS NULL OR average_rating BETWEEN 1 AND 5", name="ck_library_primitives_rating"
        ),
        sa.CheckConstraint(
            "(source = 'local' AND source_url IS NULL AND checksum IS NULL AND ed25519_signature IS NULL AND verified IS NULL AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) OR (source = 'modulo' AND source_url IS NULL AND checksum IS NULL AND ed25519_signature IS NULL AND verified IS NULL AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) OR (source = 'community' AND source_url IS NULL AND checksum IS NULL AND ed25519_signature IS NULL AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) OR (source = 'registry' AND owner_team_id IS NULL AND visibility = 'org' AND forked_from IS NULL AND source_url IS NOT NULL AND checksum IS NOT NULL AND ed25519_signature IS NOT NULL AND verified IS NOT NULL AND download_count IS NOT NULL AND average_rating IS NOT NULL AND review_count IS NOT NULL)",
            name="ck_library_primitives_source_fields",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["forked_from"], ["library_primitives.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["update_available_version_id"], ["library_primitives.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "source", "slug", "version", name="uq_library_primitive_version"),
    )
    op.create_index(
        op.f("ix_library_primitives_organisation_id"), "library_primitives", ["organisation_id"], unique=False
    )
    op.create_table(
        "primitive_ratings",
        sa.Column("primitive_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("thumbs_up", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["primitive_id"], ["library_primitives.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "primitive_id", "account_id", name="uq_ratings_per_user"),
    )
    op.create_index(
        op.f("ix_primitive_ratings_organisation_id"), "primitive_ratings", ["organisation_id"], unique=False
    )
    op.create_index(op.f("ix_primitive_ratings_primitive_id"), "primitive_ratings", ["primitive_id"], unique=False)
    op.create_table(
        "primitive_abuse_reports",
        sa.Column("primitive_id", sa.Uuid(), nullable=False),
        sa.Column("rating_id", sa.Uuid(), nullable=True),
        sa.Column("reporter_account_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_account_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("status IN ('pending', 'reviewed', 'dismissed')", name="ck_abuse_reports_status"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["primitive_id"], ["library_primitives.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rating_id"], ["primitive_ratings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reporter_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewer_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_primitive_abuse_reports_organisation_id"), "primitive_abuse_reports", ["organisation_id"], unique=False
    )
    op.create_index(
        op.f("ix_primitive_abuse_reports_primitive_id"), "primitive_abuse_reports", ["primitive_id"], unique=False
    )
    op.create_table(
        "schemas",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("abstract_name", sa.String(length=255), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("deprecated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "name", name="uq_schemas_organisation_name"),
    )
    op.create_index(op.f("ix_schemas_organisation_id"), "schemas", ["organisation_id"], unique=False)
    op.create_table(
        "schema_versions",
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("published", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("deprecated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["schema_id"], ["schemas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schema_id", "version", "organisation_id", name="uq_schema_versions_schema_version_organisation"
        ),
    )
    op.create_index(op.f("ix_schema_versions_organisation_id"), "schema_versions", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_schema_versions_schema_id"), "schema_versions", ["schema_id"], unique=False)


def _create_trigger_functions() -> None:
    op.execute(
        sa.text("""
        CREATE FUNCTION enforce_library_fork_provenance() RETURNS trigger AS $$
        DECLARE
            parent_source text;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.forked_from IS DISTINCT FROM NEW.forked_from THEN
                RAISE EXCEPTION 'library primitive forked_from is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.forked_from IS NOT NULL THEN
                SELECT source INTO parent_source
                FROM library_primitives
                WHERE id = NEW.forked_from;
                IF parent_source IS DISTINCT FROM 'registry' THEN
                    RAISE EXCEPTION 'forked_from must reference a registry primitive'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    )
    op.execute(
        sa.text("""
        CREATE FUNCTION check_team_privilege_cap()
        RETURNS TRIGGER AS $$
        DECLARE
            _user_org_role TEXT;
            _org_level INT;
            _team_level INT;
        BEGIN
            SELECT role INTO _user_org_role
            FROM org_memberships
            WHERE account_id = NEW.account_id
              AND organisation_id = NEW.organisation_id;
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
                ELSE -1
            END;
            IF _team_level > _org_level THEN
                RAISE EXCEPTION
                    'Team role "%" exceeds org role "%" for account %',
                    NEW.role, _user_org_role, NEW.account_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    )


def _create_triggers() -> None:
    for child_table, child_column, parent_table in _TENANT_REFS:
        op.execute(
            sa.text(
                f'CREATE TRIGGER "trg_{child_table}_{child_column}_tenant" '
                f'BEFORE INSERT OR UPDATE OF "{child_column}", "organisation_id" ON "{child_table}" '
                f"FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('{parent_table}', '{child_column}')"
            )
        )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_library_primitives_fork_provenance "
            "BEFORE INSERT OR UPDATE OF forked_from ON library_primitives "
            "FOR EACH ROW EXECUTE FUNCTION enforce_library_fork_provenance()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_team_privilege_cap "
            "BEFORE INSERT OR UPDATE ON team_memberships "
            "FOR EACH ROW EXECUTE FUNCTION check_team_privilege_cap()"
        )
    )


def _enable_rls() -> None:
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    team = (
        "(visibility = 'org' OR visibility IS NULL) "
        "OR (owner_team_id IS NULL) "
        "OR (owner_team_id IN ("
        "SELECT team_id FROM team_memberships "
        "WHERE account_id = nullif(current_setting('app.user_id', true), '')::uuid"
        ")) "
        "OR (nullif(current_setting('app.org_role', true), '') = 'admin')"
    )
    for table in _STRICT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({strict})'))
    for table in _TEAM_SCOPED_RLS:
        op.execute(sa.text(f'CREATE POLICY rls_team_isolation ON "{table}" USING ({team})'))


def downgrade() -> None:
    for table in _TEAM_SCOPED_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"'))
    for table in _STRICT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_team_privilege_cap ON team_memberships"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_library_primitives_fork_provenance ON library_primitives"))
    for child_table, child_column, _ in _TENANT_REFS:
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS "trg_{child_table}_{child_column}_tenant" ON "{child_table}"'))
    op.execute(sa.text("DROP FUNCTION IF EXISTS enforce_library_fork_provenance() CASCADE"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS check_team_privilege_cap() CASCADE"))

    op.drop_index(op.f("ix_schema_versions_schema_id"), table_name="schema_versions")
    op.drop_index(op.f("ix_schema_versions_organisation_id"), table_name="schema_versions")
    op.drop_table("schema_versions")
    op.drop_index(op.f("ix_schemas_organisation_id"), table_name="schemas")
    op.drop_table("schemas")
    op.drop_index(op.f("ix_primitive_abuse_reports_primitive_id"), table_name="primitive_abuse_reports")
    op.drop_index(op.f("ix_primitive_abuse_reports_organisation_id"), table_name="primitive_abuse_reports")
    op.drop_table("primitive_abuse_reports")
    op.drop_index(op.f("ix_primitive_ratings_primitive_id"), table_name="primitive_ratings")
    op.drop_index(op.f("ix_primitive_ratings_organisation_id"), table_name="primitive_ratings")
    op.drop_table("primitive_ratings")
    op.drop_index(op.f("ix_library_primitives_organisation_id"), table_name="library_primitives")
    op.drop_table("library_primitives")
    op.drop_index(op.f("ix_team_memberships_team_id"), table_name="team_memberships")
    op.drop_index(op.f("ix_team_memberships_organisation_id"), table_name="team_memberships")
    op.drop_index(op.f("ix_team_memberships_account_id"), table_name="team_memberships")
    op.drop_table("team_memberships")
    op.drop_index(op.f("ix_teams_organisation_id"), table_name="teams")
    op.drop_table("teams")

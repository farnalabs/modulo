"""Schema-level tests for Account and OrgMembership models."""

from sqlalchemy import UniqueConstraint

from modulo.db.models import Account, Base, OrgMembership, OrgScoped


def test_account_table_exists() -> None:
    tables = Base.metadata.tables
    assert "accounts" in tables


def test_account_columns() -> None:
    cols = Base.metadata.tables["accounts"].c
    assert "id" in cols
    assert "email" in cols
    assert "display_name" in cols
    assert "password_hash" in cols
    assert "auth_provider" in cols
    assert "sso_subject" in cols
    assert "active" in cols
    assert "last_login" in cols
    assert "preferences" in cols
    assert "is_system_admin" in cols
    assert "created_at" in cols
    assert "updated_at" in cols


def test_account_email_unique() -> None:
    table = Base.metadata.tables["accounts"]
    has_unique = any(
        isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["email"]
        for c in table.constraints
    )
    assert has_unique


def test_account_auth_provider_check() -> None:
    checks = " ".join(
        str(c.sqltext) for c in Base.metadata.tables["accounts"].constraints if hasattr(c, "sqltext")
    )
    assert "local" in checks
    assert "oidc" in checks
    assert "saml" in checks
    assert "scim" in checks


def test_account_not_org_scoped() -> None:
    assert not issubclass(Account, OrgScoped)


def test_org_membership_table_exists() -> None:
    tables = Base.metadata.tables
    assert "org_memberships" in tables


def test_org_membership_columns() -> None:
    cols = Base.metadata.tables["org_memberships"].c
    assert "id" in cols
    assert "account_id" in cols
    assert "organisation_id" in cols
    assert "role" in cols
    assert "joined_at" in cols
    assert "deactivated_at" in cols
    assert "created_at" in cols
    assert "updated_at" in cols


def test_org_membership_is_org_scoped() -> None:
    assert issubclass(OrgMembership, OrgScoped)


def test_org_membership_role_check() -> None:
    checks = " ".join(
        str(c.sqltext) for c in Base.metadata.tables["org_memberships"].constraints if hasattr(c, "sqltext")
    )
    for role in ("owner", "admin", "operator", "runner", "viewer"):
        assert role in checks


def test_org_membership_account_org_unique() -> None:
    table = Base.metadata.tables["org_memberships"]
    has_unique = any(
        isinstance(c, UniqueConstraint)
        and sorted(col.name for col in c.columns) == ["account_id", "organisation_id"]
        for c in table.constraints
    )
    assert has_unique

"""Schema-level tests for Account and OrgMembership models."""

from sqlalchemy import CheckConstraint, UniqueConstraint

from modulo.db.models import Account, Base, OrgMembership, OrgScoped
from modulo.db.models.account import ACCOUNT_KINDS, KIND_HUMAN, KIND_SERVICE


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
        isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["email"] for c in table.constraints
    )
    assert has_unique


def test_account_auth_provider_check() -> None:
    checks = " ".join(str(c.sqltext) for c in Base.metadata.tables["accounts"].constraints if hasattr(c, "sqltext"))
    assert "local" in checks
    assert "oidc" in checks
    assert "saml" in checks
    assert "scim" in checks


# ---------------------------------------------------------------------------
# FAR-1385 slice 1 — the pinned ``kind`` shape.
#
# These pin the DECISION, not just the existence: kind is a NOT NULL column
# with a 'human' server default, the vocabulary is exactly human|service, a
# service row can never be a system admin or break-glass, and the human-shaped
# identity columns are nullable so a service row carries no synthetic email.
# ---------------------------------------------------------------------------


def test_account_kind_column_present_and_not_null_with_human_default() -> None:
    col = Base.metadata.tables["accounts"].c["kind"]
    assert col.nullable is False
    assert col.server_default is not None
    assert "human" in str(col.server_default.arg)


def test_account_kind_check_vocabulary() -> None:
    checks = {c.name: str(c.sqltext) for c in Base.metadata.tables["accounts"].constraints if hasattr(c, "sqltext")}
    assert "ck_accounts_kind" in checks
    assert "human" in checks["ck_accounts_kind"]
    assert "service" in checks["ck_accounts_kind"]


def test_account_service_privilege_check_forbids_admin_and_break_glass() -> None:
    checks = {c.name: str(c.sqltext) for c in Base.metadata.tables["accounts"].constraints if hasattr(c, "sqltext")}
    assert "ck_accounts_service_privilege" in checks
    sql = checks["ck_accounts_service_privilege"]
    assert "is_system_admin" in sql
    assert "is_break_glass" in sql


def test_account_human_shaped_identity_columns_are_nullable() -> None:
    """A service row carries no synthetic identity — pin the nullable shape."""
    cols = Base.metadata.tables["accounts"].c
    assert cols["email"].nullable is True
    assert cols["auth_provider"].nullable is True
    assert cols["sso_subject"].nullable is True
    assert cols["last_login"].nullable is True
    assert cols["password_hash"].nullable is True


def test_account_kind_vocabulary_constant_matches_check() -> None:
    assert ACCOUNT_KINDS == (KIND_HUMAN, KIND_SERVICE)
    assert KIND_HUMAN == "human"
    assert KIND_SERVICE == "service"


def test_account_kind_is_classified_by_the_shared_helpers() -> None:
    from modulo.core.principals import is_human_account, is_service_account

    human = Account(email="a@example.com", display_name="A", kind=KIND_HUMAN)
    service = Account(display_name="svc", kind=KIND_SERVICE)
    assert is_human_account(human) is True
    assert is_service_account(human) is False
    assert is_service_account(service) is True
    assert is_human_account(service) is False
    # A transient row with no kind set defaults to human (the server default).
    transient = Account(email="b@example.com", display_name="B")
    assert is_human_account(transient) is True


def test_account_kind_is_a_check_constraint_not_a_unique_column() -> None:
    table = Base.metadata.tables["accounts"]
    check_names = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
    assert "ck_accounts_kind" in check_names


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
    for role in ("admin", "operator", "runner", "viewer"):
        assert role in checks


def test_org_membership_role_check_has_no_owner() -> None:
    """The 'owner' role was dropped (ADR 047 A1a) — the model CHECK constraint
    must not silently regress and re-admit it."""
    checks = " ".join(
        str(c.sqltext) for c in Base.metadata.tables["org_memberships"].constraints if hasattr(c, "sqltext")
    )
    assert "owner" not in checks


def test_org_membership_account_org_unique() -> None:
    table = Base.metadata.tables["org_memberships"]
    has_unique = any(
        isinstance(c, UniqueConstraint) and sorted(col.name for col in c.columns) == ["account_id", "organisation_id"]
        for c in table.constraints
    )
    assert has_unique

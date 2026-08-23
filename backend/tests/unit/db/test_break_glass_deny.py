"""Unit tests for the shared break-glass deny builder + deny-site oracle.

Covers the pure deny/live decisions (expired / NULL-expiry / deactivated /
inactive each -> denied; live+unexpired+active -> not denied; non-break-glass
-> never denied; boundary instant), the PostgreSQL-rendered predicates emitted
by the single builder, and the deny-site oracle: a mapping table of every
live role-resolution / membership-lookup call site with its deny/fold status,
asserted for set-equality against an AST import-site scan of the codebase.
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.dialects import postgresql

from modulo.db.crud.break_glass_deny import (
    BREAK_GLASS_COLUMNS,
    denied_predicate,
    is_break_glass_denied,
    is_break_glass_live,
    live_predicate,
    render_sql,
)
from modulo.db.models.account import Account

_BACKEND_ROOT = Path(__file__).parents[3]
_SRC_ROOT = _BACKEND_ROOT / "src"
_SRC_MODULO = _SRC_ROOT / "modulo"

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _denied(
    *,
    is_break_glass: bool = True,
    expires_at: datetime | None = None,
    deactivated_at: datetime | None = None,
    active: bool = True,
    now: datetime = _NOW,
) -> bool:
    return is_break_glass_denied(
        is_break_glass=is_break_glass,
        break_glass_expires_at=expires_at,
        break_glass_deactivated_at=deactivated_at,
        active=active,
        now=now,
    )


def _live(
    *,
    is_break_glass: bool = True,
    expires_at: datetime | None = None,
    deactivated_at: datetime | None = None,
    active: bool = True,
    now: datetime = _NOW,
) -> bool:
    return is_break_glass_live(
        is_break_glass=is_break_glass,
        break_glass_expires_at=expires_at,
        break_glass_deactivated_at=deactivated_at,
        active=active,
        now=now,
    )


class TestIsBreakGlassDenied:
    def test_live_unexpired_active_not_denied(self) -> None:
        assert _denied(expires_at=_NOW + timedelta(hours=1)) is False
        assert _live(expires_at=_NOW + timedelta(hours=1)) is True

    def test_expired_is_denied(self) -> None:
        assert _denied(expires_at=_NOW - timedelta(seconds=1)) is True
        assert _live(expires_at=_NOW - timedelta(seconds=1)) is False

    def test_boundary_instant_expired(self) -> None:
        """expires_at <= now is deny-eligible (boundary included)."""
        assert _denied(expires_at=_NOW) is True
        assert _live(expires_at=_NOW) is False

    def test_null_expiry_is_denied(self) -> None:
        assert _denied(expires_at=None) is True
        assert _live(expires_at=None) is False

    def test_deactivated_is_denied(self) -> None:
        assert _denied(expires_at=_NOW + timedelta(hours=1), deactivated_at=_NOW) is True
        assert _live(expires_at=_NOW + timedelta(hours=1), deactivated_at=_NOW) is False

    def test_inactive_is_denied(self) -> None:
        assert _denied(expires_at=_NOW + timedelta(hours=1), active=False) is True
        assert _live(expires_at=_NOW + timedelta(hours=1), active=False) is False

    def test_non_break_glass_never_denied(self) -> None:
        """A normal account is never deny-eligible regardless of other fields."""
        assert _denied(is_break_glass=False, expires_at=None) is False
        assert _denied(is_break_glass=False, expires_at=_NOW - timedelta(hours=1)) is False
        assert _denied(is_break_glass=False, deactivated_at=_NOW) is False
        assert _denied(is_break_glass=False, active=False) is False
        assert _live(is_break_glass=False, expires_at=_NOW + timedelta(hours=1)) is False


class TestPredicateRendering:
    def test_denied_predicate_renders_postgres_sql(self) -> None:
        sql = render_sql(denied_predicate())
        assert "accounts.is_break_glass" in sql
        assert "IS NULL" in sql
        assert "IS NOT NULL" in sql
        assert "current_timestamp" in sql
        assert "IS NOT true" in sql  # active IS NOT TRUE branch
        assert "<= " in sql  # break_glass_expires_at <= current_timestamp

    def test_live_predicate_renders_postgres_sql(self) -> None:
        sql = render_sql(live_predicate())
        assert "accounts.is_break_glass" in sql
        assert "IS NULL" in sql
        assert "IS NOT NULL" in sql
        assert "current_timestamp" in sql
        assert "IS true" in sql  # active IS TRUE branch
        assert "> current_timestamp" in sql

    def test_join_negation_compiles(self) -> None:
        """The JOIN exclusion is NOT denied_predicate — a deny row is dropped."""
        negated = (~denied_predicate()).compile(dialect=postgresql.dialect())
        assert str(negated).startswith("NOT")

    def test_builder_columns_match_model(self) -> None:
        """The single-sourced column names exist on the ORM model."""
        for col in BREAK_GLASS_COLUMNS:
            assert hasattr(Account, col)


# ── deny-site oracle ──────────────────────────────────────────────────

#: Per-site deny/fold status mapping table (plan v17 §TTL-enforcement). A role-
#: resolution site must treat a denied break-glass account's None as a deny;
#: a membership-lookup site never grants a role (deny is applied downstream at
#: resolve_role_from_membership). Status values follow the plan's pinned set:
#: 401 / 403 / 422 / 409 / 503 / deny / fold.
ROLE_RESOLUTION_SITES: dict[str, str] = {
    "modulo/api/dependencies.py": "403 — require_target_org_role: non-member denied (assert_org_role fail-closed)",
    "modulo/api/mcp_server.py": "401/403 — fail-closed live-role deny (JWT + API-key role clamp)",
    "modulo/api/routes/api_keys.py": "403 — mint-cap deny (live role is None)",
    "modulo/api/routes/auth.py": "401 — refresh / ws-token / me deny (OrganisationMembershipNotFound)",
    "modulo/auth/dependencies.py": "401 — _verify_identity deny (OrganisationMembershipNotFound)",
    "modulo/auth/oauth.py": "401 — InvalidGrantError on OAuth token/refresh",
    "modulo/db/crud/hitl_gate_guard.py": "deny — HitlGateWeakeningDenied (fail-closed)",
}

MEMBERSHIP_LOOKUP_SITES: dict[str, str] = {
    "modulo/api/routes/admin.py": "fold — membership lookup only (deactivation/authz checks), no role grant",
    "modulo/api/routes/admin_orgs.py": "fold — membership lookup only, no role grant",
    "modulo/api/routes/scim.py": "fold — membership lookup only (SCIM deactivate), no role grant",
    "modulo/api/routes/teams.py": "fold — membership lookup only (team ops), no role grant",
    "modulo/auth/sso.py": "fold — membership lookup only (SSO), no role grant",
    "modulo/cli/migrate.py": "fold — membership lookup only (import), no role grant",
    "modulo/db/crud/scim.py": "fold — membership lookup only (SCIM CRUD), no role grant",
}

_IMPORT_MODULES = ("modulo.auth.dependencies", "modulo.db.crud.org_membership")
_ALLOWED_DENY_STATUS = {"401", "403", "422", "409", "503", "deny"}


def _scan_import_sites() -> tuple[set[str], set[str]]:
    """AST import-site scan: every module importing the two names.

    ``resolve_role_from_membership`` importers are role-resolution sites;
    ``get_membership_by_account_and_org`` importers are membership-lookup sites.
    ``org_membership.py`` (the definition module) is excluded.
    """
    role_sites: set[str] = set()
    lookup_sites: set[str] = set()
    for path in sorted(_SRC_MODULO.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel == "modulo/db/crud/org_membership.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        # Local names bound to the org_membership module (e.g. the
        # `from modulo.db.crud import org_membership as crud_org_membership`
        # alias used to keep crud attributes patchable in unit tests).
        org_membership_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in _IMPORT_MODULES:
                names = {alias.name for alias in node.names}
                if "resolve_role_from_membership" in names:
                    role_sites.add(rel)
                if "get_membership_by_account_and_org" in names:
                    lookup_sites.add(rel)
            elif isinstance(node, ast.ImportFrom) and node.module == "modulo.db.crud":
                for alias in node.names:
                    if alias.name == "org_membership":
                        org_membership_aliases.add(alias.asname or alias.name)
        # Attribute-access form: crud_org_membership.get_membership_by_account_and_org(...)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in org_membership_aliases
                and node.attr in ("get_membership_by_account_and_org", "resolve_role_from_membership")
            ):
                if node.attr == "get_membership_by_account_and_org":
                    lookup_sites.add(rel)
                else:
                    role_sites.add(rel)
    return role_sites, lookup_sites


class TestDenySiteOracle:
    def test_role_resolution_sites_set_equality(self) -> None:
        role_sites, _ = _scan_import_sites()
        assert role_sites == set(ROLE_RESOLUTION_SITES)

    def test_membership_lookup_sites_set_equality(self) -> None:
        _, lookup_sites = _scan_import_sites()
        assert lookup_sites == set(MEMBERSHIP_LOOKUP_SITES)

    def test_every_role_resolution_site_denies(self) -> None:
        """Each role-resolution site carries a deny status + a None-deny marker."""
        for rel, status in ROLE_RESOLUTION_SITES.items():
            code = (_SRC_ROOT / rel).read_text(encoding="utf-8")
            marker = status.split(" ")[0].rstrip("/")
            assert all(part in _ALLOWED_DENY_STATUS for part in marker.split("/")), (
                f"{rel}: invalid deny status {marker!r}"
            )
            assert "resolve_role_from_membership" in code, rel
            assert "None" in code, f"{rel} has no None-handling (deny) path"
            assert any(token in code for token in ("raise", "deny", "denied", "forbidden", "unauthorized")), rel

    def test_every_lookup_site_folds(self) -> None:
        """Membership-lookup sites fold: they never import the role resolver."""
        for rel, status in MEMBERSHIP_LOOKUP_SITES.items():
            code = (_SRC_ROOT / rel).read_text(encoding="utf-8")
            assert status.startswith("fold"), f"{rel}: expected fold status"
            assert "get_membership_by_account_and_org" in code, rel
            assert "resolve_role_from_membership" not in code, f"{rel} must not resolve roles"

    def test_oracle_sites_are_disjoint(self) -> None:
        assert not (set(ROLE_RESOLUTION_SITES) & set(MEMBERSHIP_LOOKUP_SITES))

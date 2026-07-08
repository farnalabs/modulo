"""Unit tests for team-level RBAC: role hierarchy and effective access model."""

from modulo.auth.team_rbac import (
    ORG_ROLE_HIERARCHY,
    TEAM_ROLE_HIERARCHY,
    VALID_ORG_ROLES,
    VALID_TEAM_ROLES,
    get_effective_team_role,
    org_role_level,
    team_role_level,
)


class TestConstants:
    def test_team_role_hierarchy_has_expected_roles(self) -> None:
        assert TEAM_ROLE_HIERARCHY == {
            "viewer": 0,
            "runner": 1,
            "operator": 2,
        }

    def test_org_role_hierarchy_has_expected_roles(self) -> None:
        assert ORG_ROLE_HIERARCHY == {
            "viewer": 0,
            "runner": 1,
            "operator": 2,
            "admin": 3,
        }

    def test_valid_team_roles_is_frozenset(self) -> None:
        assert frozenset({"viewer", "runner", "operator"}) == VALID_TEAM_ROLES

    def test_valid_org_roles_is_frozenset(self) -> None:
        assert frozenset({"viewer", "runner", "operator", "admin"}) == VALID_ORG_ROLES


class TestEffectiveAccessModel:
    def test_viewer_org_all_team_roles_capped_to_viewer(self) -> None:
        for team_role in VALID_TEAM_ROLES:
            assert get_effective_team_role("viewer", team_role) == "viewer"

    def test_runner_org_caps_team_roles_above_runner(self) -> None:
        assert get_effective_team_role("runner", "viewer") == "viewer"
        assert get_effective_team_role("runner", "runner") == "runner"
        assert get_effective_team_role("runner", "operator") == "runner"

    def test_operator_org_caps_team_roles_above_operator(self) -> None:
        assert get_effective_team_role("operator", "viewer") == "viewer"
        assert get_effective_team_role("operator", "runner") == "runner"
        assert get_effective_team_role("operator", "operator") == "operator"

    def test_admin_org_allows_all_team_roles(self) -> None:
        assert get_effective_team_role("admin", "viewer") == "viewer"
        assert get_effective_team_role("admin", "runner") == "runner"
        assert get_effective_team_role("admin", "operator") == "operator"


class TestGetEffectiveTeamRole:
    def test_returns_correct_role_for_valid_pairs(self) -> None:
        assert get_effective_team_role("admin", "operator") == "operator"
        assert get_effective_team_role("admin", "viewer") == "viewer"
        assert get_effective_team_role("viewer", "operator") == "viewer"
        assert get_effective_team_role("operator", "runner") == "runner"
        assert get_effective_team_role("runner", "operator") == "runner"

    def test_unknown_org_role_falls_back_to_viewer(self) -> None:
        assert get_effective_team_role("superadmin", "operator") == "viewer"

    def test_unknown_team_role_falls_back_to_viewer(self) -> None:
        assert get_effective_team_role("admin", "superadmin") == "viewer"

    def test_both_unknown_falls_back_to_viewer(self) -> None:
        assert get_effective_team_role("superadmin", "superadmin") == "viewer"


class TestTeamRoleLevel:
    def test_returns_level_for_valid_roles(self) -> None:
        assert team_role_level("viewer") == 0
        assert team_role_level("runner") == 1
        assert team_role_level("operator") == 2

    def test_returns_minus_one_for_unknown_role(self) -> None:
        assert team_role_level("superadmin") == -1


class TestOrgRoleLevel:
    def test_returns_level_for_valid_roles(self) -> None:
        assert org_role_level("viewer") == 0
        assert org_role_level("runner") == 1
        assert org_role_level("operator") == 2
        assert org_role_level("admin") == 3

    def test_returns_minus_one_for_unknown_role(self) -> None:
        assert org_role_level("superadmin") == -1

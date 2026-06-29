"""Unit tests for team-level RBAC: role hierarchy and effective access model."""

from modulo.auth.team_rbac import (
    EFFECTIVE_ACCESS_MODEL,
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
        assert VALID_TEAM_ROLES == frozenset({"viewer", "runner", "operator"})

    def test_valid_org_roles_is_frozenset(self) -> None:
        assert VALID_ORG_ROLES == frozenset({"viewer", "runner", "operator", "admin"})


class TestEffectiveAccessModel:
    def test_effective_access_model_has_all_org_roles(self) -> None:
        assert set(EFFECTIVE_ACCESS_MODEL) == {"viewer", "runner", "operator", "admin"}

    def test_viewer_org_all_team_roles_capped_to_viewer(self) -> None:
        caps = EFFECTIVE_ACCESS_MODEL["viewer"]
        for team_role in VALID_TEAM_ROLES:
            assert caps[team_role] == "viewer"

    def test_runner_org_caps_team_roles_above_runner(self) -> None:
        caps = EFFECTIVE_ACCESS_MODEL["runner"]
        assert caps["viewer"] == "viewer"
        assert caps["runner"] == "runner"
        assert caps["operator"] == "runner"

    def test_operator_org_caps_team_roles_above_operator(self) -> None:
        caps = EFFECTIVE_ACCESS_MODEL["operator"]
        assert caps["viewer"] == "viewer"
        assert caps["runner"] == "runner"
        assert caps["operator"] == "operator"

    def test_admin_org_allows_all_team_roles(self) -> None:
        caps = EFFECTIVE_ACCESS_MODEL["admin"]
        assert caps["viewer"] == "viewer"
        assert caps["runner"] == "runner"
        assert caps["operator"] == "operator"


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

"""Unit tests for the centralized permission registry (ADR 017).

Covers ``assert_org_role`` boundary matrix, ``resolve_required`` fail-fast
behaviour, and the import-time registry validation.
"""

import pytest

from modulo.auth.permissions import (
    PERMISSIONS,
    PermissionConfigurationError,
    PermissionDenied,
    assert_org_role,
    resolve_required,
)
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY


class TestAssertOrgRole:
    """Boundary matrix: each role x each required level x unknown/empty/None."""

    @pytest.mark.parametrize("required", ["viewer", "runner", "operator", "admin"])
    def test_sufficient_role_passes(self, required: str) -> None:
        assert assert_org_role(required, required, "test.permission") is None

    @pytest.mark.parametrize(
        ("role", "required"),
        [
            ("viewer", "runner"),
            ("viewer", "operator"),
            ("viewer", "admin"),
            ("runner", "operator"),
            ("runner", "admin"),
            ("operator", "admin"),
        ],
    )
    def test_insufficient_role_denied(self, role: str, required: str) -> None:
        with pytest.raises(PermissionDenied) as excinfo:
            assert_org_role(role, required, "test.permission")
        assert excinfo.value.permission == "test.permission"
        assert excinfo.value.required_role == required
        assert excinfo.value.actual_role == role

    @pytest.mark.parametrize("required", ["viewer", "runner", "operator", "admin"])
    def test_unknown_role_denied(self, required: str) -> None:
        with pytest.raises(PermissionDenied) as excinfo:
            assert_org_role("superadmin", required, "test.permission")
        assert excinfo.value.reason == "unknown_role"
        assert excinfo.value.actual_role == "superadmin"

    @pytest.mark.parametrize("required", ["viewer", "runner", "operator", "admin"])
    def test_empty_string_role_denied(self, required: str) -> None:
        with pytest.raises(PermissionDenied) as excinfo:
            assert_org_role("", required, "test.permission")
        assert excinfo.value.reason == "unknown_role"
        assert not excinfo.value.actual_role

    @pytest.mark.parametrize("required", ["viewer", "runner", "operator", "admin"])
    def test_none_role_denied(self, required: str) -> None:
        with pytest.raises(PermissionDenied) as excinfo:
            assert_org_role(None, required, "test.permission")
        assert excinfo.value.reason == "unknown_role"
        assert excinfo.value.actual_role is None

    def test_whitespace_only_role_denied(self) -> None:
        with pytest.raises(PermissionDenied) as excinfo:
            assert_org_role("   ", "runner", "test.permission")
        assert excinfo.value.reason == "unknown_role"

    def test_unknown_required_role_is_configuration_error(self) -> None:
        with pytest.raises(PermissionConfigurationError):
            assert_org_role("admin", "superadmin", "test.permission")

    def test_message_contains_required_and_actual(self) -> None:
        with pytest.raises(PermissionDenied) as excinfo:
            assert_org_role("viewer", "operator", "test.permission")
        message = str(excinfo.value)
        assert "test.permission" in message
        assert "operator" in message
        assert "viewer" in message


class TestResolveRequired:
    def test_known_key_returns_role(self) -> None:
        assert resolve_required("pipeline.graph.update") == "operator"
        assert resolve_required("run.trigger") == "runner"
        assert resolve_required("metrics.ingest") == "viewer"
        assert resolve_required("org.email.manage") == "admin"

    def test_unknown_key_raises_configuration_error(self) -> None:
        with pytest.raises(PermissionConfigurationError):
            resolve_required("nonexistent.permission")

    def test_key_resolves_match_required_adr_keys(self) -> None:
        expected = {
            "pipeline.graph.update": "operator",
            "run.trigger": "runner",
            "connector.create": "operator",
            "connector.delete": "operator",
            "secret.manage": "operator",
            "trigger.events.list": "runner",
            "api_key.create": "runner",
            "api_key.update": "runner",
            "metrics.ingest": "viewer",
            "oauth.client.create": "operator",
            "oauth.client.list": "operator",
            "trigger.cleanup": "runner",
            "org.email.view": "operator",
            "org.email.manage": "admin",
            "org.license.view": "operator",
        }
        for permission, role in expected.items():
            assert PERMISSIONS[permission] == role, permission


class TestRegistryValidation:
    def test_all_values_are_known_org_roles(self) -> None:
        for permission, role in PERMISSIONS.items():
            assert role in ORG_ROLE_HIERARCHY, f"{permission} maps to unknown role '{role}'"

    def test_registry_non_empty(self) -> None:
        assert len(PERMISSIONS) > 0

    def test_adr_required_keys_present(self) -> None:
        required = {
            "pipeline.graph.update",
            "run.trigger",
            "connector.create",
            "connector.delete",
            "secret.manage",
            "trigger.events.list",
            "api_key.create",
            "api_key.update",
            "metrics.ingest",
            "oauth.client.create",
            "oauth.client.list",
            "trigger.cleanup",
            "org.email.view",
            "org.email.manage",
            "org.license.view",
        }
        assert required <= set(PERMISSIONS)

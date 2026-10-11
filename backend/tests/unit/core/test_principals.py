"""FAR-1385 slice 1 — principal-kind helpers.

Pins the two ADR-058 rules this module owns: the human-path predicate, and the
``ceiling <= operator`` clamp. The delegable-permission leg is deliberately NOT
re-listed here — :func:`service_permission_allowed` must delegate to FAR-1477's
registry flag, so these tests assert the delegation rather than a copy.
"""

from __future__ import annotations

import pytest

from modulo.auth.permissions import is_delegable
from modulo.core.principals import (
    KIND_HUMAN,
    KIND_SERVICE,
    SERVICE_ROLE_CEILING,
    account_kind,
    clamp_service_role,
    is_human_account,
    is_service_account,
    service_permission_allowed,
)


class _FakeAccount:
    def __init__(self, kind: str | None) -> None:
        self.kind = kind


class TestAccountKind:
    def test_human_and_service_are_classified(self) -> None:
        assert account_kind(_FakeAccount(KIND_HUMAN)) == KIND_HUMAN
        assert account_kind(_FakeAccount(KIND_SERVICE)) == KIND_SERVICE

    def test_unset_kind_defaults_to_human(self) -> None:
        assert account_kind(_FakeAccount(None)) == KIND_HUMAN

    def test_unrecognised_kind_classifies_as_human(self) -> None:
        # The DB CHECK rejects unknown values; classification is defensive.
        assert account_kind(_FakeAccount("robot")) == KIND_HUMAN

    def test_service_predicate(self) -> None:
        assert is_service_account(_FakeAccount(KIND_SERVICE)) is True
        assert is_service_account(_FakeAccount(KIND_HUMAN)) is False
        assert is_human_account(_FakeAccount(KIND_SERVICE)) is False
        assert is_human_account(_FakeAccount(None)) is True


class TestServicePermissionAllowed:
    """Consumes FAR-1477's registry flag — never a second exclusion list."""

    def test_delegates_to_is_delegable_for_every_registry_key(self) -> None:
        from modulo.auth.permissions import PERMISSIONS

        for key in PERMISSIONS:
            assert service_permission_allowed(key) == is_delegable(key), key

    @pytest.mark.parametrize("key", ["api_key.create", "oauth.client.create", "system.config.manage", "org.delete"])
    def test_credential_lifecycle_and_system_keys_are_denied(self, key: str) -> None:
        assert service_permission_allowed(key) is False

    def test_ordinary_operational_key_is_allowed(self) -> None:
        assert service_permission_allowed("pipeline.create") is True

    def test_unknown_key_denies_fail_closed(self) -> None:
        assert service_permission_allowed("not.a.real.key") is False


class TestServiceRoleCeiling:
    def test_ceiling_is_operator(self) -> None:
        assert SERVICE_ROLE_CEILING == "operator"

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("admin", "operator"),
            ("operator", "operator"),
            ("runner", "runner"),
            ("viewer", "viewer"),
        ],
    )
    def test_roles_are_clamped_to_operator(self, role: str, expected: str) -> None:
        assert clamp_service_role(role) == expected

    @pytest.mark.parametrize("role", [None, "", "owner", "superuser"])
    def test_unknown_or_absent_role_denies(self, role: str | None) -> None:
        assert not clamp_service_role(role)

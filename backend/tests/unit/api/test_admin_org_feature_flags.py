"""Router-level tests for org-admin feature-flag governance (FAR-2302).

An organisation admin reads and sets the feature flags that govern their OWN
organisation's usage through ``GET/PUT/DELETE
/api/v1/admin/org/feature-flags[/{flag_name}]``, without the instance-wide
``system.config.manage`` gate the system feature-flags page carries.

The classification is fail-closed: only community-tier flags whose
``FeatureFlag.org_admin_settable`` is True are listed and writable; every
team-tier flag and every unclassified/sensitive community flag is
system-administrator-only, and ``mcp_human_linked_hitl_authority`` is excluded
because it already has a dedicated org control.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.api.routes import admin_org_feature_flags as route_module
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.auth.permissions import PERMISSIONS, is_delegable
from modulo.core.feature_flags import FeatureFlag
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PATH = "/api/v1/admin/org/feature-flags"
_PERMISSION = "org.feature_flags.manage"

_SETTABLE = "webhook_notification_log"
_SETTABLE_DEFAULT_OFF = "dashboard_charts"
_NOT_SETTABLE = "sso_unrestricted_provisioning"
_TEAM_TIER = "sso"
_DEDICATED = "mcp_human_linked_hitl_authority"

_FLAGS: tuple[FeatureFlag, ...] = (
    FeatureFlag(
        name=_SETTABLE,
        description="Webhook notification delivery log",
        tier="community",
        currently_active=True,
        org_admin_settable=True,
    ),
    FeatureFlag(
        name=_SETTABLE_DEFAULT_OFF,
        description="Dashboard trend charts",
        tier="community",
        currently_active=False,
        org_admin_settable=True,
    ),
    FeatureFlag(
        name=_NOT_SETTABLE,
        description="SSO unrestricted provisioning",
        tier="community",
        currently_active=False,
    ),
    FeatureFlag(
        name=_TEAM_TIER,
        description="Single sign-on",
        tier="team",
        currently_active=False,
    ),
    FeatureFlag(
        # Deliberately classified settable in this fake registry to prove the
        # dedicated-control exclusion is independent of the flag classification.
        name=_DEDICATED,
        description="Human-linked MCP HITL authority",
        tier="community",
        currently_active=True,
        org_admin_settable=True,
    ),
)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _mock_registry() -> MagicMock:
    registry = MagicMock()
    registry.list_flags.return_value = list(_FLAGS)
    registry.get_flag.side_effect = lambda name: next((f for f in _FLAGS if f.name == name), None)
    return registry


def _client(*, role: str) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    # ``audited()`` resolves its principal through ``resolve_audit_principal`` →
    # ``get_current_user``, so both identity seams must be stubbed.
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="tenant",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    yield from _client(role="admin")


@pytest.fixture
def operator_client() -> Generator[TestClient, None, None]:
    yield from _client(role="operator")


def _org(overrides: dict[str, bool] | None = None) -> SimpleNamespace:
    settings_json = None if overrides is None else {"feature_overrides": overrides}
    return SimpleNamespace(settings_json=settings_json)


class TestPermissionRegistry:
    def test_key_resolves_to_admin(self) -> None:
        assert PERMISSIONS[_PERMISSION] == "admin"

    def test_key_is_not_delegable(self) -> None:
        """Org feature-flag governance must never ride a delegated credential."""
        assert is_delegable(_PERMISSION) is False

    def test_no_sibling_key_shares_the_namespace(self) -> None:
        """A near-miss key would silently resolve to a sibling's role (FAR-1955 class)."""
        overlaps = [key for key in PERMISSIONS if key.startswith("org.feature_flags")]
        assert overlaps == [_PERMISSION]


class TestListFlags:
    def test_lists_only_org_settable_community_flags(self, admin_client: TestClient) -> None:
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(route_module, "get_organisation", AsyncMock(return_value=_org())),
        ):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 200
        names = [item["name"] for item in resp.json()["flags"]]
        assert names == [_SETTABLE, _SETTABLE_DEFAULT_OFF]

    def test_reports_effective_state_and_override(self, admin_client: TestClient) -> None:
        registry = _mock_registry()
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=registry)),
            patch.object(
                route_module,
                "get_organisation",
                AsyncMock(return_value=_org({_SETTABLE_DEFAULT_OFF: True})),
            ),
        ):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 200
        rows = {item["name"]: item for item in resp.json()["flags"]}
        assert rows[_SETTABLE] == {
            "name": _SETTABLE,
            "description": "Webhook notification delivery log",
            "enabled": True,
            "overridden": False,
            "default": True,
        }
        assert rows[_SETTABLE_DEFAULT_OFF]["default"] is False
        assert rows[_SETTABLE_DEFAULT_OFF]["enabled"] is True
        assert rows[_SETTABLE_DEFAULT_OFF]["overridden"] is True

    def test_operator_is_denied(self, operator_client: TestClient) -> None:
        resp = operator_client.get(_PATH)
        assert resp.status_code == 403

    def test_missing_org_is_a_404(self, admin_client: TestClient) -> None:
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(route_module, "get_organisation", AsyncMock(return_value=None)),
        ):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 404


class TestSetFlag:
    def _env(self, *, org: Any, audit: AsyncMock) -> Any:
        return (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(route_module, "get_organisation", AsyncMock(return_value=org)),
            patch.object(route_module, "apply_org_settings_patch_or_404", AsyncMock(return_value={})),
            patch.object(route_module, "_invalidate_cache", AsyncMock()),
            patch(
                "modulo.api.routes.admin_feature_flags.append_audit_event_isolated",
                new=audit,
            ),
        )

    def test_set_writes_nested_override_audits_and_invalidates_cache(self, admin_client: TestClient) -> None:
        audit = AsyncMock()
        patch_patch, patch_org, patch_apply, patch_cache, patch_audit = self._env(org=_org(), audit=audit)
        with patch_patch, patch_org, patch_apply as apply_mock, patch_cache as cache_mock, patch_audit:
            resp = admin_client.put(f"{_PATH}/{_SETTABLE}", json={"enabled": False})

        assert resp.status_code == 200
        assert resp.json() == {
            "name": _SETTABLE,
            "description": "Webhook notification delivery log",
            "enabled": False,
            "overridden": True,
            "default": True,
        }
        assert apply_mock.await_args.kwargs["nested_patch"] == {"feature_overrides": {_SETTABLE: False}}
        cache_mock.assert_awaited_once()
        audit.assert_awaited_once()
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "feature_flag_override_set"
        assert kwargs["payload"] == {"flag_name": _SETTABLE, "enabled": False}

    def test_operator_cannot_write(self, operator_client: TestClient) -> None:
        resp = operator_client.put(f"{_PATH}/{_SETTABLE}", json={"enabled": True})
        assert resp.status_code == 403

    def test_unknown_flag_is_a_404(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())):
            resp = admin_client.put(f"{_PATH}/no_such_flag", json={"enabled": True})

        assert resp.status_code == 404

    def test_non_settable_community_flag_is_403(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())):
            resp = admin_client.put(f"{_PATH}/{_NOT_SETTABLE}", json={"enabled": True})

        assert resp.status_code == 403

    def test_team_tier_flag_is_403(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())):
            resp = admin_client.put(f"{_PATH}/{_TEAM_TIER}", json={"enabled": True})

        assert resp.status_code == 403

    def test_dedicated_control_flag_is_403_even_when_classified_settable(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())):
            resp = admin_client.put(f"{_PATH}/{_DEDICATED}", json={"enabled": False})

        assert resp.status_code == 403

    def test_a_refused_write_does_not_touch_the_org(self, admin_client: TestClient) -> None:
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(route_module, "apply_org_settings_patch_or_404", AsyncMock()) as apply_mock,
        ):
            resp = admin_client.put(f"{_PATH}/{_TEAM_TIER}", json={"enabled": True})

        assert resp.status_code == 403
        apply_mock.assert_not_awaited()


class TestClearFlag:
    def test_clear_removes_the_override_audits_and_invalidates_cache(self, admin_client: TestClient) -> None:
        audit = AsyncMock()
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(
                route_module,
                "get_organisation",
                AsyncMock(return_value=_org({_SETTABLE: False, "dashboard_charts": True})),
            ),
            patch.object(route_module, "apply_org_settings_patch_or_404", AsyncMock(return_value={})) as apply_mock,
            patch.object(route_module, "_invalidate_cache", AsyncMock()) as cache_mock,
            patch(
                "modulo.api.routes.admin_feature_flags.append_audit_event_isolated",
                new=audit,
            ),
        ):
            resp = admin_client.delete(f"{_PATH}/{_SETTABLE}")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
        assert resp.json()["overridden"] is False
        # Only the cleared flag is removed; a sibling override survives.
        assert apply_mock.await_args.kwargs["patch"] == {"feature_overrides": {"dashboard_charts": True}}
        cache_mock.assert_awaited_once()
        audit.assert_awaited_once()
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "feature_flag_override_cleared"
        assert kwargs["payload"] == {"flag_name": _SETTABLE}

    def test_missing_org_is_a_404(self, admin_client: TestClient) -> None:
        """A cleared override on a vanished org fails closed, never optimistic.

        ``get_organisation`` returning ``None`` must surface as a 404 inside the
        write transaction. Falling through to the patch helper would either
        report a bogus success or touch a row that does not exist, so the
        handler raises before the write (and ``audited()`` records the error).
        """
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(route_module, "get_organisation", AsyncMock(return_value=None)),
        ):
            resp = admin_client.delete(f"{_PATH}/{_SETTABLE}")

        assert resp.status_code == 404

    def test_unknown_flag_is_a_404(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())):
            resp = admin_client.delete(f"{_PATH}/no_such_flag")

        assert resp.status_code == 404

    def test_team_tier_flag_is_403(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())):
            resp = admin_client.delete(f"{_PATH}/{_TEAM_TIER}")

        assert resp.status_code == 403


def _programming_error() -> ProgrammingError:
    return ProgrammingError("SELECT 1", {}, Exception("missing column"))


class TestFailClosedReadErrors:
    """A failed read answers a typed status, never an empty/optimistic list."""

    def test_list_programming_error_answers_501(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(side_effect=_programming_error())):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 501
        assert resp.json()["detail"]

    def test_list_database_error_answers_503(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(side_effect=SQLAlchemyError("boom"))):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 503

    def test_list_org_read_error_answers_500(self, admin_client: TestClient) -> None:
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(route_module, "get_organisation", AsyncMock(side_effect=ValueError("boom"))),
        ):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 500

    def test_set_programming_error_answers_501(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(side_effect=_programming_error())):
            resp = admin_client.put(f"{_PATH}/{_SETTABLE}", json={"enabled": True})

        assert resp.status_code == 501

    def test_set_database_error_answers_503(self, admin_client: TestClient) -> None:
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(
                route_module,
                "apply_org_settings_patch_or_404",
                AsyncMock(side_effect=SQLAlchemyError("boom")),
            ),
        ):
            resp = admin_client.put(f"{_PATH}/{_SETTABLE}", json={"enabled": True})

        assert resp.status_code == 503

    def test_set_http_exception_from_patch_helper_propagates(self, admin_client: TestClient) -> None:
        with (
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(
                route_module,
                "apply_org_settings_patch_or_404",
                AsyncMock(side_effect=HTTPException(status_code=404, detail="Organisation not found.")),
            ),
        ):
            resp = admin_client.put(f"{_PATH}/{_SETTABLE}", json={"enabled": True})

        assert resp.status_code == 404

    def test_clear_programming_error_answers_501(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "_build_registry", AsyncMock(side_effect=_programming_error())):
            resp = admin_client.delete(f"{_PATH}/{_SETTABLE}")

        assert resp.status_code == 501


class TestCoarseAuditOutcome:
    """An audited mutation records the real outcome, never a false success.

    The route raises on failure (it does not return a failure-shaped response),
    so ``audited()`` teardown sees the exception and records ``outcome="error"``.
    Returning a 403/500 body instead would silently record a SUCCESS for a
    refused governance write — the FAR-1791 audit-inversion class.
    """

    def test_success_and_refusal_record_distinct_outcomes(self, admin_client: TestClient) -> None:
        recorded: list[dict[str, Any]] = []

        async def capture(_session: Any, _principal: Any, **kwargs: Any) -> None:
            recorded.append(kwargs["payload"])

        with (
            patch("modulo.core.audit_coverage.append_audit_event_isolated", new=capture),
            patch.object(route_module, "_build_registry", AsyncMock(return_value=_mock_registry())),
            patch.object(route_module, "apply_org_settings_patch_or_404", AsyncMock(return_value={})),
            patch.object(route_module, "_invalidate_cache", AsyncMock()),
            patch(
                "modulo.api.routes.admin_feature_flags.append_audit_event_isolated",
                new=AsyncMock(),
            ),
        ):
            ok = admin_client.put(f"{_PATH}/{_SETTABLE}", json={"enabled": True})
            refused = admin_client.put(f"{_PATH}/{_TEAM_TIER}", json={"enabled": True})

        assert ok.status_code == 200
        assert refused.status_code == 403
        assert [payload["outcome"] for payload in recorded] == ["success", "error"]

"""Router-level tests for the org-admin human-linked MCP HITL authority control (FAR-1972).

The reachability half of ADR 063 step 1: an org admin reads and sets their OWN
org's ``feature_overrides["mcp_human_linked_hitl_authority"]`` override through
``GET/PUT /api/v1/admin/org/mcp-human-linked-hitl-authority``, without the
instance-wide ``system.config.manage`` gate the feature-flags page carries.

These tests prove the permission is enforced (org admin yes, operator no), that
the write targets the exact key the existing ``resolve_flag`` org-override layer
reads, and that the override is reported back distinctly from the default.
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
from modulo.api.routes import admin_org_mcp_authority as route_module
from modulo.api.routes.admin_org_mcp_authority import MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.auth.permissions import PERMISSIONS, is_delegable
from modulo.core.feature_flags import feature_overrides_from_settings
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PATH = "/api/v1/admin/org/mcp-human-linked-hitl-authority"
_PERMISSION = "org.mcp.human_linked_hitl_authority.manage"


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


class TestPermissionRegistry:
    def test_key_resolves_to_admin(self) -> None:
        assert PERMISSIONS[_PERMISSION] == "admin"

    def test_key_is_not_delegable(self) -> None:
        """An org-level authz governance toggle must never ride a delegated credential."""
        assert is_delegable(_PERMISSION) is False

    def test_no_sibling_key_shares_the_namespace(self) -> None:
        """A near-miss key would silently resolve to a sibling's role (FAR-1955 class)."""
        overlaps = [key for key in PERMISSIONS if key.startswith("org.mcp")]
        assert overlaps == [_PERMISSION]


class TestOrgAdminReachability:
    def test_org_admin_can_read_the_setting(self, admin_client: TestClient) -> None:
        org = SimpleNamespace(settings_json=None)
        with patch.object(route_module, "get_organisation", AsyncMock(return_value=org)):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 200
        assert resp.json() == {"enabled": True, "overridden": False}

    def test_operator_is_denied(self, operator_client: TestClient) -> None:
        """A non-admin org member cannot read or change the governance toggle."""
        resp = operator_client.get(_PATH)
        assert resp.status_code == 403

    def test_operator_cannot_write(self, operator_client: TestClient) -> None:
        resp = operator_client.put(_PATH, json={"enabled": False})
        assert resp.status_code == 403

    def test_explicit_override_is_reported_as_overridden(self, admin_client: TestClient) -> None:
        org = SimpleNamespace(settings_json={"feature_overrides": {MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG: False}})
        with patch.object(route_module, "get_organisation", AsyncMock(return_value=org)):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "overridden": True}


class TestOrgAdminWrite:
    def test_write_targets_the_org_override_the_resolver_reads(self, admin_client: TestClient) -> None:
        """The PUT must write exactly the key the existing resolve_flag layer reads."""
        captured: dict[str, Any] = {}

        async def capture_patch(_session: Any, _org_id: Any, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            settings: dict[str, Any] = {}
            for key, sub in kwargs.get("nested_patch", {}).items():
                settings[key] = sub
            return settings

        with patch.object(route_module, "apply_org_settings_patch_or_404", capture_patch):
            resp = admin_client.put(_PATH, json={"enabled": False})

        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "overridden": True}
        assert captured["nested_patch"] == {"feature_overrides": {MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG: False}}

    def test_written_override_is_visible_to_the_flag_reader(self, admin_client: TestClient) -> None:
        """Compose the write with the real reader: the stored override must be honoured."""
        stored_settings: dict[str, Any] = {}

        async def capture_patch(_session: Any, _org_id: Any, **kwargs: Any) -> dict[str, Any]:
            nested = kwargs.get("nested_patch", {})
            stored_settings.update(nested)
            return stored_settings

        with patch.object(route_module, "apply_org_settings_patch_or_404", capture_patch):
            resp = admin_client.put(_PATH, json={"enabled": False})

        assert resp.status_code == 200
        assert feature_overrides_from_settings(stored_settings) == {MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG: False}

    def test_org_not_found_is_a_404(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "get_organisation", AsyncMock(return_value=None)):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 404


class TestDefaultResolution:
    def test_a_registered_flag_supplies_its_own_default(self, admin_client: TestClient) -> None:
        """Once FAR-1957 registers the flag, the registry - not the fallback - decides.

        The route's ``_default_enabled`` falls back to ON only while the flag is
        unregistered; a registered flag that is currently inactive must be
        reported as OFF, otherwise the read would contradict the registry the
        gate enforcement consults.
        """
        org = SimpleNamespace(settings_json=None)
        registry = MagicMock()
        registry.get_flag.return_value = SimpleNamespace(currently_active=False)
        with (
            patch.object(route_module, "get_organisation", AsyncMock(return_value=org)),
            patch.object(route_module, "get_registry", MagicMock(return_value=registry)),
        ):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "overridden": False}
        registry.get_flag.assert_called_once_with(MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG)


def _programming_error() -> ProgrammingError:
    return ProgrammingError("SELECT 1", {}, Exception("missing column"))


class TestReadErrorArms:
    """The read's local arms answer a typed status instead of a bare 500."""

    def test_programming_error_answers_501(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "get_organisation", AsyncMock(side_effect=_programming_error())):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 501
        assert resp.json()["error"]["code"] == "NOT_IMPLEMENTED"

    def test_database_error_answers_503(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "get_organisation", AsyncMock(side_effect=SQLAlchemyError("boom"))):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

    def test_unexpected_error_answers_500(self, admin_client: TestClient) -> None:
        with patch.object(route_module, "get_organisation", AsyncMock(side_effect=ValueError("boom"))):
            resp = admin_client.get(_PATH)

        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"


class TestWriteErrorArms:
    """The write keeps the shared status mapping instead of collapsing to 500."""

    def test_an_http_exception_from_the_patch_helper_propagates(self, admin_client: TestClient) -> None:
        with patch.object(
            route_module,
            "apply_org_settings_patch_or_404",
            AsyncMock(side_effect=HTTPException(status_code=404, detail="Organisation not found.")),
        ):
            resp = admin_client.put(_PATH, json={"enabled": False})

        assert resp.status_code == 404

    def test_programming_error_answers_501(self, admin_client: TestClient) -> None:
        with patch.object(
            route_module,
            "apply_org_settings_patch_or_404",
            AsyncMock(side_effect=_programming_error()),
        ):
            resp = admin_client.put(_PATH, json={"enabled": False})

        assert resp.status_code == 501
        assert resp.json()["error"]["code"] == "NOT_IMPLEMENTED"

    def test_database_error_answers_503(self, admin_client: TestClient) -> None:
        with patch.object(
            route_module,
            "apply_org_settings_patch_or_404",
            AsyncMock(side_effect=SQLAlchemyError("boom")),
        ):
            resp = admin_client.put(_PATH, json={"enabled": False})

        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

    def test_unexpected_error_answers_500(self, admin_client: TestClient) -> None:
        with patch.object(
            route_module,
            "apply_org_settings_patch_or_404",
            AsyncMock(side_effect=ValueError("boom")),
        ):
            resp = admin_client.put(_PATH, json={"enabled": False})

        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"

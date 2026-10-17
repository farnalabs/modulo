"""FAR-1930: behaviour-parity tests for the three system-admin-gated MCP tools.

The system-admin requirement is declared once in
``scope_validator.SYSTEM_ADMIN_TOOLS`` and enforced by the shared scope path
(``resolve_tool_access``, reached through the real ``_check_agent_tool_scope``
chokepoint). These tests drive the REAL handlers and assert the caller-facing
contract for each of the three tools:

* runner rejected
* operator rejected
* org-admin without the system-admin claim rejected
* system admin accepted

The two housekeeping tools also carry the admin org-role floor, so a runner or
operator is rejected by the ROLE leg (detail names the required role) while an
org-admin is rejected by the SYSTEM-ADMIN leg (detail says "system admin").
``list_feature_flags`` is pinned read-only at the role leg, so every
non-system-admin role is rejected by the system-admin leg.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import (
    list_feature_flags,
    list_housekeeping,
    perform_housekeeping,
)
from tests.unit.mcp.helpers import AuthContext, make_session_context

_SYS_ADMIN_DETAIL = "{tool} requires system admin role"


class _ParityAuthContext(AuthContext):
    """AuthContext that also normalises the system-admin ContextVar.

    ``AuthContext`` sets role/org/auth token; the system-admin claim is set
    per-test (explicitly False here) and always reset so a True cannot leak
    into a later test.
    """

    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_is_system_admin

        _ctx_is_system_admin.set(False)

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_is_system_admin

        _ctx_is_system_admin.set(False)
        super().teardown_method()


# ---------------------------------------------------------------------------
# list_feature_flags — read-only floor, system-admin leg is the discriminator
# ---------------------------------------------------------------------------


class TestListFeatureFlagsSystemAdminParity(_ParityAuthContext):
    @pytest.fixture(autouse=True)
    def _validate_auth(self) -> Iterator[None]:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            yield

    @pytest.mark.parametrize("role", ["runner", "operator"])
    async def test_low_role_rejected_by_system_admin_leg(self, role: str) -> None:
        # viewer..admin all clear the read-only role leg; the system-admin
        # requirement is the sole discriminator.
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set(role)
        result = await list_feature_flags()
        assert result["error"] == "insufficient_scope"
        assert result["detail"] == _SYS_ADMIN_DETAIL.format(tool="list_feature_flags")

    async def test_org_admin_without_system_admin_rejected(self) -> None:
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("admin")
        result = await list_feature_flags()
        assert result["error"] == "insufficient_scope"
        assert result["detail"] == _SYS_ADMIN_DETAIL.format(tool="list_feature_flags")

    async def test_system_admin_accepted(self) -> None:
        from modulo.api.mcp_server import _ctx_is_system_admin, _ctx_role

        org = MagicMock()
        org.settings_json = {}
        plan_ctx = MagicMock()
        plan_ctx.tier.return_value = "community"
        flag = MagicMock()
        flag.name = "schema_version_history"
        flag.description = "Snapshot diff + rollback"
        flag.tier = "team"
        flag.currently_active = False
        flag.depends_on = None
        registry = MagicMock()
        registry.current_tier = "community"
        registry.has_license_key = False
        registry.list_flags.return_value = [flag]
        registry.tier_gap_flags.return_value = []
        settings = MagicMock()
        settings.modulo_license_key = ""
        settings.modulo_dev_mode = False

        _ctx_role.set("admin")
        _ctx_is_system_admin.set(True)
        with (
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.db.crud.organisation.get_organisation", new=AsyncMock(return_value=org)),
            patch("modulo.core.feature_flags.resolve_plan_context", new=AsyncMock(return_value=plan_ctx)),
            patch(
                "modulo.core.feature_flags.FeatureFlagRegistry.from_db",
                new=AsyncMock(return_value=registry),
            ),
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.api.mcp_server.get_settings", return_value=settings),
        ):
            result = await list_feature_flags()

        assert "error" not in result
        assert result["flags"][0]["name"] == "schema_version_history"


# ---------------------------------------------------------------------------
# list_housekeeping — admin org-role floor AND system-admin claim
# ---------------------------------------------------------------------------


class TestListHousekeepingSystemAdminParity(_ParityAuthContext):
    @pytest.fixture(autouse=True)
    def _validate_auth(self) -> Iterator[None]:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            yield

    @pytest.mark.parametrize("role", ["runner", "operator"])
    async def test_low_role_rejected_by_org_role_floor(self, role: str) -> None:
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set(role)
        result = await list_housekeeping()
        assert result["error"] == "insufficient_scope"
        assert "requires 'admin' role" in result["detail"]

    async def test_org_admin_without_system_admin_rejected(self) -> None:
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("admin")
        result = await list_housekeeping()
        assert result["error"] == "insufficient_scope"
        assert result["detail"] == _SYS_ADMIN_DETAIL.format(tool="list_housekeeping")

    async def test_system_admin_accepted(self) -> None:
        from modulo.api.mcp_server import _ctx_is_system_admin, _ctx_role

        _ctx_role.set("admin")
        _ctx_is_system_admin.set(True)
        with (
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.core.housekeeping.scan_all", new=AsyncMock(return_value=[])),
        ):
            result = await list_housekeeping()

        assert result == {"categories": [], "total_count": 0}


# ---------------------------------------------------------------------------
# perform_housekeeping — admin org-role floor AND system-admin claim
# ---------------------------------------------------------------------------


class TestPerformHousekeepingSystemAdminParity(_ParityAuthContext):
    @pytest.fixture(autouse=True)
    def _validate_auth(self) -> Iterator[None]:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            yield

    @pytest.mark.parametrize("role", ["runner", "operator"])
    async def test_low_role_rejected_by_org_role_floor(self, role: str) -> None:
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set(role)
        result = await perform_housekeeping(items=[])
        assert result["error"] == "insufficient_scope"
        assert "requires 'admin' role" in result["detail"]

    async def test_org_admin_without_system_admin_rejected(self) -> None:
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("admin")
        result = await perform_housekeeping(items=[])
        assert result["error"] == "insufficient_scope"
        assert result["detail"] == _SYS_ADMIN_DETAIL.format(tool="perform_housekeeping")

    async def test_system_admin_accepted(self) -> None:
        from modulo.api.mcp_server import _ctx_is_system_admin, _ctx_role

        _ctx_role.set("admin")
        _ctx_is_system_admin.set(True)
        with patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())):
            result = await perform_housekeeping(items=[])

        assert result == {"deleted_count": 0, "errors": []}

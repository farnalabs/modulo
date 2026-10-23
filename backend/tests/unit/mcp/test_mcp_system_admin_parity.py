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

import logging
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import (
    get_org_config,
    list_feature_flags,
    list_housekeeping,
    perform_housekeeping,
)
from tests.unit.api.mock_session import make_session_scope
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
# get_org_config — FAR-1942: read-only floor, system-admin leg is the
# discriminator (same shape as ``list_feature_flags``; both mirror a REST route
# gated only by ``require_system_permission``).
# ---------------------------------------------------------------------------


class TestGetOrgConfigSystemAdminParity(_ParityAuthContext):
    @pytest.fixture(autouse=True)
    def _validate_auth(self) -> Iterator[None]:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            yield

    @pytest.mark.parametrize("role", ["viewer", "runner", "operator", "admin"])
    async def test_any_role_without_system_admin_rejected(self, role: str) -> None:
        # Every org role clears the read-only role leg; the system-admin
        # requirement is the sole discriminator — matching REST, whose
        # ``require_system_permission`` ignores the org role entirely.
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set(role)
        result = await get_org_config()
        assert result["error"] == "insufficient_scope"
        assert result["detail"] == _SYS_ADMIN_DETAIL.format(tool="get_org_config")

    async def test_system_admin_accepted(self) -> None:
        from modulo.api.mcp_server import _ctx_is_system_admin, _ctx_role

        _ctx_role.set("admin")
        _ctx_is_system_admin.set(True)
        with (
            patch("modulo.api.mcp_server._session", return_value=make_session_context(make_session_scope())),
            patch("modulo.db.crud.system_config.list_config", new=AsyncMock(return_value=[])),
        ):
            result = await get_org_config()

        assert result["count"] == 0
        assert "No configuration found" in result["results"]


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
            patch("modulo.api.mcp_server._session", return_value=make_session_context(make_session_scope())),
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
            patch("modulo.api.mcp_server._session", return_value=make_session_context(make_session_scope())),
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
        with patch("modulo.api.mcp_server._session", return_value=make_session_context(make_session_scope())):
            result = await perform_housekeeping(items=[])

        assert result == {"deleted_count": 0, "errors": []}


# ---------------------------------------------------------------------------
# FAR-1930 (finding C): exactly ONE canonical warning per system-admin denial
#
# Centralising the gate must not double the log volume. The shared resolver
# recognises the denial but stays silent; the MCP adapter emits the single
# call-site line carrying the tool name and the ``system_admin_required``
# reason. Before the fix the resolver's generic ``Scope check failed`` line
# AND the adapter line both fired (housekeeping 1 -> 2 warnings;
# ``list_feature_flags`` 0 -> 2).
# ---------------------------------------------------------------------------


class TestSystemAdminDenialLogging(_ParityAuthContext):
    @pytest.fixture(autouse=True)
    def _validate_auth(self) -> Iterator[None]:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            yield

    @pytest.mark.parametrize(
        "tool",
        ["get_org_config", "list_feature_flags", "list_housekeeping", "perform_housekeeping"],
    )
    async def test_denial_logs_exactly_one_canonical_line(self, tool: str, caplog: pytest.LogCaptureFixture) -> None:
        import modulo.api.mcp_server as ms

        ms._ctx_role.set("admin")
        with caplog.at_level(logging.WARNING):
            if tool == "get_org_config":
                result = await ms.get_org_config()
            elif tool == "list_feature_flags":
                result = await ms.list_feature_flags()
            elif tool == "list_housekeeping":
                result = await ms.list_housekeeping()
            else:
                result = await ms.perform_housekeeping(items=[])

        assert result["error"] == "insufficient_scope"
        assert result["detail"] == _SYS_ADMIN_DETAIL.format(tool=tool)
        messages = [record.getMessage() for record in caplog.records]
        assert sum("system_admin_required" in message for message in messages) == 1, messages
        assert not any(message.startswith("Scope check failed") for message in messages), messages
        assert any(tool in message for message in messages), messages


# ---------------------------------------------------------------------------
# FAR-1942: the REST twin and the MCP tool admit the SAME caller role.
# ---------------------------------------------------------------------------


def _rest_permission_tag(endpoint: object, parameter: str) -> object:
    """Return the ``_tagged_dep`` metadata attached to a route's parameter."""
    import typing

    hints = typing.get_type_hints(endpoint, include_extras=True)
    return typing.get_args(hints[parameter])[1]


def test_get_org_config_mcp_matches_its_rest_twin() -> None:
    """The REST route and MCP tool agree on the caller each admits.

    ``GET /api/v1/system-admin/config`` is gated by
    ``require_system_permission("system.config.manage")`` — a strict
    ``is_system_admin`` claim with no org-role fall-through. The MCP tool must
    therefore admit EXACTLY the system-admin caller, for every org role. A
    change to either surface (a different REST dependency, or dropping the
    claim from the MCP declaration) fails here, so they cannot drift again.
    """
    from modulo.api.routes.admin_system_config import admin_list_config
    from modulo.core.mcp.scope_validator import SYSTEM_ADMIN_TOOLS, resolve_tool_access

    rest_tag = _rest_permission_tag(admin_list_config, "_current_user")
    assert getattr(rest_tag, "permission_kind", None) == "system"
    assert getattr(rest_tag, "permission", None) == "system.config.manage"

    assert "get_org_config" in SYSTEM_ADMIN_TOOLS

    for role in ("viewer", "runner", "operator", "admin"):
        allowed_as_admin, _ = resolve_tool_access(
            tool="get_org_config",
            action=None,
            role=role,
            key_scope="org",
            auth_type="jwt",
            allowed_tools=None,
            kill_switch=True,
            is_system_admin=True,
        )
        allowed_without_claim, _ = resolve_tool_access(
            tool="get_org_config",
            action=None,
            role=role,
            key_scope="org",
            auth_type="jwt",
            allowed_tools=None,
            kill_switch=True,
            is_system_admin=False,
        )
        assert allowed_as_admin is True
        assert allowed_without_claim is False

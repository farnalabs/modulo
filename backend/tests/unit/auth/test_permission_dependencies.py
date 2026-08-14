"""Unit tests for the permission-based REST dependency adapters (ADR 017).

Each test drives the dependency's inner coroutine directly (the same style
``require_feature`` uses) and asserts the five variants behave as specified.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from modulo.api.dependencies import (
    require_permission,
    require_system_or_org_admin,
    require_system_permission,
    require_target_org_role,
)
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal

_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()
_ACCOUNT = uuid.uuid4()


def _tenant(org_role: str, *, is_system_admin: bool = False) -> TenantPrincipal:
    return TenantPrincipal(
        username="user@example.com",
        organisation_id=_ORG_B,
        account_id=_ACCOUNT,
        org_role=org_role,
        is_system_admin=is_system_admin,
    )


def _auth(is_system_admin: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="user@example.com",
        organisation_id=_ORG_B,
        account_id=_ACCOUNT,
        org_role="operator",
        is_system_admin=is_system_admin,
    )


class TestRequirePermission:
    """Tenant org-role variant (``permission_kind="tenant"``)."""

    def test_tags(self) -> None:
        dep = require_permission("pipeline.graph.update")
        assert dep.permission == "pipeline.graph.update"
        assert dep.permission_kind == "tenant"

    def test_unknown_permission_fails_fast_at_factory(self) -> None:
        from modulo.auth.permissions import PermissionConfigurationError

        with pytest.raises(PermissionConfigurationError):
            require_permission("nonexistent.permission")

    @pytest.mark.asyncio
    async def test_operator_allowed(self) -> None:
        dep = require_permission("pipeline.graph.update")
        result = await dep.dependency(principal=_tenant("operator"), session=_make_session(role="operator"))
        assert result.org_role == "operator"

    @pytest.mark.asyncio
    async def test_admin_allowed(self) -> None:
        dep = require_permission("pipeline.graph.update")
        result = await dep.dependency(principal=_tenant("admin"), session=_make_session(role="admin"))
        assert result.org_role == "admin"

    @pytest.mark.asyncio
    async def test_viewer_denied(self) -> None:
        dep = require_permission("pipeline.graph.update")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_session(role="viewer"))
        assert excinfo.value.status_code == 403
        assert "pipeline.graph.update" in excinfo.value.detail
        assert "operator" in excinfo.value.detail


class TestRequireSystemPermission:
    """Strict is_system_admin-only variant (``permission_kind="system"``)."""

    def test_tags(self) -> None:
        dep = require_system_permission("org.email.manage")
        assert dep.permission == "org.email.manage"
        assert dep.permission_kind == "system"

    @pytest.mark.asyncio
    async def test_system_admin_allowed(self) -> None:
        dep = require_system_permission("org.email.manage")
        result = await dep.dependency(current_user=_auth(is_system_admin=True))
        assert result.is_system_admin is True

    @pytest.mark.asyncio
    async def test_org_admin_denied_no_fall_through(self) -> None:
        dep = require_system_permission("org.email.manage")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(current_user=_auth(is_system_admin=False))
        assert excinfo.value.status_code == 403


class TestRequireSystemOrOrgAdmin:
    """The one true hybrid (``permission_kind="system_or_org"``)."""

    def test_tags(self) -> None:
        dep = require_system_or_org_admin("org.delete")
        assert dep.permission == "org.delete"
        assert dep.permission_kind == "system_or_org"

    @pytest.mark.asyncio
    async def test_org_admin_allowed(self) -> None:
        dep = require_system_or_org_admin("org.delete")
        result = await dep.dependency(principal=_tenant("admin"))
        assert result.org_role == "admin"

    @pytest.mark.asyncio
    async def test_system_admin_allowed(self) -> None:
        dep = require_system_or_org_admin("org.delete")
        result = await dep.dependency(principal=_tenant("operator", is_system_admin=True))
        assert result.is_system_admin is True

    @pytest.mark.asyncio
    async def test_operator_denied(self) -> None:
        dep = require_system_or_org_admin("org.delete")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("operator"))
        assert excinfo.value.status_code == 403


class TestRequireTargetOrgRoleReads:
    """Scoped-hybrid reads: ``org.email.view``/``org.license.view`` @ operator."""

    def test_tags(self) -> None:
        dep = require_target_org_role("org.email.view", "operator")
        assert dep.permission == "org.email.view"
        assert dep.permission_kind == "scoped_hybrid"
        assert dep.min_role == "operator"

    @pytest.mark.asyncio
    async def test_target_org_member_at_operator_allowed(self) -> None:
        dep = require_target_org_role("org.email.view", "operator")
        session = _make_session(role="operator")

        outcome = await dep.dependency(_request(org_id=_ORG_A), current_user=_auth(), session=session)
        assert outcome.account_id == _ACCOUNT

    @pytest.mark.asyncio
    async def test_cross_org_member_at_operator_allowed(self) -> None:
        """A member of B operating with current-org B gains access to org A at operator."""
        dep = require_target_org_role("org.email.view", "operator")
        session = _make_session(role="operator")

        outcome = await dep.dependency(_request(org_id=_ORG_A), current_user=_auth(), session=session)
        assert outcome.account_id == _ACCOUNT

    @pytest.mark.asyncio
    async def test_non_member_denied(self) -> None:
        dep = require_target_org_role("org.email.view", "operator")
        session = _make_session(role=None)

        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(_request(org_id=_ORG_A), current_user=_auth(), session=session)
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_target_member_below_min_denied(self) -> None:
        dep = require_target_org_role("org.email.view", "operator")
        session = _make_session(role="runner")

        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(_request(org_id=_ORG_A), current_user=_auth(), session=session)
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_system_admin_allowed_without_membership(self) -> None:
        dep = require_target_org_role("org.email.view", "operator")
        session = AsyncMock()
        outcome = await dep.dependency(
            _request(org_id=_ORG_A),
            current_user=_auth(is_system_admin=True),
            session=session,
        )
        assert outcome.is_system_admin is True
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_org_id_path_param_denied(self) -> None:
        dep = require_target_org_role("org.email.view", "operator")
        request = MagicMock()
        request.path_params = {}
        session = AsyncMock()
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(request, current_user=_auth(), session=session)
        assert excinfo.value.status_code == 400


class TestRequireTargetOrgRoleMutations:
    """Scoped-hybrid mutations: ``org.email.manage`` @ admin."""

    def test_tags(self) -> None:
        dep = require_target_org_role("org.email.manage", "admin")
        assert dep.permission == "org.email.manage"
        assert dep.permission_kind == "scoped_hybrid"
        assert dep.min_role == "admin"

    @pytest.mark.asyncio
    async def test_target_org_admin_allowed(self) -> None:
        dep = require_target_org_role("org.email.manage", "admin")
        session = _make_session(role="admin")

        outcome = await dep.dependency(_request(org_id=_ORG_A), current_user=_auth(), session=session)
        assert outcome.account_id == _ACCOUNT

    @pytest.mark.asyncio
    async def test_target_org_operator_denied(self) -> None:
        dep = require_target_org_role("org.email.manage", "admin")
        session = _make_session(role="operator")

        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(_request(org_id=_ORG_A), current_user=_auth(), session=session)
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_min_role_is_configuration_error(self) -> None:
        from modulo.auth.permissions import PermissionConfigurationError

        with pytest.raises(PermissionConfigurationError):
            require_target_org_role("org.email.manage", "superadmin")

    @pytest.mark.asyncio
    async def test_min_role_mismatch_with_registry_is_configuration_error(self) -> None:
        from modulo.auth.permissions import PermissionConfigurationError

        with pytest.raises(PermissionConfigurationError):
            require_target_org_role("org.email.manage", "operator")


class TestRequireTargetOrgRolePauseKillSwitch:
    """``org.triggers.pause.manage`` uses ``kill_switch_eligible=False`` — the
    authz kill-switch must never lift the pause toggle gate."""

    def test_pause_tags(self) -> None:
        dep = require_target_org_role("org.triggers.pause.manage", "admin", kill_switch_eligible=False)
        assert dep.permission == "org.triggers.pause.manage"
        assert dep.permission_kind == "scoped_hybrid"
        assert dep.min_role == "admin"

    @pytest.mark.asyncio
    async def test_pause_route_viewer_denied_when_kill_switch_off(self) -> None:
        dep = require_target_org_role("org.triggers.pause.manage", "admin", kill_switch_eligible=False)
        session = _make_session_with_enforce(role="viewer", enforce=False)

        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(_request(org_id=_ORG_A), current_user=_auth(), session=session)
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_pause_route_org_admin_allowed(self) -> None:
        dep = require_target_org_role("org.triggers.pause.manage", "admin", kill_switch_eligible=False)
        session = _make_session_with_enforce(role="admin", enforce=False)

        outcome = await dep.dependency(_request(org_id=_ORG_A), current_user=_auth(), session=session)
        assert outcome.account_id == _ACCOUNT

    @pytest.mark.asyncio
    async def test_pause_route_system_admin_allowed(self) -> None:
        dep = require_target_org_role("org.triggers.pause.manage", "admin", kill_switch_eligible=False)
        session = _make_session(role="viewer")

        outcome = await dep.dependency(
            _request(org_id=_ORG_A),
            current_user=_auth(is_system_admin=True),
            session=session,
        )
        assert outcome.account_id == _ACCOUNT


class TestRequireInDevOperator:
    """The In-Dev reveal gate: ``*.list.in_dev`` / ``library.search.in_dev`` @ operator.

    Fail-closed and kill-switch-immune (``kill_switch_eligible=False``): a
    viewer/runner must never reveal ADR 010-hidden In-Dev items, even in orgs
    with authz enforcement disabled.
    """

    @pytest.mark.parametrize(
        "permission",
        ["connector.list.in_dev", "model_backend.list.in_dev", "library.search.in_dev"],
    )
    def test_operator_allowed(self, permission: str) -> None:
        from modulo.api.dependencies import require_in_dev_operator

        assert require_in_dev_operator(_tenant("operator"), permission) is None

    @pytest.mark.parametrize(
        "permission",
        ["connector.list.in_dev", "model_backend.list.in_dev", "library.search.in_dev"],
    )
    def test_admin_allowed(self, permission: str) -> None:
        from modulo.api.dependencies import require_in_dev_operator

        assert require_in_dev_operator(_tenant("admin"), permission) is None

    @pytest.mark.parametrize(
        "permission",
        ["connector.list.in_dev", "model_backend.list.in_dev", "library.search.in_dev"],
    )
    def test_viewer_denied(self, permission: str) -> None:
        from modulo.api.dependencies import require_in_dev_operator

        with pytest.raises(HTTPException) as excinfo:
            require_in_dev_operator(_tenant("viewer"), permission)
        assert excinfo.value.status_code == 403
        assert permission in excinfo.value.detail
        assert "operator" in excinfo.value.detail

    @pytest.mark.parametrize(
        "permission",
        ["connector.list.in_dev", "model_backend.list.in_dev", "library.search.in_dev"],
    )
    def test_runner_denied(self, permission: str) -> None:
        from modulo.api.dependencies import require_in_dev_operator

        with pytest.raises(HTTPException) as excinfo:
            require_in_dev_operator(_tenant("runner"), permission)
        assert excinfo.value.status_code == 403
        assert permission in excinfo.value.detail


def _request(org_id: uuid.UUID) -> MagicMock:
    request = MagicMock()
    request.path_params = {"org_id": str(org_id)}
    return request


def _make_session(*, role: str | None) -> AsyncMock:
    """Return a session whose live-role lookup resolves to ``role``.

    ``session.begin`` must be a plain ``MagicMock`` returning a context-manager
    mock — ``async with session.begin():`` does not work when ``begin`` itself
    is an async mock (calling it yields an unawaited coroutine).
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    result = MagicMock()
    result.scalar_one_or_none.return_value = role
    session.execute.return_value = result
    return session


def _make_session_with_enforce(*, role: str | None, enforce: bool) -> AsyncMock:
    """Session for scoped-hybrid deps: first execute returns the LIVE role,
    second returns the authz_enforce read (so kill-switch-off scenarios can be
    tested with a real role string)."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    call_count = 0

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = role if call_count == 1 else enforce
        return result

    session.execute = _execute
    return session

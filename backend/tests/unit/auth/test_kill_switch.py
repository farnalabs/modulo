"""Unit tests for the tenancy-bounded authorization kill switch (ADR 017 DECISION 3).

Covers ``resolve_authz_enforce``, the request-scoped ``_authz_enforce_ctx``
fail-open gate in ``assert_org_role``, the REST ``require_permission`` lift,
tenancy-bounded isolation, the destructive-mutation carve-out, fail-closed
behaviour on read error, and per-request ContextVar reset.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import require_permission, require_system_or_org_admin
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.permissions import (
    PermissionDenied,
    _authz_enforce_ctx,
    assert_org_role,
    reset_authz_enforce,
    set_authz_enforce,
)
from modulo.db.settings_resolver import resolve_authz_enforce

_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()
_ACCOUNT = uuid.uuid4()


def _tenant(org_role: str, *, org_id: uuid.UUID = _ORG_B) -> TenantPrincipal:
    return TenantPrincipal(
        username="user@example.com",
        organisation_id=org_id,
        account_id=_ACCOUNT,
        org_role=org_role,
        is_system_admin=False,
    )


def _make_authz_session(value: object) -> MagicMock:
    """Session whose single ``authz_enforce`` read resolves to ``value``.

    ``begin`` is a plain ``MagicMock`` returning a context-manager mock so
    ``async with session.begin():`` works for the REST dependency.
    """
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def _execute(stmt, *args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        return result

    session.execute = _execute
    return session


def _make_error_session() -> MagicMock:
    """Session whose flag read raises ``SQLAlchemyError`` (fail-closed path)."""
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def _execute(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("kill-switch read failed")

    session.execute = _execute
    return session


def _make_branching_session(values: dict[uuid.UUID, object]) -> MagicMock:
    """Session whose flag read branches on the org id in the WHERE clause.

    Enables the two-orgs-one-process tenancy test: org A flipped off, org B on.
    """

    def _extract_org_id(stmt: object) -> object:
        whereclause = getattr(stmt, "whereclause", None)
        right = getattr(whereclause, "right", None)
        return getattr(right, "value", right)

    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = values.get(_extract_org_id(stmt))
        return result

    session.execute = _execute
    return session


@pytest.fixture(autouse=True)
def _reset_authz_ctx() -> None:
    """Guarantee no kill-switch ContextVar leaks between tests."""
    _authz_enforce_ctx.set(None)
    yield
    _authz_enforce_ctx.set(None)


class TestResolveAuthzEnforce:
    @pytest.mark.asyncio
    async def test_returns_org_value(self) -> None:
        assert await resolve_authz_enforce(_make_authz_session(False), _ORG_A) is False

    @pytest.mark.asyncio
    async def test_true_value_returned(self) -> None:
        assert await resolve_authz_enforce(_make_authz_session(True), _ORG_A) is True

    @pytest.mark.asyncio
    async def test_defaults_to_true_when_row_absent(self) -> None:
        assert await resolve_authz_enforce(_make_authz_session(None), _ORG_A) is True

    @pytest.mark.asyncio
    async def test_defaults_to_true_when_column_falsy_none(self) -> None:
        # A NULL column value is treated as "no explicit kill switch" → enforce.
        assert await resolve_authz_enforce(_make_authz_session(None), _ORG_A) is True

    @pytest.mark.asyncio
    async def test_defaults_to_true_when_org_id_none(self) -> None:
        assert await resolve_authz_enforce(_make_authz_session(False), None) is True

    @pytest.mark.asyncio
    async def test_fails_closed_on_sqlalchemy_error(self) -> None:
        assert await resolve_authz_enforce(_make_error_session(), _ORG_A) is True

    @pytest.mark.asyncio
    async def test_tenancy_bounded_two_orgs(self) -> None:
        """Org A flipped off, org B stays on — one process, two orgs."""
        session = _make_branching_session({_ORG_A: False, _ORG_B: True})
        assert await resolve_authz_enforce(session, _ORG_A) is False
        assert await resolve_authz_enforce(session, _ORG_B) is True


class TestAssertOrgRoleKillSwitch:
    def test_fail_open_when_context_false(self) -> None:
        token = set_authz_enforce(False)
        try:
            result = assert_org_role("viewer", "operator", "test.permission")
            assert result is None
        finally:
            reset_authz_enforce(token)

    def test_enforces_when_context_none(self) -> None:
        with pytest.raises(PermissionDenied):
            assert_org_role("viewer", "operator", "test.permission")

    def test_enforces_when_context_true(self) -> None:
        token = set_authz_enforce(True)
        try:
            with pytest.raises(PermissionDenied):
                assert_org_role("viewer", "operator", "test.permission")
        finally:
            reset_authz_enforce(token)

    def test_unknown_role_still_denied_with_context_false(self) -> None:
        token = set_authz_enforce(False)
        try:
            with pytest.raises(PermissionDenied):
                assert_org_role(None, "operator", "test.permission")
        finally:
            reset_authz_enforce(token)

    def test_kill_switch_immune_for_destructive_gate(self) -> None:
        token = set_authz_enforce(False)
        try:
            with pytest.raises(PermissionDenied):
                assert_org_role(
                    "viewer",
                    "operator",
                    "test.permission",
                    kill_switch_eligible=False,
                )
        finally:
            reset_authz_enforce(token)


class TestRequirePermissionKillSwitch:
    @pytest.mark.asyncio
    async def test_enforce_false_lifts_403_for_viewer(self) -> None:
        dep = require_permission("pipeline.graph.update")
        result = await dep.dependency(principal=_tenant("viewer"), session=_make_authz_session(False))
        assert result.org_role == "viewer"

    @pytest.mark.asyncio
    async def test_enforce_true_restores_403_for_viewer(self) -> None:
        dep = require_permission("pipeline.graph.update")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_authz_session(True))
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_tenancy_bounded_org_a_flip_does_not_affect_org_b(self) -> None:
        dep = require_permission("pipeline.graph.update")
        session = _make_branching_session({_ORG_A: False, _ORG_B: True})
        result = await dep.dependency(principal=_tenant("viewer", org_id=_ORG_A), session=session)
        assert result.org_role == "viewer"
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer", org_id=_ORG_B), session=session)
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_flag_read_error_defaults_to_enforce(self) -> None:
        dep = require_permission("pipeline.graph.update")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_error_session())
        assert excinfo.value.status_code == 403


class TestSystemOrOrgAdminKillSwitch:
    @pytest.mark.asyncio
    async def test_org_delete_not_lifted_by_enforce_false(self) -> None:
        """Destructive mutations are NEVER lifted by the kill switch."""
        dep = require_system_or_org_admin("org.delete")
        token = set_authz_enforce(False)
        try:
            with pytest.raises(HTTPException) as excinfo:
                await dep.dependency(principal=_tenant("viewer"))
            assert excinfo.value.status_code == 403
        finally:
            reset_authz_enforce(token)

    @pytest.mark.asyncio
    async def test_org_admin_still_allowed_with_enforce_false(self) -> None:
        dep = require_system_or_org_admin("org.delete")
        token = set_authz_enforce(False)
        try:
            result = await dep.dependency(principal=_tenant("admin"))
            assert result.org_role == "admin"
        finally:
            reset_authz_enforce(token)


class TestContextVarReset:
    @pytest.mark.asyncio
    async def test_contextvar_reset_after_request_no_leak(self) -> None:
        dep = require_permission("pipeline.graph.update")
        result = await dep.dependency(principal=_tenant("viewer"), session=_make_authz_session(False))
        assert result.org_role == "viewer"
        assert _authz_enforce_ctx.get() is None

    @pytest.mark.asyncio
    async def test_second_request_without_flag_reverts_to_enforce(self) -> None:
        """No stale value leaks into a subsequent request that sets nothing."""
        dep = require_permission("pipeline.graph.update")
        await dep.dependency(principal=_tenant("viewer"), session=_make_authz_session(False))
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_authz_session(True))
        assert excinfo.value.status_code == 403

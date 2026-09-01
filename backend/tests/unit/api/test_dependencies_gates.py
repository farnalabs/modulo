"""QA lens pass on ``modulo.api.dependencies`` — the API-key credential gate,
the feature-availability gate, and the dependency-tagging contract.

``require_permission_any_credential`` is the CI/CD credential path (PRD §5.2):
it accepts both user JWTs and ``mk_`` org API keys and applies the same
org-role floor and tenancy-bounded authz kill switch as ``require_permission``.
The lens review found one divergence: the kill-switch read was not wrapped in
the fail-closed ``SQLAlchemyError`` handler that ``require_permission`` has
(ADR 017 DECISION 3 — a DB blip must not fail-open the gate). A ``begin()``
failure there escaped as an unhandled 500 instead of degrading to ENFORCE.
This suite locks the fail-closed read, the kill-switch lift, the
runner/operator/viewer role floor, the per-request ContextVar reset, and the
``permission_kind="tenant_or_api_key"`` introspection tags — plus the
``require_feature`` 402 gate used across admin routes.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import (
    require_feature,
    require_permission_any_credential,
)
from modulo.api.models.problem import ProblemException, ProblemType
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.permissions import PermissionConfigurationError, _authz_enforce_ctx

_ORG = uuid.uuid4()
_ACCOUNT = uuid.uuid4()


def _tenant(org_role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username="ci@example.com",
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role=org_role,
    )


def _make_session(*, enforce: bool | None = None) -> MagicMock:
    """Session whose ``authz_enforce`` read resolves to ``enforce`` (None -> True)."""
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = enforce
        return result

    session.execute = _execute
    return session


def _make_raising_read_session() -> MagicMock:
    """Session whose flag read raises ``SQLAlchemyError`` (internal fail-closed
    path inside ``resolve_authz_enforce``)."""
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        raise SQLAlchemyError("kill-switch read failed")

    session.execute = _execute
    return session


def _make_failing_begin_session() -> MagicMock:
    """Session whose ``begin()`` itself raises — the path that escapes
    ``resolve_authz_enforce``'s internal catch and must fail closed."""
    session = MagicMock()
    session.begin = MagicMock(side_effect=SQLAlchemyError("begin failed"))
    return session


@pytest.fixture(autouse=True)
def _reset_authz_ctx() -> None:
    """Guarantee no kill-switch ContextVar leaks between tests."""
    _authz_enforce_ctx.set(None)
    yield
    _authz_enforce_ctx.set(None)


class TestRequirePermissionAnyCredential:
    """JWT + API-key org-role gate (``permission_kind="tenant_or_api_key"``)."""

    def test_tags(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        assert dep.permission == "run.trigger"
        assert dep.permission_kind == "tenant_or_api_key"

    def test_unknown_permission_fails_fast_at_factory(self) -> None:
        with pytest.raises(PermissionConfigurationError):
            require_permission_any_credential("nonexistent.permission")

    async def test_runner_allowed(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        result = await dep.dependency(principal=_tenant("runner"), session=_make_session())
        assert result.org_role == "runner"

    async def test_operator_allowed(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        result = await dep.dependency(principal=_tenant("operator"), session=_make_session())
        assert result.org_role == "operator"

    async def test_viewer_denied(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_session())
        assert excinfo.value.status_code == 403
        assert "run.trigger" in excinfo.value.detail
        assert "runner" in excinfo.value.detail

    async def test_missing_role_read_defaults_to_enforce(self) -> None:
        """Row absent (None read) => enforce stays True; viewer is still denied."""
        dep = require_permission_any_credential("run.trigger")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_session(enforce=None))
        assert excinfo.value.status_code == 403

    async def test_enforce_false_lifts_403_for_viewer(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        result = await dep.dependency(principal=_tenant("viewer"), session=_make_session(enforce=False))
        assert result.org_role == "viewer"

    async def test_enforce_true_restores_403_for_viewer(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_session(enforce=True))
        assert excinfo.value.status_code == 403

    async def test_flag_read_error_fails_closed_to_enforce(self) -> None:
        """A DB blip on the ``authz_enforce`` read must NOT fail-open the gate."""
        dep = require_permission_any_credential("run.trigger")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_raising_read_session())
        assert excinfo.value.status_code == 403

    async def test_begin_error_fails_closed_to_enforce(self) -> None:
        """``begin()`` failure escapes ``resolve_authz_enforce``'s internal
        catch; the dependency must still degrade to ENFORCE (403, never an
        unhandled 500)."""
        dep = require_permission_any_credential("run.trigger")
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=_tenant("viewer"), session=_make_failing_begin_session())
        assert excinfo.value.status_code == 403

    async def test_begin_error_operator_still_allowed(self) -> None:
        """Fail-closed to ENFORCE keeps the allowed floor intact for a
        sufficiently privileged caller."""
        dep = require_permission_any_credential("run.trigger")
        result = await dep.dependency(principal=_tenant("operator"), session=_make_failing_begin_session())
        assert result.org_role == "operator"

    async def test_denied_request_resets_kill_switch_contextvar(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        with pytest.raises(HTTPException):
            await dep.dependency(principal=_tenant("viewer"), session=_make_session(enforce=True))
        assert _authz_enforce_ctx.get() is None

    async def test_allowed_request_resets_kill_switch_contextvar(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        await dep.dependency(principal=_tenant("runner"), session=_make_session(enforce=False))
        assert _authz_enforce_ctx.get() is None


class TestRequireFeature:
    """Feature-availability gate -> 402 ``ProblemException``."""

    async def test_enabled_feature_returns_none(self) -> None:
        dep = require_feature("team_rbac")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = True
        assert await dep.dependency(ctx=ctx) is None

    async def test_disabled_feature_raises_402_with_instance(self) -> None:
        dep = require_feature("team_rbac")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = False
        with pytest.raises(ProblemException) as excinfo:
            await dep.dependency(ctx=ctx)
        exc = excinfo.value
        assert exc.status_code == 402
        assert exc.problem.status == 402
        assert exc.problem.type == f"urn:problem:modulo:{ProblemType.FEATURE_REQUIRED.value}"
        assert exc.problem.instance == "team_rbac"
        assert "team_rbac" in exc.problem.detail

    async def test_disabled_feature_uses_feature_name_as_instance(self) -> None:
        dep = require_feature("sso")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = False
        with pytest.raises(ProblemException) as excinfo:
            await dep.dependency(ctx=ctx)
        assert excinfo.value.problem.instance == "sso"

    async def test_feature_enabled_checked_by_name(self) -> None:
        dep = require_feature("audit_viewer")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = True
        await dep.dependency(ctx=ctx)
        ctx.feature_enabled.assert_called_once_with("audit_viewer")


class TestSystemEngineIsFallback:
    """The degraded-system-engine predicate must be correct regardless of
    call ordering: the fallback flag is only set as a side effect of
    ``get_or_create_system_engine``, so a first-caller in the degraded state
    would read a stale False if the predicate read the bare module global.
    The predicate now initialises the (idempotent, lock-guarded) singleton
    before reading. Both tests patch the settings name the factory resolves
    (``get_settings`` is lru_cached) to pin the provisioned/un-provisioned
    scenario explicitly."""

    def test_true_when_system_url_unset_and_no_prior_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modulo.api import dependencies as deps

        class _UnprovisionedSettings:
            modulo_system_database_url = ""

        monkeypatch.setattr(deps, "get_settings", lambda: _UnprovisionedSettings())
        saved_engine = deps._SYSTEM_ASYNC_ENGINE
        saved_flag = deps._SYSTEM_ENGINE_IS_FALLBACK
        deps._SYSTEM_ASYNC_ENGINE = None
        deps._SYSTEM_ENGINE_IS_FALLBACK = False
        try:
            assert deps.system_engine_is_fallback() is True
        finally:
            deps._SYSTEM_ASYNC_ENGINE = saved_engine
            deps._SYSTEM_ENGINE_IS_FALLBACK = saved_flag

    def test_false_when_system_url_set_and_no_prior_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modulo.api import dependencies as deps

        class _ProvisionedSettings:
            modulo_system_database_url = "postgresql+asyncpg://localhost/system-test"

        monkeypatch.setattr(deps, "get_settings", lambda: _ProvisionedSettings())
        saved_engine = deps._SYSTEM_ASYNC_ENGINE
        saved_flag = deps._SYSTEM_ENGINE_IS_FALLBACK
        deps._SYSTEM_ASYNC_ENGINE = None
        deps._SYSTEM_ENGINE_IS_FALLBACK = False
        try:
            assert deps.system_engine_is_fallback() is False
        finally:
            deps._SYSTEM_ASYNC_ENGINE = saved_engine
            deps._SYSTEM_ENGINE_IS_FALLBACK = saved_flag

"""QA lens pass on ``modulo.api.dependencies`` — the API-key credential gate,
the feature-availability gate, and the dependency-tagging contract.

``require_permission_any_credential`` is the CI/CD credential path (PRD §5.2):
it accepts both user JWTs and ``mk_`` org API keys and applies the same
org-role floor and tenancy-bounded authz kill switch as ``require_permission``.
The lens review found one divergence: the kill-switch read was not wrapped in
the fail-closed ``SQLAlchemyError`` handler that ``require_permission`` has
(ADR 047 DECISION 3 — a DB blip must not fail-open the gate). A ``begin()``
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


def _signed_team_licence(*, org_id: str = "tenant-org", expires_days: int = 30) -> tuple[str, str]:
    """Return ``(licence_key, public_key)`` for a throwaway Ed25519 keypair."""
    import base64
    import json
    from datetime import UTC, datetime, timedelta

    from modulo.core.registry.crypto import generate_keypair, sign_primitive

    kp = generate_keypair()
    payload = {
        "tier": "team",
        "features": ["sso"],
        "expires_at": (datetime.now(UTC) + timedelta(days=expires_days)).isoformat(),
        "org_id": org_id,
    }
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sign_primitive(payload, kp["private_key"]))).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}", kp["public_key"]


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

    async def test_org_less_principal_skips_kill_switch_read(self) -> None:
        """A principal with no organisation (organisation_id=None) must skip
        the kill-switch read entirely, assert the org role directly, and reset
        without touching the authz ContextVar."""
        dep = require_permission_any_credential("run.trigger")
        org_less = TenantPrincipal(
            username="anon@example.com",
            organisation_id=None,
            account_id=_ACCOUNT,
            org_role="operator",
        )
        result = await dep.dependency(principal=org_less, session=_make_failing_begin_session())
        assert result.org_role == "operator"
        assert _authz_enforce_ctx.get() is None

    async def test_org_less_principal_viewer_still_denied(self) -> None:
        dep = require_permission_any_credential("run.trigger")
        org_less = TenantPrincipal(
            username="anon@example.com",
            organisation_id=None,
            account_id=_ACCOUNT,
            org_role="viewer",
        )
        with pytest.raises(HTTPException) as excinfo:
            await dep.dependency(principal=org_less, session=_make_failing_begin_session())
        assert excinfo.value.status_code == 403
        assert _authz_enforce_ctx.get() is None

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

    def test_enabled_feature_returns_none(self) -> None:
        dep = require_feature("team_rbac")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = True
        assert dep.dependency(ctx=ctx) is None

    def test_disabled_feature_raises_402_with_instance(self) -> None:
        dep = require_feature("team_rbac")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = False
        with pytest.raises(ProblemException) as excinfo:
            dep.dependency(ctx=ctx)
        exc = excinfo.value
        assert exc.status_code == 402
        assert exc.problem.status == 402
        assert exc.problem.type == f"urn:problem:modulo:{ProblemType.FEATURE_REQUIRED.value}"
        assert exc.problem.instance == "team_rbac"
        assert "team_rbac" in exc.problem.detail

    def test_disabled_feature_uses_feature_name_as_instance(self) -> None:
        dep = require_feature("sso")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = False
        with pytest.raises(ProblemException) as excinfo:
            dep.dependency(ctx=ctx)
        assert excinfo.value.problem.instance == "sso"

    def test_feature_enabled_checked_by_name(self) -> None:
        dep = require_feature("audit_viewer")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = True
        dep.dependency(ctx=ctx)
        ctx.feature_enabled.assert_called_once_with("audit_viewer")


class TestRequireFeatureAnonymous:
    """Pre-auth gate: fail-closed by default, degrade-only for authentication.

    FAR-2059: ``allow_degraded_authentication`` opts an AUTHENTICATION entry
    point out of the licence lockout when NO valid licence resolves. It must not
    become a blanket bypass: without the opt-in, or when a licence DOES resolve
    but lacks the feature, the gate still returns 402 (FAR-2060).
    """

    @staticmethod
    def _check(dep: object, *, feature_enabled: bool, has_license_key: bool):
        ctx = MagicMock()
        ctx.feature_enabled.return_value = feature_enabled
        ctx.has_license_key.return_value = has_license_key
        return dep.dependency(ctx=ctx)  # type: ignore[attr-defined]

    def test_enabled_feature_passes(self) -> None:
        from modulo.api.dependencies import require_feature_anonymous

        dep = require_feature_anonymous("sso", allow_degraded_authentication=True)
        assert self._check(dep, feature_enabled=True, has_license_key=False) is None

    def test_unlicensed_authentication_entry_point_degrades(self, caplog: pytest.LogCaptureFixture) -> None:
        """No valid licence at any scope -> the gate lets authentication through."""
        from modulo.api.dependencies import require_feature_anonymous

        dep = require_feature_anonymous("sso", allow_degraded_authentication=True)
        with caplog.at_level("WARNING", logger="modulo.api.dependencies"):
            assert self._check(dep, feature_enabled=False, has_license_key=False) is None
        assert any("anonymous_degraded_authentication" in rec.message for rec in caplog.records)

    def test_unlicensed_without_opt_in_still_402(self) -> None:
        """The default gate is NOT weakened — no opt-in means no degrade."""
        from modulo.api.dependencies import require_feature_anonymous

        dep = require_feature_anonymous("sso")
        with pytest.raises(ProblemException) as excinfo:
            self._check(dep, feature_enabled=False, has_license_key=False)
        assert excinfo.value.status_code == 402

    def test_resolved_licence_without_feature_still_402_names_scope(self) -> None:
        """A determinate downgrade refuses even on an authentication entry point."""
        from modulo.api.dependencies import require_feature_anonymous

        dep = require_feature_anonymous("sso", allow_degraded_authentication=True)
        with pytest.raises(ProblemException) as excinfo:
            self._check(dep, feature_enabled=False, has_license_key=True)
        exc = excinfo.value
        assert exc.status_code == 402
        assert "system or organisation scope" in exc.problem.detail

    def test_authenticated_gate_names_scope_too(self) -> None:
        """FAR-2060: the operator-facing 402 says which licence scope works."""
        dep = require_feature("sso")
        ctx = MagicMock()
        ctx.feature_enabled.return_value = False
        with pytest.raises(ProblemException) as excinfo:
            dep.dependency(ctx=ctx)
        assert "system or organisation scope" in excinfo.value.problem.detail


class TestAnonymousPlanTenantScope:
    """FAR-2060: the anonymous resolver binds the trusted tenant, not system scope only."""

    @staticmethod
    def _session(rows: list[object] | None = None) -> MagicMock:
        """Session whose login-tenant read returns ``rows`` (the only query
        ``resolve_login_tenant`` issues)."""
        session = MagicMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows or []
        session.execute = AsyncMock(return_value=result)
        return session

    async def test_binds_the_single_login_active_org(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modulo.api import dependencies as deps
        from modulo.core import feature_flags as ff

        org = MagicMock()
        captured: dict[str, object] = {}

        async def _fake_resolve(settings: object, session: object, org: object = None) -> object:
            captured["org"] = org
            return ff.CommunityTier()

        monkeypatch.setattr(ff, "resolve_plan_context", _fake_resolve)
        await deps.resolve_anonymous_plan_context(MagicMock(), self._session([org]))
        assert captured["org"] is org

    async def test_zero_or_multi_org_falls_back_to_system_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modulo.api import dependencies as deps
        from modulo.core import feature_flags as ff

        captured: dict[str, object] = {}

        async def _fake_resolve(settings: object, session: object, org: object = None) -> object:
            captured["org"] = org
            return ff.CommunityTier()

        monkeypatch.setattr(ff, "resolve_plan_context", _fake_resolve)
        await deps.resolve_anonymous_plan_context(MagicMock(), self._session([MagicMock(), MagicMock()]))
        assert captured["org"] is None

    async def test_single_org_with_no_identified_tenant_fails_closed(self) -> None:
        """A single-org instance with zero rows must NOT drop to system scope."""
        from fastapi import HTTPException

        from modulo.api import dependencies as deps

        settings = MagicMock()
        settings.modulo_multi_org_enabled = False
        with pytest.raises(HTTPException) as excinfo:
            await deps.resolve_anonymous_plan_context(settings, self._session([]))
        assert excinfo.value.status_code == 503

    async def test_org_scoped_team_key_enables_sso_for_the_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The paying customer's org-level key is what the anonymous gate now sees."""
        from modulo.api import dependencies as deps
        from modulo.core import feature_flags as ff
        from modulo.core.license import _LICENSE_PUBLIC_KEY_HEX, clear_license, set_public_key

        key, public_key = _signed_team_licence()
        set_public_key(public_key)
        clear_license()

        org = MagicMock()
        org.id = "tenant-org"  # matches the signed key's org_id claim (FAR-2060)
        org.settings_json = {"license_key": key}
        captured: dict[str, object] = {}

        async def _real_plan(settings: object, session: object, org: object = None) -> object:
            captured["org"] = org
            data = ff.resolve_effective_license(settings, org)
            assert data is not None
            return ff.LicenseKeyTier(data)

        monkeypatch.setattr(ff, "resolve_plan_context", _real_plan)
        try:
            ctx = await deps.resolve_anonymous_plan_context(MagicMock(), self._session([org]))
        finally:
            set_public_key(_LICENSE_PUBLIC_KEY_HEX)

        assert captured["org"] is org
        assert ctx.has_license_key() is True
        assert ctx.feature_enabled("sso") is True

    def test_org_scoped_key_for_another_org_is_not_applied(self) -> None:
        """FAR-2060: a key whose ``org_id`` claim names a different org must not
        license this tenant — the claim is load-bearing at resolution time."""
        from modulo.core import feature_flags as ff
        from modulo.core.license import _LICENSE_PUBLIC_KEY_HEX, clear_license, set_public_key

        key, public_key = _signed_team_licence(org_id="some-other-tenant")
        set_public_key(public_key)
        clear_license()
        org = MagicMock()
        org.id = "tenant-org"
        org.settings_json = {"license_key": key}
        settings = MagicMock()
        settings.modulo_license_key = ""
        try:
            assert ff.resolve_effective_license(settings, org) is None
        finally:
            set_public_key(_LICENSE_PUBLIC_KEY_HEX)

    async def test_system_level_licence_still_works_without_a_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-2060 acceptance: the system-scope key must keep enabling SSO.

        With no single login-active org the ladder runs at system scope; the
        env-var key rung is verified and resolves the plan.
        """
        from modulo.api import dependencies as deps
        from modulo.core import feature_flags as ff
        from modulo.core.license import _LICENSE_PUBLIC_KEY_HEX, clear_license, set_public_key

        key, public_key = _signed_team_licence()
        set_public_key(public_key)
        clear_license()
        settings = MagicMock()
        settings.modulo_license_key = key

        async def _real_plan(_settings: object, session: object, org: object = None) -> object:
            assert org is None
            data = ff.resolve_effective_license(settings, None)
            assert data is not None
            return ff.LicenseKeyTier(data)

        monkeypatch.setattr(ff, "resolve_plan_context", _real_plan)
        try:
            ctx = await deps.resolve_anonymous_plan_context(settings, self._session([]))
        finally:
            set_public_key(_LICENSE_PUBLIC_KEY_HEX)
            clear_license()

        assert ctx.has_license_key() is True
        assert ctx.feature_enabled("sso") is True


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
            db_pool_recycle_seconds = 1500

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

    def test_sqlite_system_url_builds_without_asyncpg_connect_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A SQLite system URL must NOT go through the asyncpg ssl translation
        (FAR-1440: split_postgres_sslmode rejects non-Postgres schemes, and the
        aiosqlite driver has no ssl/statement_cache_size connect args)."""
        from unittest.mock import patch

        import sqlalchemy.ext.asyncio as sa_asyncio

        from modulo.api import dependencies as deps

        class _SqliteSettings:
            modulo_system_database_url = "sqlite+aiosqlite:///system-test.db"
            db_pool_recycle_seconds = 1500

        monkeypatch.setattr(deps, "get_settings", lambda: _SqliteSettings())
        saved_engine = deps._SYSTEM_ASYNC_ENGINE
        saved_flag = deps._SYSTEM_ENGINE_IS_FALLBACK
        deps._SYSTEM_ASYNC_ENGINE = None
        deps._SYSTEM_ENGINE_IS_FALLBACK = False
        try:
            with patch.object(sa_asyncio, "create_async_engine") as mock_create:
                assert deps.system_engine_is_fallback() is False
            kw = mock_create.call_args.kwargs
            assert mock_create.call_args.args[0] == "sqlite+aiosqlite:///system-test.db"
            assert kw["connect_args"] == {"timeout": 10, "command_timeout": deps._SYSTEM_DB_COMMAND_TIMEOUT_SECONDS}
            assert "ssl" not in kw["connect_args"]
            assert "statement_cache_size" not in kw["connect_args"]
            assert deps._SYSTEM_ASYNC_ENGINE is mock_create.return_value
        finally:
            deps._SYSTEM_ASYNC_ENGINE = saved_engine
            deps._SYSTEM_ENGINE_IS_FALLBACK = saved_flag


class TestSystemEnginePoolRecycle:
    """FAR-1524: the dedicated system engine must take its pool_recycle from
    ``Settings.db_pool_recycle_seconds`` (default 1500 s) and stay strictly
    below the Fly HAProxy 30-minute session window (1800 s) — never the old
    hardcoded 3600 s."""

    _HAPROXY_IDLE_WINDOW_SECONDS = 1800

    def test_recycle_comes_from_settings_and_sits_below_the_proxy_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import patch

        import sqlalchemy.ext.asyncio as sa_asyncio

        from modulo.api import dependencies as deps

        class _ProvisionedSettings:
            modulo_system_database_url = "postgresql+asyncpg://localhost/system-test"
            db_pool_recycle_seconds = 1500

        monkeypatch.setattr(deps, "get_settings", lambda: _ProvisionedSettings())
        saved_engine = deps._SYSTEM_ASYNC_ENGINE
        saved_flag = deps._SYSTEM_ENGINE_IS_FALLBACK
        deps._SYSTEM_ASYNC_ENGINE = None
        deps._SYSTEM_ENGINE_IS_FALLBACK = False
        try:
            with patch.object(sa_asyncio, "create_async_engine") as mock_create:
                deps.get_or_create_system_engine()
            kw = mock_create.call_args.kwargs
            assert kw["pool_recycle"] == 1500
            assert kw["pool_recycle"] < self._HAPROXY_IDLE_WINDOW_SECONDS
            assert kw["pool_pre_ping"] is True
        finally:
            deps._SYSTEM_ASYNC_ENGINE = saved_engine
            deps._SYSTEM_ENGINE_IS_FALLBACK = saved_flag

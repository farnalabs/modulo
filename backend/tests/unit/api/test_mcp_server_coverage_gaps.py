"""FAR-574 addendum: unit tests closing remaining coverage gaps in api/mcp_server.py.

Targets error paths and uncovered branches of the MCP tool surface that the
existing mcp test files do not reach: per-tool exception envelopes (repo
convention: ProgrammingError -> migration_required, SQLAlchemyError ->
db-unavailable, IntegrityError -> conflict, Exception -> internal_error),
validation branches, live re-validation helpers, run-scoped key scope
resolution, trigger update helpers, and the MCP resources. Unit tier: no DB,
no Docker, no live model backends — all persistence is mocked.
"""

import asyncio
import base64
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jwt import InvalidTokenError as JWTError
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

import modulo.api.mcp_server as ms
from modulo.api.mcp_server import (
    _analytics_deep_link,
    _append_mcp_hitl_denial_audit,
    _assert_failure_behaviour,
    _assert_pass_threshold,
    _authenticate_api_key,
    _authenticate_oauth_jwt,
    _clamp_mcp_number,
    _create_manual_run,
    _create_trigger_impl,
    _detect_masked_fields,
    _dispatch_hitl_action,
    _dispatch_unauth_paths,
    _evict_stale_live_role_cache,
    _extract_bearer_token,
    _extract_oauth_refresh_credentials,
    _format_breakdown_line,
    _get_doc_index,
    _hitl_required_team_name,
    _load_hitl_run,
    _node_allowed_tools_for_key,
    _oauth_authorize,
    _oauth_refresh,
    _oauth_refresh_impl,
    _oauth_token,
    _oauth_token_impl,
    _paginate_trigger_events,
    _parse_api_key_team_id,
    _parse_basic_auth_header,
    _revalidate_live_role,
    _review_hitl_impl,
    _run_status_node,
    _sanitize_cost_breakdown,
    _sanitize_cost_breakdown_entry,
    _sanitize_mcp_basis_value,
    _sanitize_mcp_mapping,
    _sanitize_mcp_sequence,
    _set_authz_enforce,
    _trigger_pipeline_impl,
    _validate_api_key_live,
    _validate_cron_update,
    _validate_oauth_live,
    _validate_ongoing_config_change,
    _validate_ongoing_trigger_update,
    _validate_principal_live,
    bind_connector_to_node,
    build_mcp_asgi_app,
    cancel_run,
    copy_library_primitive,
    create_agent,
    create_api_key,
    create_connector,
    create_eval_definition,
    create_model_backend,
    create_pipeline,
    create_schema,
    create_secret,
    create_trigger,
    delete_connector,
    delete_eval_definition,
    delete_pipeline,
    delete_secret,
    delete_trigger,
    get_api_key_role_cap_count,
    get_available_features,
    get_integration_status,
    get_org_config,
    get_pipeline_graph_tool,
    get_run_evals,
    get_run_output,
    get_trigger,
    infer_schema,
    list_api_keys,
    list_eval_definitions,
    list_housekeeping,
    list_pipelines_tool,
    list_runs,
    list_schemas,
    list_secrets,
    list_trigger_events,
    perform_housekeeping,
    resource_hitl_gate,
    resource_model_backends,
    resource_pipeline_snapshot_detail,
    resource_pipeline_snapshots,
    resource_run,
    resource_schemas,
    revoke_api_key,
    search_documentation,
    search_library,
    set_org_triggers_paused,
    update_eval_definition,
    update_pipeline_graph,
    update_trigger,
    validate_current_auth,
    validate_payload,
)
from modulo.auth.oauth import InvalidGrantError, OAuthAccessTokenClaims
from modulo.core.analytics.builder import (
    AnalyticsGroupBy,
    AnalyticsStatus,
    AnalyticsTriggerType,
)
from modulo.core.analytics.service import AnalyticsParams
from modulo.core.exceptions import OrgDeletedError, SnapshotLockNotAvailableError
from modulo.core.mcp.scope_validator import MCPAuthorizationError
from modulo.db.capacity import StorageExhaustedError

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    return session


def _make_execute_result(scalar_one_or_none: Any = None, scalars: Any = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar_one_or_none
    if scalars is not None:
        result.scalars.return_value = scalars
    return result


def _mock_factory(session: AsyncMock) -> MagicMock:
    factory = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    factory.return_value = cm
    return factory


class _AuthContext:
    """Set/teardown the MCP ContextVars and neutralise the tool-scope chokepoint.

    Scope rules are covered by their own dedicated suites; these tests target
    error envelopes and uncovered branches, so ``check_tool_scope`` is stubbed
    to a no-op and ``_session`` yields a mock session, keeping scope denials
    and real DB connections out of the asserted failure modes. Individual
    tests can still re-patch either with ``patch.object``.
    """

    def setup_method(self) -> None:
        ms._ctx_org_id.set(_ORG_ID)
        ms._ctx_role.set("operator")
        ms._ctx_user_id.set(_USER_ID)
        ms._ctx_key_id.set(_USER_ID)
        ms._ctx_auth_token.set(_API_KEY)
        ms._ctx_auth_type.set("api_key")
        ms._ctx_team_id.set(None)
        ms._ctx_node_allowed_tools.set(None)
        self._scope_patcher = patch.object(ms, "check_tool_scope", MagicMock())
        self._scope_patcher.start()
        self._session_patcher = patch.object(ms, "_session", return_value=_make_session_context(_mock_session()))
        self._session_patcher.start()

    def teardown_method(self) -> None:
        self._session_patcher.stop()
        self._scope_patcher.stop()
        for var in (
            ms._ctx_org_id,
            ms._ctx_role,
            ms._ctx_user_id,
            ms._ctx_key_id,
            ms._ctx_auth_token,
            ms._ctx_auth_type,
            ms._ctx_team_id,
        ):
            var.set(None)
        ms._ctx_node_allowed_tools.set(None)


class _AdminContext(_AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        ms._ctx_role.set("admin")


# ---------------------------------------------------------------------------
# Pure sanitizer helpers
# ---------------------------------------------------------------------------


class TestMcpSanitizerHelpers:
    def test_clamp_number_rejects_non_numeric(self) -> None:
        assert _clamp_mcp_number("not-a-number") == 0.0  # type: ignore[arg-type]

    def test_clamp_number_clamps_hostile_magnitude(self) -> None:
        assert _clamp_mcp_number(1e300) == 1e6

    def test_clamp_number_passthrough(self) -> None:
        assert _clamp_mcp_number(1.5) == 1.5

    def test_basis_value_dispatches_every_type(self) -> None:
        assert _sanitize_mcp_basis_value("x" * 300) == "x" * 256
        assert _sanitize_mcp_basis_value(True) is True
        assert _sanitize_mcp_basis_value(1e300) == 1e6
        assert _sanitize_mcp_basis_value({"a": 1}) == {"a": 1.0}
        assert _sanitize_mcp_basis_value([1, "b"]) == [1.0, "b"]
        assert _sanitize_mcp_basis_value(None) is None
        sentinel = object()
        assert isinstance(_sanitize_mcp_basis_value(sentinel), str)

    def test_mapping_and_sequence_wrappers(self) -> None:
        assert _sanitize_mcp_mapping({"a": "x" * 300}) == {"a": "x" * 256}
        assert _sanitize_mcp_sequence(["x" * 300]) == ["x" * 256]

    def test_cost_breakdown_rejects_non_list_and_skips_non_dict(self) -> None:
        assert not _sanitize_cost_breakdown("nope")
        sanitized = _sanitize_cost_breakdown(["not-a-dict", {"component": "llm", "amount_usd": 0.5}])
        assert len(sanitized) == 1
        assert sanitized[0]["component"] == "llm"

    def test_cost_breakdown_entry_filters_and_coerces(self) -> None:
        entry = {
            "unknown_key": "dropped",
            "basis": {"raw_reported": 1e300},
            "missing_self_report": True,
            "amount_usd": None,
            "display_name": 12345,
            "source": _NOW,
        }
        out = _sanitize_cost_breakdown_entry(entry)
        assert "unknown_key" not in out
        assert out["basis"] == {"raw_reported": 1e6}
        assert out["missing_self_report"] is True
        assert out["amount_usd"] is None
        assert out["display_name"] == 12345.0
        assert out["source"] == str(_NOW)

    def test_format_breakdown_line_branches(self) -> None:
        fallback_name = _format_breakdown_line({"amount_usd": 1.5})
        assert fallback_name.startswith("- component")
        rate_only = _format_breakdown_line({"component": "c", "rate_usd": 0.25, "source": "derived"})
        assert "$0.25" in rate_only
        assert "derived" in rate_only
        not_reported = _format_breakdown_line({"component": "c", "missing_self_report": True})
        assert "(not reported)" in not_reported
        errored = _format_breakdown_line({"component": "c", "error": "bad math"})
        assert "(bad math)" in errored


class TestRoleCapCounter:
    def test_record_and_read_role_cap_count(self) -> None:
        before = get_api_key_role_cap_count()
        ms._record_api_key_role_cap(
            minted_role="operator",
            effective_role="runner",
            org_id=_ORG_ID,
            degraded=True,
            key_id=_USER_ID,
        )
        assert get_api_key_role_cap_count() == before + 1


# ---------------------------------------------------------------------------
# Live-role revalidation helpers
# ---------------------------------------------------------------------------


class TestLiveRoleHelpers(_AuthContext):
    async def test_set_authz_enforce_resolves_flag(self) -> None:
        session = _mock_session()
        with (
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "resolve_authz_enforce", new=AsyncMock(return_value=False)),
            patch.object(ms, "set_authz_enforce") as mock_set,
        ):
            await _set_authz_enforce(_ORG_ID)
        mock_set.assert_called_once_with(False)

    async def test_set_authz_enforce_fails_closed_on_error(self) -> None:
        with (
            patch.object(ms, "_session", side_effect=RuntimeError("db down")),
            patch.object(ms, "set_authz_enforce") as mock_set,
        ):
            await _set_authz_enforce(_ORG_ID)
        mock_set.assert_called_once_with(True)

    def test_evict_stale_live_role_cache(self) -> None:
        now = 10_000.0
        ms._live_role_cache.clear()
        try:
            for i in range(ms._MAX_LIVE_ROLE_CACHE + 1):
                ms._live_role_cache[f"stale-{i}"] = (now - 1_000.0, "admin")
            _evict_stale_live_role_cache(now)
            assert not ms._live_role_cache

            for i in range(ms._MAX_LIVE_ROLE_CACHE + 2):
                ms._live_role_cache[f"fresh-{i}"] = (now, "admin")
            _evict_stale_live_role_cache(now)
            assert len(ms._live_role_cache) < ms._MAX_LIVE_ROLE_CACHE
        finally:
            ms._live_role_cache.clear()

    async def test_revalidate_live_role_cache_hit(self) -> None:
        ms._live_role_cache.clear()
        try:
            ms._live_role_cache[_API_KEY] = (float("inf"), "admin")
            with patch.object(ms, "_session") as mock_session:
                role = await _revalidate_live_role(_API_KEY, _USER_ID, _ORG_ID)
            assert role == "admin"
            mock_session.assert_not_called()
        finally:
            ms._live_role_cache.clear()

    async def test_revalidate_live_role_db_error_returns_none(self) -> None:
        ms._live_role_cache.clear()
        try:
            with patch.object(ms, "_session", side_effect=SQLAlchemyError("down")):
                role = await _revalidate_live_role(_API_KEY, _USER_ID, _ORG_ID)
            assert role is None
        finally:
            ms._live_role_cache.clear()

    async def test_validate_current_auth_swallows_unexpected_error(self) -> None:
        with patch.object(ms, "_validate_api_key_live", side_effect=RuntimeError("boom")):
            assert await validate_current_auth() is False

    def test_extract_bearer_token_rejects_non_bearer(self) -> None:
        request = MagicMock()
        request.headers = {"Authorization": "Basic abc"}
        token, err = _extract_bearer_token(request)
        assert token is None
        assert err is not None
        assert err.status_code == 401

    async def test_dispatch_unauth_paths_healthz(self) -> None:
        request = MagicMock()
        request.url.path = "/mcp/healthz"
        sentinel = MagicMock()
        call_next = AsyncMock(return_value=sentinel)
        assert await _dispatch_unauth_paths(request, call_next) is sentinel


class TestValidateApiKeyLive(_AuthContext):
    def _key(self, role: str = "operator") -> MagicMock:
        key = MagicMock()
        key.role = role
        key.id = _USER_ID
        key.team_id = None
        key.account_id = _USER_ID
        return key

    def _patches(self, live_role: str | None) -> tuple[Any, Any, Any]:
        return (
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "validate_api_key", new=AsyncMock(return_value=self._key())),
            patch.object(ms, "_revalidate_live_role", new=AsyncMock(return_value=live_role)),
        )

    async def test_no_user_context_denies(self) -> None:
        ms._ctx_user_id.set(None)
        session_patch, key_patch, live_patch = self._patches("admin")
        with session_patch, key_patch, live_patch:
            assert await _validate_api_key_live(_API_KEY, _ORG_ID) is False

    async def test_removed_owner_kills_key(self) -> None:
        session_patch, key_patch, live_patch = self._patches(None)
        with session_patch, key_patch, live_patch:
            assert await _validate_api_key_live(_API_KEY, _ORG_ID) is False

    async def test_unknown_live_role_kills_key(self) -> None:
        session_patch, key_patch, live_patch = self._patches("bogus-role")
        with session_patch, key_patch, live_patch:
            assert await _validate_api_key_live(_API_KEY, _ORG_ID) is False

    async def test_demoted_key_degrades(self) -> None:
        session_patch, key_patch, live_patch = self._patches("runner")
        with session_patch, key_patch, live_patch:
            assert await _validate_api_key_live(_API_KEY, _ORG_ID) is True
        assert ms._ctx_role.get() == "runner"


class TestValidatePrincipalLive(_AuthContext):
    def _principal(self, org_id: uuid.UUID | None = _ORG_ID) -> MagicMock:
        principal = MagicMock()
        principal.organisation_id = org_id
        principal.account_id = _USER_ID
        return principal

    async def test_no_org_denies(self) -> None:
        assert await _validate_principal_live(_API_KEY, self._principal(org_id=None)) is False

    async def test_missing_membership_denies(self) -> None:
        with patch.object(ms, "_revalidate_live_role", new=AsyncMock(return_value=None)):
            assert await _validate_principal_live(_API_KEY, self._principal()) is False

    async def test_live_role_applied(self) -> None:
        with patch.object(ms, "_revalidate_live_role", new=AsyncMock(return_value="admin")):
            assert await _validate_principal_live(_API_KEY, self._principal()) is True
        assert ms._ctx_role.get() == "admin"
        assert ms._ctx_team_id.get() is None


class TestValidateOauthLive(_AuthContext):
    def _claims(self) -> OAuthAccessTokenClaims:
        return OAuthAccessTokenClaims(
            client_id="client-1",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            scopes=["trigger:run"],
            token_family="fam",
            token_sequence=1,
        )

    async def test_regular_jwt_fallback_success(self) -> None:
        principal = MagicMock()
        principal.organisation_id = _ORG_ID
        principal.account_id = _USER_ID
        with (
            patch.object(ms, "decode_oauth_access_token", side_effect=JWTError("not oauth")),
            patch.object(ms, "get_settings", return_value=MagicMock(secret_key="k")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(return_value=principal)),
            patch.object(ms, "_revalidate_live_role", new=AsyncMock(return_value="admin")),
        ):
            assert await _validate_oauth_live(_API_KEY) is True
        assert ms._ctx_role.get() == "admin"

    async def test_regular_jwt_fallback_failure(self) -> None:
        with (
            patch.object(ms, "decode_oauth_access_token", side_effect=JWTError("not oauth")),
            patch.object(ms, "get_settings", return_value=MagicMock(secret_key="k")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(side_effect=JWTError("bad"))),
        ):
            assert await _validate_oauth_live(_API_KEY) is False

    async def test_valid_family_with_dead_membership_denies(self) -> None:
        session = _mock_session()
        with (
            patch.object(ms, "decode_oauth_access_token", new=MagicMock(return_value=self._claims())),
            patch.object(ms, "get_settings", return_value=MagicMock(secret_key="k")),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "check_oauth_token_family_valid", new=AsyncMock(return_value=True)),
            patch.object(ms, "_revalidate_live_role", new=AsyncMock(return_value=None)),
        ):
            assert await _validate_oauth_live(_API_KEY) is False


# ---------------------------------------------------------------------------
# Middleware authentication paths
# ---------------------------------------------------------------------------


def _mock_request() -> MagicMock:
    request = MagicMock()
    request.scope = {}
    return request


def _mock_api_key(role: str = "operator", run_id: uuid.UUID | None = None) -> MagicMock:
    key = MagicMock()
    key.id = _USER_ID
    key.role = role
    key.organisation_id = _ORG_ID
    key.account_id = _USER_ID
    key.team_id = None
    key.run_id = run_id
    key.name = None
    return key


class TestAuthenticateApiKey:
    @patch("modulo.db.rls._ensure_active_transaction", new=AsyncMock(return_value="postgresql"))
    @patch("modulo.api.mcp_server._node_allowed_tools_for_key", new=AsyncMock(return_value=None))
    async def test_postgres_lookup_and_success(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=_ORG_ID)
        with (
            patch.object(ms, "_get_session_factory", return_value=_mock_factory(session)),
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "validate_api_key", new=AsyncMock(return_value=_mock_api_key())),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="operator")),
        ):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is True
        assert err is None
        assert ms._ctx_org_id.get() == _ORG_ID

    @patch("modulo.db.rls._ensure_active_transaction", new=AsyncMock(return_value="sqlite"))
    async def test_unknown_key_returns_401(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=None)
        with patch.object(ms, "_get_session_factory", return_value=_mock_factory(session)):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is False
        assert err is not None
        assert err.status_code == 401

    @patch("modulo.db.rls._ensure_active_transaction", new=AsyncMock(return_value="postgresql"))
    async def test_dead_membership_denies_key(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=_ORG_ID)
        with (
            patch.object(ms, "_get_session_factory", return_value=_mock_factory(session)),
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "validate_api_key", new=AsyncMock(return_value=_mock_api_key())),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value=None)),
        ):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is False
        assert err is not None
        assert err.status_code == 401

    @patch("modulo.db.rls._ensure_active_transaction", new=AsyncMock(return_value="postgresql"))
    @patch("modulo.api.mcp_server._node_allowed_tools_for_key", new=AsyncMock(return_value=None))
    async def test_demoted_key_degrades_and_succeeds(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=_ORG_ID)
        with (
            patch.object(ms, "_get_session_factory", return_value=_mock_factory(session)),
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "validate_api_key", new=AsyncMock(return_value=_mock_api_key("operator"))),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="runner")),
        ):
            handled, _err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is True
        assert ms._ctx_role.get() == "runner"

    async def test_db_unavailable_returns_503(self) -> None:
        with (
            patch.object(ms, "_get_session_factory", side_effect=SQLAlchemyError("down")),
        ):
            handled, err = await _authenticate_api_key(_mock_request(), _API_KEY)
        assert handled is False
        assert err is not None
        assert err.status_code == 503


class TestAuthenticateOauthJwt:
    def _principal(self, *, org_id: uuid.UUID | None = _ORG_ID, org_role: str | None = "admin") -> MagicMock:
        principal = MagicMock()
        principal.organisation_id = org_id
        principal.account_id = _USER_ID
        principal.org_role = org_role
        return principal

    async def test_principal_without_org_returns_403(self) -> None:
        settings = MagicMock(secret_key="k")
        with (
            patch.object(ms, "decode_oauth_access_token", side_effect=JWTError("not oauth")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(return_value=self._principal(org_id=None))),
        ):
            handled, err, claims = await _authenticate_oauth_jwt(_mock_request(), _API_KEY, settings)
        assert handled is False
        assert claims is None
        assert err is not None
        assert err.status_code == 403

    async def test_principal_without_role_claim_returns_403(self) -> None:
        settings = MagicMock(secret_key="k")
        with (
            patch.object(ms, "decode_oauth_access_token", side_effect=JWTError("not oauth")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(return_value=self._principal(org_role=None))),
        ):
            _handled, err, _claims = await _authenticate_oauth_jwt(_mock_request(), _API_KEY, settings)
        assert err is not None
        assert err.status_code == 403

    async def test_db_failure_returns_503(self) -> None:
        settings = MagicMock(secret_key="k")
        with (
            patch.object(ms, "decode_oauth_access_token", side_effect=JWTError("not oauth")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(return_value=self._principal())),
            patch.object(ms, "_session", side_effect=SQLAlchemyError("down")),
        ):
            _handled, err, _claims = await _authenticate_oauth_jwt(_mock_request(), _API_KEY, settings)
        assert err is not None
        assert err.status_code == 503

    async def test_dead_membership_returns_403(self) -> None:
        settings = MagicMock(secret_key="k")
        with (
            patch.object(ms, "decode_oauth_access_token", side_effect=JWTError("not oauth")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(return_value=self._principal())),
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value=None)),
        ):
            _handled, err, _claims = await _authenticate_oauth_jwt(_mock_request(), _API_KEY, settings)
        assert err is not None
        assert err.status_code == 403

    async def test_valid_principal_sets_context(self) -> None:
        settings = MagicMock(secret_key="k")
        request = _mock_request()
        with (
            patch.object(ms, "decode_oauth_access_token", side_effect=JWTError("not oauth")),
            patch("modulo.auth.jwt.decode_principal", new=MagicMock(return_value=self._principal())),
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="runner")),
        ):
            handled, err, claims = await _authenticate_oauth_jwt(request, _API_KEY, settings)
        assert handled is True
        assert err is None
        assert claims is None
        assert request.scope["auth_principal"]["type"] == "user"
        assert ms._ctx_role.get() == "runner"


class TestVerifyTokenFamilyAndScopeHelpers(_AuthContext):
    async def test_token_family_check_unexpected_error_returns_401(self) -> None:
        claims = MagicMock()
        with (
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch.object(ms, "check_oauth_token_family_valid", side_effect=RuntimeError("boom")),
        ):
            response = await ms._verify_oauth_token_family(_API_KEY, claims)
        assert response is not None
        assert response.status_code == 401

    async def test_node_allowed_tools_none_for_non_run_key(self) -> None:
        assert await _node_allowed_tools_for_key(org_id=_ORG_ID, run_id=uuid.uuid4(), key_name="user-key") is None

    async def test_node_allowed_tools_none_when_run_missing(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=None)
        with patch.object(ms, "_session", return_value=_make_session_context(session)):
            result = await _node_allowed_tools_for_key(
                org_id=_ORG_ID, run_id=uuid.uuid4(), key_name="run:abc:node:node-1"
            )
        assert result is None

    async def test_node_allowed_tools_none_when_snapshot_missing(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=uuid.uuid4())
        session.get.return_value = None
        with patch.object(ms, "_session", return_value=_make_session_context(session)):
            result = await _node_allowed_tools_for_key(
                org_id=_ORG_ID, run_id=uuid.uuid4(), key_name="run:abc:node:node-1"
            )
        assert result is None

    async def test_node_allowed_tools_none_when_node_not_in_graph(self) -> None:
        snapshot = MagicMock()
        snapshot.graph_json = {"nodes": [{"id": "other-node"}]}
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=uuid.uuid4())
        session.get.return_value = snapshot
        with patch.object(ms, "_session", return_value=_make_session_context(session)):
            result = await _node_allowed_tools_for_key(
                org_id=_ORG_ID, run_id=uuid.uuid4(), key_name="run:abc:node:node-1"
            )
        assert result is None

    async def test_node_allowed_tools_swallows_db_error(self) -> None:
        session = _mock_session()
        session.execute.side_effect = SQLAlchemyError("down")
        with patch.object(ms, "_session", return_value=_make_session_context(session)):
            result = await _node_allowed_tools_for_key(
                org_id=_ORG_ID, run_id=uuid.uuid4(), key_name="run:abc:node:node-1"
            )
        assert result is None

    def test_parse_basic_auth_header_malformed(self) -> None:
        request = MagicMock()
        raw = base64.b64encode(b"\xff\xfe-not-utf8").decode()
        request.headers = {"Authorization": f"Basic {raw}"}
        creds, err = _parse_basic_auth_header(request, {})
        assert not creds
        assert err is not None
        assert err.status_code == 400


# ---------------------------------------------------------------------------
# OAuth protocol handlers
# ---------------------------------------------------------------------------


def _make_starlette_request(
    *, method: str = "GET", path: str = "/", headers: dict | None = None, query: bytes = b"", body: bytes = b""
) -> Any:
    from starlette.requests import Request

    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": raw_headers,
        "query_string": query,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive=receive)


class TestOauthAuthorizeHandler:
    _VALID_PARAMS: ClassVar[dict[str, str]] = {
        "response_type": "code",
        "client_id": "oauth_client_1",
        "redirect_uri": "https://app.example.com/callback",
        "scope": "trigger:run",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "state": "xyz",
    }

    def _request(self, params: dict[str, str]) -> Any:
        from urllib.parse import urlencode

        return _make_starlette_request(path="/mcp/oauth/authorize", query=urlencode(params).encode())

    async def _call(
        self, params: dict[str, str] | None = None, patches: dict[str, dict[str, Any]] | None = None
    ) -> Any:
        patches = patches or {}
        params = params if params is not None else self._VALID_PARAMS
        request = self._request(params)
        default_settings = MagicMock(
            modulo_public_url="https://modulo.example.com",
            cors_origins="https://modulo.example.com",
        )
        stack = [patch.object(ms, "get_settings", return_value=default_settings)]
        stack.extend(patch(target, **kwargs) for target, kwargs in patches.items())
        for cm in stack:
            cm.start()
        try:
            return await _oauth_authorize(request)
        finally:
            for cm in reversed(stack):
                cm.stop()

    async def test_unsupported_response_type(self) -> None:
        response = await self._call({**self._VALID_PARAMS, "response_type": "token"})
        assert response.status_code == 400

    async def test_missing_client_id(self) -> None:
        response = await self._call({**self._VALID_PARAMS, "client_id": ""})
        assert response.status_code == 400

    async def test_settings_error_when_public_url_missing(self) -> None:
        response = await self._call(
            patches={"modulo.api.mcp_server.get_settings": {"return_value": MagicMock(modulo_public_url=None)}}
        )
        assert response.status_code == 500

    async def test_unknown_client(self) -> None:
        response = await self._call(
            patches={
                "modulo.api.mcp_server._get_session_factory": {"return_value": _mock_factory(_mock_session())},
                "modulo.auth.oauth.get_oauth_client_by_client_id": {"new": AsyncMock(return_value=None)},
            }
        )
        assert response.status_code == 400
        assert response.body is not None

    async def test_invalid_scope(self) -> None:
        client = MagicMock()
        client.redirect_uris = "https://app.example.com/callback"
        response = await self._call(
            patches={
                "modulo.api.mcp_server._get_session_factory": {"return_value": _mock_factory(_mock_session())},
                "modulo.auth.oauth.get_oauth_client_by_client_id": {"new": AsyncMock(return_value=client)},
                "modulo.auth.oauth.normalize_scopes": {"side_effect": InvalidGrantError("bad scope")},
            }
        )
        assert response.status_code == 400

    async def test_programming_error_returns_501(self) -> None:
        session = _mock_session()
        session.begin.side_effect = ProgrammingError("stmt", {}, Exception("boom"))
        response = await self._call(
            patches={
                "modulo.api.mcp_server._get_session_factory": {"return_value": _mock_factory(session)},
            }
        )
        assert response.status_code == 501

    async def test_sqlalchemy_error_returns_503(self) -> None:
        session = _mock_session()
        session.begin.side_effect = SQLAlchemyError("down")
        response = await self._call(
            patches={
                "modulo.api.mcp_server._get_session_factory": {"return_value": _mock_factory(session)},
            }
        )
        assert response.status_code == 503

    async def test_unexpected_error_returns_500(self) -> None:
        session = _mock_session()
        session.begin.side_effect = RuntimeError("boom")
        response = await self._call(
            patches={
                "modulo.api.mcp_server._get_session_factory": {"return_value": _mock_factory(session)},
            }
        )
        assert response.status_code == 500

    async def test_success_redirects_to_consent(self) -> None:
        client = MagicMock()
        client.redirect_uris = "https://app.example.com/callback"
        client.organisation_id = _ORG_ID
        response = await self._call(
            patches={
                "modulo.api.mcp_server._get_session_factory": {"return_value": _mock_factory(_mock_session())},
                "modulo.auth.oauth.get_oauth_client_by_client_id": {"new": AsyncMock(return_value=client)},
                "modulo.auth.oauth.normalize_scopes": {"new": AsyncMock(return_value=["trigger:run"])},
                "modulo.auth.oauth.validate_client_scopes": {"new": AsyncMock(return_value=["trigger:run"])},
                "modulo.auth.oauth.create_consent_state": {"new": AsyncMock(return_value=None)},
                "modulo.api.mcp_server.set_rls_org": {"new": AsyncMock(return_value=None)},
            }
        )
        assert response.status_code == 302
        assert response.headers["Referrer-Policy"] == "no-referrer"


class TestOauthTokenAndRefreshHandlers:
    def _request(self, *, headers: dict | None = None) -> Any:
        return _make_starlette_request(method="POST", path="/mcp/oauth/token", headers=headers)

    async def test_token_impl_runtime_error_when_form_parse_degenerate(self) -> None:
        with (
            patch(
                "modulo.api.mcp_server._parse_oauth_form",
                new=AsyncMock(return_value=(None, None)),
            ),
            pytest.raises(RuntimeError),
        ):
            await _oauth_token_impl(self._request())

    async def test_token_impl_settings_error(self) -> None:
        with (
            patch(
                "modulo.api.mcp_server._parse_oauth_form",
                new=AsyncMock(return_value=({"grant_type": "authorization_code"}, None)),
            ),
            patch("modulo.api.mcp_server._extract_oauth_client_credentials", return_value=({"code": "c"}, None)),
            patch("modulo.api.mcp_server.get_settings", return_value=MagicMock(modulo_public_url=None)),
        ):
            response = await _oauth_token_impl(self._request())
        assert response.status_code == 500

    async def test_token_impl_propagates_exchange_error(self) -> None:
        with (
            patch(
                "modulo.api.mcp_server._parse_oauth_form",
                new=AsyncMock(return_value=({"grant_type": "authorization_code"}, None)),
            ),
            patch("modulo.api.mcp_server._extract_oauth_client_credentials", return_value=({"code": "c"}, None)),
            patch("modulo.api.mcp_server.get_settings", return_value=MagicMock(modulo_public_url="https://x")),
            patch(
                "modulo.api.mcp_server._exchange_authorization_code",
                new=AsyncMock(return_value=(None, MagicMock())),
            ),
        ):
            response = await _oauth_token_impl(self._request())
        assert response is not None

    async def test_token_http_exception_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_token_impl", side_effect=StarletteHTTPException(422, "nope")):
            response = await _oauth_token(self._request())
        assert response.status_code == 422

    async def test_token_programming_error_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_token_impl", side_effect=ProgrammingError("s", {}, Exception())):
            response = await _oauth_token(self._request())
        assert response.status_code == 501

    async def test_token_sqlalchemy_error_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_token_impl", side_effect=SQLAlchemyError("down")):
            response = await _oauth_token(self._request())
        assert response.status_code == 503

    async def test_token_unexpected_error_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_token_impl", side_effect=RuntimeError("boom")):
            response = await _oauth_token(self._request())
        assert response.status_code == 500

    async def test_refresh_impl_parse_error(self) -> None:
        error = MagicMock()
        with patch("modulo.api.mcp_server._parse_oauth_form", new=AsyncMock(return_value=(None, error))):
            assert await _oauth_refresh_impl(self._request()) is error

    async def test_refresh_impl_degenerate_parse(self) -> None:
        with (
            patch("modulo.api.mcp_server._parse_oauth_form", new=AsyncMock(return_value=(None, None))),
            pytest.raises(RuntimeError),
        ):
            await _oauth_refresh_impl(self._request())

    async def test_refresh_impl_credential_error(self) -> None:
        error = MagicMock()
        with (
            patch(
                "modulo.api.mcp_server._parse_oauth_form",
                new=AsyncMock(return_value=({"grant_type": "refresh_token"}, None)),
            ),
            patch("modulo.api.mcp_server._extract_oauth_refresh_credentials", return_value=({}, error)),
        ):
            assert await _oauth_refresh_impl(self._request()) is error

    async def test_refresh_impl_exchange_error(self) -> None:
        error = MagicMock()
        with (
            patch(
                "modulo.api.mcp_server._parse_oauth_form",
                new=AsyncMock(return_value=({"grant_type": "refresh_token"}, None)),
            ),
            patch(
                "modulo.api.mcp_server._extract_oauth_refresh_credentials",
                return_value=({"refresh_token": "r"}, None),
            ),
            patch("modulo.api.mcp_server._exchange_refresh_token", new=AsyncMock(return_value=(None, error))),
        ):
            assert await _oauth_refresh_impl(self._request()) is error

    async def test_refresh_impl_degenerate_exchange(self) -> None:
        with (
            patch(
                "modulo.api.mcp_server._parse_oauth_form",
                new=AsyncMock(return_value=({"grant_type": "refresh_token"}, None)),
            ),
            patch(
                "modulo.api.mcp_server._extract_oauth_refresh_credentials",
                return_value=({"refresh_token": "r"}, None),
            ),
            patch("modulo.api.mcp_server._exchange_refresh_token", new=AsyncMock(return_value=(None, None))),
            pytest.raises(RuntimeError),
        ):
            await _oauth_refresh_impl(self._request())

    async def test_refresh_value_error_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_refresh_impl", side_effect=ValueError("bad token")):
            response = await _oauth_refresh(self._request())
        assert response.status_code == 400

    async def test_refresh_jwt_error_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_refresh_impl", side_effect=JWTError("expired")):
            response = await _oauth_refresh(self._request())
        assert response.status_code == 400

    async def test_refresh_http_exception_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_refresh_impl", side_effect=StarletteHTTPException(400, "x")):
            response = await _oauth_refresh(self._request())
        assert response.status_code == 400

    async def test_refresh_programming_error_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_refresh_impl", side_effect=ProgrammingError("s", {}, Exception())):
            response = await _oauth_refresh(self._request())
        assert response.status_code == 501

    async def test_refresh_sqlalchemy_error_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_refresh_impl", side_effect=SQLAlchemyError("down")):
            response = await _oauth_refresh(self._request())
        assert response.status_code == 503

    async def test_refresh_unexpected_error_handler(self) -> None:
        with patch("modulo.api.mcp_server._oauth_refresh_impl", side_effect=RuntimeError("boom")):
            response = await _oauth_refresh(self._request())
        assert response.status_code == 500

    def test_extract_refresh_credentials_rejects_wrong_grant(self) -> None:
        request = MagicMock()
        request.headers = {}
        creds, err = _extract_oauth_refresh_credentials(request, {"grant_type": "authorization_code"})
        assert not creds
        assert err is not None


class TestMcpAsgiApp:
    @pytest.fixture
    def app(self) -> Any:
        with patch("modulo.core.rate_limiter.RateLimiterRegistry.check", new=AsyncMock(return_value=True)):
            yield build_mcp_asgi_app()

    def test_healthz_bypasses_auth(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_missing_bearer_token_returns_401(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get("/mcp", headers={"X-Modulo-Allowed-Tools": "list_runs, cancel_run"})
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"

    def test_api_key_auth_error_response_is_returned(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        auth_err = ms.Response('{"error":"unauthorized"}', status_code=401, media_type="application/json")
        with (
            patch.object(ms, "_authenticate_api_key", new=AsyncMock(return_value=(False, auth_err))),
            TestClient(app) as client,
        ):
            resp = client.get("/mcp", headers={"Authorization": f"Bearer {_API_KEY}"})
        assert resp.status_code == 401

    def test_mcp_exception_handler_shape(self) -> None:
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/mcp"
        response = ms._mcp_exception_handler(request, RuntimeError("boom"))
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# Tool error-path envelopes
# ---------------------------------------------------------------------------


class TestSimpleToolErrorHandlers(_AuthContext):
    async def test_list_pipelines_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.pipeline.list_pipelines", side_effect=RuntimeError("boom")),
        ):
            result = await list_pipelines_tool()
        assert result["error"] == "internal_error"

    async def test_create_pipeline_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.pipeline.create_pipeline", side_effect=RuntimeError("boom")),
        ):
            result = await create_pipeline(name="p")
        assert result["error"] == "internal_error"

    async def test_list_runs_error_envelopes(self) -> None:
        with patch.object(ms, "_list_runs_impl", side_effect=MCPAuthorizationError("no")):
            assert (await list_runs())["error"] == "insufficient_scope"
        with patch.object(ms, "_list_runs_impl", side_effect=ProgrammingError("s", {}, Exception())):
            assert (await list_runs())["error"] == "migration_required"
        with patch.object(ms, "_list_runs_impl", side_effect=RuntimeError("boom")):
            assert (await list_runs())["error"] == "internal_error"

    async def test_get_pipeline_graph_defensive_invalid_id(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_parse_uuid_param", return_value=(None, None)),
        ):
            result = await get_pipeline_graph_tool(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "invalid_id"

    async def test_get_pipeline_graph_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.pipeline.get_pipeline_graph", side_effect=RuntimeError("boom")),
        ):
            result = await get_pipeline_graph_tool(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_copy_library_primitive_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "library_copy_to_adapt", side_effect=RuntimeError("boom")),
        ):
            result = await copy_library_primitive(primitive_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_search_library_migration_required(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "list_primitives", side_effect=ProgrammingError("s", {}, Exception())),
        ):
            result = await search_library()
        assert result["error"] == "migration_required"

    async def test_create_agent_migration_required(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.agent.create_agent", side_effect=ProgrammingError("s", {}, Exception())),
        ):
            result = await create_agent(name="a", prompt_template="p")
        assert result["error"] == "migration_required"

    async def test_get_integration_status_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", side_effect=RuntimeError("boom")),
        ):
            result = await get_integration_status()
        assert result["error"] == "internal_error"

    async def test_get_org_config_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.system_config.list_config", side_effect=RuntimeError("boom")),
        ):
            result = await get_org_config()
        assert result["error"] == "internal_error"

    async def test_get_org_config_success_with_sections(self) -> None:
        long_value = {"blob": "v" * 300}
        cfg_remy = MagicMock(key="remy_config:1", value="plain")
        cfg_limits = MagicMock(key="rate_limits:y", value=long_value)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.system_config.list_config", new=AsyncMock(return_value=[cfg_remy, cfg_limits])),
        ):
            result = await get_org_config(section="rate_limits")
        assert result["count"] == 1
        assert "..." in result["results"]

    async def test_get_available_features_internal_error(self) -> None:
        org = MagicMock()
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_settings", return_value=MagicMock()),
            patch("modulo.db.crud.organisation.get_organisation", new=AsyncMock(return_value=org)),
            patch("modulo.core.feature_flags.resolve_plan_context", side_effect=RuntimeError("boom")),
        ):
            result = await get_available_features()
        assert result["error"] == "internal_error"

    async def test_create_schema_database_unavailable_and_internal(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "db_create_schema", side_effect=SQLAlchemyError("down")),
        ):
            result = await create_schema(name="s")
        assert result["error"] == "database_unavailable"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "db_create_schema", side_effect=RuntimeError("boom")),
        ):
            result = await create_schema(name="s")
        assert result["error"] == "internal_error"

    async def test_list_schemas_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "db_list_schemas", side_effect=RuntimeError("boom")),
        ):
            result = await list_schemas()
        assert result["error"] == "internal_error"

    async def test_infer_schema_error_envelopes(self) -> None:
        settings = MagicMock(modulo_dev_mode=True)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.settings.get_settings", return_value=settings),
            patch(
                "modulo.db.crud.model_backend.list_model_backends", side_effect=ProgrammingError("s", {}, Exception())
            ),
        ):
            result = await infer_schema(input_sample={"a": 1})
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.settings.get_settings", return_value=settings),
            patch("modulo.db.crud.model_backend.list_model_backends", side_effect=RuntimeError("boom")),
        ):
            result = await infer_schema(input_sample={"a": 1})
        assert result["error"] == "internal_error"

    async def test_validate_payload_error_envelopes(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_schema", side_effect=ProgrammingError("s", {}, Exception())),
        ):
            result = await validate_payload(schema_id=str(uuid.uuid4()), payload={})
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_schema", side_effect=RuntimeError("boom")),
        ):
            result = await validate_payload(schema_id=str(uuid.uuid4()), payload={})
        assert result["error"] == "internal_error"

    async def test_list_housekeeping_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.core.housekeeping.scan_all", side_effect=RuntimeError("boom")),
        ):
            result = await list_housekeeping()
        assert result["error"] == "internal_error"

    async def test_perform_housekeeping_deletes_and_reports_unknown(self) -> None:
        from modulo.db.models.secret import Secret

        existing = MagicMock()
        session = _mock_session()
        session.execute.side_effect = [
            _make_execute_result(scalar_one_or_none=existing),
            _make_execute_result(scalar_one_or_none=None),
        ]
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.core.housekeeping.ENTITY_MODEL_MAP", {"secret": Secret}),
            patch("modulo.core.housekeeping.NON_DELETABLE_ENTITY_TYPES", set()),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await perform_housekeeping(
                items=[
                    {"entity_type": "secret", "id": "a"},
                    {"entity_type": "secret", "id": "b"},
                ]
            )
        assert result["deleted_count"] == 1
        assert not result["errors"]

    async def test_perform_housekeeping_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", side_effect=RuntimeError("boom")),
        ):
            result = await perform_housekeeping(items=[{"entity_type": "secret", "id": "a"}])
        assert result["error"] == "internal_error"

    def test_get_doc_index_builds_and_caches(self) -> None:
        ms._doc_index = None
        ms._doc_index_ts = 0.0
        try:
            with patch.object(ms, "DocumentationIndex") as mock_cls:
                first = _get_doc_index()
                second = _get_doc_index()
            assert first is second
            assert first is mock_cls.build.return_value
        finally:
            ms._doc_index = None
            ms._doc_index_ts = 0.0

    async def test_search_documentation_empty_and_hits(self) -> None:
        index = MagicMock()
        index.search.return_value = []
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "DocumentationIndex") as mock_cls,
        ):
            mock_cls.build.return_value = index
            ms._doc_index = None
            ms._doc_index_ts = 0.0
            result = await search_documentation(query="x")
            assert result["count"] == 0
            index.search.return_value = [MagicMock()]
            index.format_results.return_value = "- hit"
            result = await search_documentation(query="x")
        assert result["count"] == 1
        assert result["results"] == "- hit"

    async def test_get_run_output_error_envelopes(self) -> None:
        with patch.object(ms, "_get_run_output_impl", side_effect=ProgrammingError("s", {}, Exception())):
            result = await get_run_output(run_id=str(uuid.uuid4()), node_id="n")
        assert result["error"] == "migration_required"
        with patch.object(ms, "_get_run_output_impl", side_effect=RuntimeError("boom")):
            result = await get_run_output(run_id=str(uuid.uuid4()), node_id="n")
        assert result["error"] == "internal_error"

    async def test_get_run_evals_internal_error(self) -> None:
        with patch.object(ms, "_get_run_evals_impl", side_effect=RuntimeError("boom")):
            result = await get_run_evals(run_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_cancel_run_internal_error(self) -> None:
        with patch.object(ms, "_cancel_run_impl", side_effect=RuntimeError("boom")):
            result = await cancel_run(run_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_list_pending_hitl_internal_error(self) -> None:
        with patch.object(ms, "_list_pending_hitl_impl", side_effect=RuntimeError("boom")):
            result = await ms.list_pending_hitl()
        assert result["error"] == "internal_error"

    async def test_list_eval_definitions_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.eval_definition.list_eval_definitions", side_effect=RuntimeError("boom")),
        ):
            result = await list_eval_definitions()
        assert result["error"] == "internal_error"

    async def test_create_model_backend_error_envelopes(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "db_create_model_backend", side_effect=ProgrammingError("s", {}, Exception())),
        ):
            result = await create_model_backend(name="n", display_name="d", provider="openai", model_id="m")
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "db_create_model_backend", side_effect=RuntimeError("boom")),
        ):
            result = await create_model_backend(name="n", display_name="d", provider="openai", model_id="m")
        assert result["error"] == "internal_error"

    async def test_delete_pipeline_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.team_scope.pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.pipeline.soft_delete_pipeline", side_effect=RuntimeError("boom")),
        ):
            result = await delete_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_delete_connector_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.connector_instance.delete_connector_instance", side_effect=RuntimeError("boom")),
        ):
            result = await delete_connector(connector_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_create_secret_error_envelopes(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="k")),
            patch(
                "modulo.core.secrets_backend.create_secrets_backend", side_effect=ProgrammingError("s", {}, Exception())
            ),
        ):
            result = await create_secret(key="k", value="v")
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="k")),
            patch("modulo.core.secrets_backend.create_secrets_backend", side_effect=RuntimeError("boom")),
        ):
            result = await create_secret(key="k", value="v")
        assert result["error"] == "internal_error"

    async def test_list_secrets_internal_error(self) -> None:
        session = _mock_session()
        session.execute.side_effect = RuntimeError("boom")
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await list_secrets()
        assert result["error"] == "internal_error"

    async def test_delete_secret_error_envelopes(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="k")),
            patch(
                "modulo.core.secrets_backend.create_secrets_backend", side_effect=ProgrammingError("s", {}, Exception())
            ),
        ):
            result = await delete_secret(key="k")
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="k")),
            patch("modulo.core.secrets_backend.create_secrets_backend", side_effect=RuntimeError("boom")),
        ):
            result = await delete_secret(key="k")
        assert result["error"] == "internal_error"

    async def test_create_connector_internal_error(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_settings", side_effect=RuntimeError("boom")),
        ):
            result = await create_connector(name="c", connector_type_id="t", credentials="x")
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# API-key management tools
# ---------------------------------------------------------------------------


class TestApiKeyToolGaps(_AuthContext):
    async def test_parse_api_key_team_id_invalid(self) -> None:
        team_uuid, err = await _parse_api_key_team_id("not-a-uuid", _ORG_ID)
        assert team_uuid is None
        assert err is not None
        assert err["field"] == "team_id"

    async def test_create_api_key_invalid_team_id(self) -> None:
        session = _mock_session()
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="operator")),
        ):
            result = await create_api_key(name="k", role="operator", team_id="not-a-uuid")
        assert result["error"] == "invalid_id"

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (IntegrityError("s", {}, Exception()), "conflict"),
            (ProgrammingError("s", {}, Exception()), "migration_required"),
            (SQLAlchemyError("down"), "internal_error"),
            (RuntimeError("boom"), "internal_error"),
        ],
    )
    async def test_create_api_key_error_envelopes(self, exc: Exception, expected: str) -> None:
        session = _mock_session()
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="operator")),
            patch.object(ms, "auth_create_api_key", side_effect=exc),
        ):
            result = await create_api_key(name="k", role="operator")
        assert result["error"] == expected

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ProgrammingError("s", {}, Exception()), "migration_required"),
            (SQLAlchemyError("down"), "internal_error"),
            (RuntimeError("boom"), "internal_error"),
        ],
    )
    async def test_list_api_keys_error_envelopes(self, exc: Exception, expected: str) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "auth_list_api_keys", side_effect=exc),
        ):
            result = await list_api_keys()
        assert result["error"] == expected

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (IntegrityError("s", {}, Exception()), "conflict"),
            (ProgrammingError("s", {}, Exception()), "migration_required"),
            (SQLAlchemyError("down"), "internal_error"),
            (RuntimeError("boom"), "internal_error"),
        ],
    )
    async def test_revoke_api_key_error_envelopes(self, exc: Exception, expected: str) -> None:
        session = _mock_session()
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "auth_revoke_api_key", side_effect=exc),
        ):
            result = await revoke_api_key(key_id=str(uuid.uuid4()))
        assert result["error"] == expected


# ---------------------------------------------------------------------------
# Pipeline graph update + connector binding
# ---------------------------------------------------------------------------


class TestUpdatePipelineGraphImpl(_AuthContext):
    def _valid_graph(self) -> tuple[list[dict], list[dict]]:
        nid = str(uuid.uuid4())
        nodes = [{"id": nid, "node_type": "agent", "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}]
        edges = [
            {
                "source_node_id": nid,
                "target_node_id": str(uuid.uuid4()),
                "edge_type": "normal",
            }
        ]
        return nodes, edges

    async def test_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await update_pipeline_graph(pipeline_id=str(uuid.uuid4()), nodes=[], edges=[])
        assert result["error"] == "auth_expired"

    async def test_invalid_pipeline_id(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_pipeline_graph(pipeline_id="nope", nodes=[], edges=[])
        assert result["error"] == "invalid_id"

    async def test_pydantic_validation_failure(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_pipeline_graph(pipeline_id=str(uuid.uuid4()), nodes=[{"bad": "node"}], edges=[])
        assert result["error"] == "validation_failed"

    async def test_sandbox_mode_gate(self) -> None:
        bogus_model = MagicMock()
        bogus_model.model_validate = MagicMock(return_value=None)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.pipelines.PipelineGraphUpdate", bogus_model),
        ):
            result = await update_pipeline_graph(
                pipeline_id=str(uuid.uuid4()),
                nodes=[{"id": "n1", "node_type": "sandbox_agent", "mode": "bogus"}],
                edges=[],
            )
        assert result["error"] == "validation_failed"
        assert result["field"] == "nodes"

    async def test_replace_returns_none(self) -> None:
        nodes, edges = self._valid_graph()
        pipeline = MagicMock()
        pipeline.owner_team_id = None
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.core.team_visibility.find_connector_team_mismatches", new=AsyncMock(return_value=[])),
            patch("modulo.db.crud.pipeline.replace_pipeline_graph", new=AsyncMock(return_value=None)),
        ):
            result = await update_pipeline_graph(pipeline_id=str(uuid.uuid4()), nodes=nodes, edges=edges)
        assert result["error"] == "pipeline_not_found"

    async def test_error_envelopes(self) -> None:
        with patch.object(ms, "_update_pipeline_graph_impl", side_effect=MCPAuthorizationError("no")):
            assert (await update_pipeline_graph(pipeline_id="p", nodes=[], edges=[]))["error"] == "insufficient_scope"
        with patch.object(ms, "_update_pipeline_graph_impl", side_effect=ProgrammingError("s", {}, Exception())):
            result = await update_pipeline_graph(pipeline_id="p", nodes=[], edges=[])
        assert result["error"] == "migration_required"
        with patch.object(ms, "_update_pipeline_graph_impl", side_effect=RuntimeError("boom")):
            result = await update_pipeline_graph(pipeline_id="p", nodes=[], edges=[])
        assert result["error"] == "internal_error"

    async def test_hitl_denial_audit_without_user_context(self) -> None:
        ms._ctx_user_id.set(None)
        exc = ms.HitlGateWeakeningDenied(
            reason_code="gate_removed",
            correlation_keys=[("a", "b", "normal")],
            weakening_types=["remove"],
            payload_json=None,
        )
        with (
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock(return_value=None)) as mock_audit,
        ):
            await _append_mcp_hitl_denial_audit(_ORG_ID, uuid.uuid4(), exc)
        assert mock_audit.await_count == 1

    async def test_hitl_denial_audit_reraises_cancellation(self) -> None:
        exc = ms.HitlGateWeakeningDenied(reason_code="gate_removed", payload_json=None)
        with (
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
            patch("modulo.core.audit_logger.append_audit_event", side_effect=asyncio.CancelledError),
            pytest.raises(asyncio.CancelledError),
        ):
            await _append_mcp_hitl_denial_audit(_ORG_ID, uuid.uuid4(), exc)


class TestBindConnectorToNode(_AuthContext):
    async def test_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await bind_connector_to_node(
                pipeline_id=str(uuid.uuid4()), node_id="n", connector_type="t", connector_instance_id=str(uuid.uuid4())
            )
        assert result["error"] == "auth_expired"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"pipeline_id": "nope"},
            {"node_id": "nope"},
            {"connector_instance_id": "nope"},
        ],
    )
    async def test_invalid_uuid_params(self, kwargs: dict) -> None:
        base = {
            "pipeline_id": str(uuid.uuid4()),
            "node_id": str(uuid.uuid4()),
            "connector_instance_id": str(uuid.uuid4()),
        }
        base.update(kwargs)
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await bind_connector_to_node(connector_type="t", **base)
        assert result["error"] == "invalid_id"

    async def test_connector_not_found(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.connector_instance.get_connector_instance", new=AsyncMock(return_value=None)),
        ):
            result = await bind_connector_to_node(
                pipeline_id=str(uuid.uuid4()),
                node_id=str(uuid.uuid4()),
                connector_type="t",
                connector_instance_id=str(uuid.uuid4()),
            )
        assert result["error"] == "connector_not_found"

    async def test_pipeline_not_found(self) -> None:
        connector = MagicMock()
        connector.organisation_id = _ORG_ID
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=None)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.connector_instance.get_connector_instance", new=AsyncMock(return_value=connector)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await bind_connector_to_node(
                pipeline_id=str(uuid.uuid4()),
                node_id=str(uuid.uuid4()),
                connector_type="t",
                connector_instance_id=str(uuid.uuid4()),
            )
        assert result["error"] == "pipeline_not_found"

    async def test_error_envelopes(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "check_tool_scope", side_effect=MCPAuthorizationError("no")),
        ):
            result = await bind_connector_to_node(
                pipeline_id=str(uuid.uuid4()), node_id="n", connector_type="t", connector_instance_id=str(uuid.uuid4())
            )
        assert result["error"] == "insufficient_scope"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch(
                "modulo.db.crud.connector_instance.get_connector_instance",
                side_effect=ProgrammingError("s", {}, Exception()),
            ),
        ):
            result = await bind_connector_to_node(
                pipeline_id=str(uuid.uuid4()),
                node_id=str(uuid.uuid4()),
                connector_type="t",
                connector_instance_id=str(uuid.uuid4()),
            )
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.connector_instance.get_connector_instance", side_effect=RuntimeError("boom")),
        ):
            result = await bind_connector_to_node(
                pipeline_id=str(uuid.uuid4()),
                node_id=str(uuid.uuid4()),
                connector_type="t",
                connector_instance_id=str(uuid.uuid4()),
            )
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# trigger_pipeline paths
# ---------------------------------------------------------------------------


class TestTriggerPipelinePaths(_AuthContext):
    async def test_create_manual_run_pipeline_not_found(self) -> None:
        with patch.object(ms, "get_pipeline", new=AsyncMock(return_value=None)):
            run_id, _thread_id, err = await _create_manual_run(
                _mock_session(), _ORG_ID, uuid.uuid4(), str(uuid.uuid4()), {}
            )
        assert run_id is None
        assert err is not None
        assert err["error"] == "pipeline_not_found"

    async def test_create_manual_run_snapshot_failed(self) -> None:
        pipeline = MagicMock()
        pipeline.owner_team_id = None
        with (
            patch.object(ms, "get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph", new=AsyncMock(return_value=None)),
        ):
            _run_id, _thread_id, err = await _create_manual_run(
                _mock_session(), _ORG_ID, uuid.uuid4(), str(uuid.uuid4()), {}
            )
        assert err is not None
        assert err["error"] == "snapshot_failed"

    async def test_create_manual_run_empty_graph(self) -> None:
        pipeline = MagicMock()
        pipeline.owner_team_id = None
        snapshot = MagicMock()
        snapshot.graph_json = {"nodes": []}
        with (
            patch.object(ms, "get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch(
                "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
                new=AsyncMock(return_value=snapshot),
            ),
        ):
            _run_id, _thread_id, err = await _create_manual_run(
                _mock_session(), _ORG_ID, uuid.uuid4(), str(uuid.uuid4()), {}
            )
        assert err is not None
        assert err["error"] == "validation_failed"

    async def test_impl_invalid_pipeline_id(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await _trigger_pipeline_impl("nope", None)
        assert result["error"] == "invalid_id"

    async def test_impl_degenerate_validation(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_trigger_pipeline_validate_id", return_value=(None, None)),
            pytest.raises(RuntimeError),
        ):
            await _trigger_pipeline_impl(str(uuid.uuid4()), None)

    async def test_impl_degenerate_manual_run(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_create_manual_run", new=AsyncMock(return_value=(None, None, None))),
            pytest.raises(RuntimeError),
        ):
            await _trigger_pipeline_impl(str(uuid.uuid4()), None)

    async def test_org_deleted_error_mapping(self) -> None:
        with patch.object(ms, "_trigger_pipeline_impl", side_effect=OrgDeletedError(org_id=_ORG_ID, deleted=True)):
            result = await ms.trigger_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "org_deleted"

    async def test_org_missing_error_mapping(self) -> None:
        with patch.object(ms, "_trigger_pipeline_impl", side_effect=OrgDeletedError(org_id=_ORG_ID, deleted=False)):
            result = await ms.trigger_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "org_not_found"

    async def test_snapshot_lock_busy_mapping(self) -> None:
        with patch.object(ms, "_trigger_pipeline_impl", side_effect=SnapshotLockNotAvailableError("busy")):
            result = await ms.trigger_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "snapshot_lock_busy"

    async def test_storage_exhausted_mapping(self) -> None:
        with patch.object(ms, "_trigger_pipeline_impl", side_effect=StorageExhaustedError("full")):
            result = await ms.trigger_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "storage_exhausted"

    async def test_migration_required_mapping(self) -> None:
        with patch.object(ms, "_trigger_pipeline_impl", side_effect=ProgrammingError("s", {}, Exception())):
            result = await ms.trigger_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"

    async def test_internal_error_mapping(self) -> None:
        with patch.object(ms, "_trigger_pipeline_impl", side_effect=RuntimeError("boom")):
            result = await ms.trigger_pipeline(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"


# ---------------------------------------------------------------------------
# Eval definition tools
# ---------------------------------------------------------------------------


class TestEvalDefinitionTools(_AdminContext):
    def test_assert_failure_behaviour_rejects_unknown(self) -> None:
        assert _assert_failure_behaviour("retry") is not None

    def test_assert_pass_threshold_rejects_out_of_range(self) -> None:
        assert _assert_pass_threshold(1.5) is not None

    async def test_create_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await create_eval_definition(pipeline_id=str(uuid.uuid4()), name="n", eval_type="llm_judge")
        assert result["error"] == "auth_expired"

    async def test_create_rejects_blank_name(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await create_eval_definition(pipeline_id=str(uuid.uuid4()), name="  ", eval_type="llm_judge")
        assert result["error"] == "invalid_name"

    async def test_create_rejects_bad_failure_behaviour(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await create_eval_definition(
                pipeline_id=str(uuid.uuid4()), name="n", eval_type="llm_judge", failure_behaviour="retry"
            )
        assert result["error"] == "invalid_failure_behaviour"

    async def test_create_rejects_bad_pass_threshold(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await create_eval_definition(
                pipeline_id=str(uuid.uuid4()), name="n", eval_type="llm_judge", pass_threshold=2.0
            )
        assert result["error"] == "invalid_pass_threshold"

    async def test_create_rejects_invalid_pipeline_id(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await create_eval_definition(pipeline_id="nope", name="n", eval_type="llm_judge")
        assert result["error"] == "invalid_id"

    async def test_create_rejects_invalid_node_id(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await create_eval_definition(
                pipeline_id=str(uuid.uuid4()), node_id="nope", name="n", eval_type="llm_judge"
            )
        assert result["error"] == "invalid_id"

    async def test_create_guardrail_validation_failure(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch(
                "modulo.api.routes.evals._validate_guardrail_request",
                side_effect=StarletteHTTPException(422, "bad guardrail"),
            ),
        ):
            result = await create_eval_definition(
                pipeline_id=str(uuid.uuid4()), name="n", eval_type="guardrail", config_json={}
            )
        assert result["error"] == "validation_failed"

    async def test_create_pipeline_not_found(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=None)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.evals._validate_guardrail_request", return_value=None),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await create_eval_definition(pipeline_id=str(uuid.uuid4()), name="n", eval_type="llm_judge")
        assert result["error"] == "pipeline_not_found"

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (IntegrityError("s", {}, Exception()), "conflict"),
            (ProgrammingError("s", {}, Exception()), "migration_required"),
            (SQLAlchemyError("down"), "database_unavailable"),
            (RuntimeError("boom"), "internal_error"),
        ],
    )
    async def test_create_error_envelopes(self, exc: Exception, expected: str) -> None:
        session = _mock_session()
        pipeline = MagicMock()
        if isinstance(exc, IntegrityError):
            session.execute.return_value = _make_execute_result(scalar_one_or_none=pipeline)
            session.flush.side_effect = exc
        else:
            session.execute.side_effect = exc
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.evals._validate_guardrail_request", return_value=None),
            patch("modulo.api.routes.evals._eval_def_to_dict", return_value={"id": "x"}),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await create_eval_definition(pipeline_id=str(uuid.uuid4()), name="n", eval_type="llm_judge")
        assert result["error"] == expected

    async def test_create_outer_http_exception(self) -> None:
        session = _mock_session()
        pipeline = MagicMock()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=pipeline)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.evals._validate_guardrail_request", return_value=None),
            patch(
                "modulo.api.routes.evals._eval_def_to_dict",
                side_effect=StarletteHTTPException(500, "serialize fail"),
            ),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await create_eval_definition(pipeline_id=str(uuid.uuid4()), name="n", eval_type="llm_judge")
        assert result["error"] == "validation_failed"

    async def test_update_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    async def test_update_rejects_bad_eval_type(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), eval_type="bogus")
        assert result["error"] == "invalid_eval_type"

    async def test_update_rejects_bad_failure_behaviour(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), failure_behaviour="retry")
        assert result["error"] == "invalid_failure_behaviour"

    async def test_update_rejects_bad_pass_threshold(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), pass_threshold=-0.5)
        assert result["error"] == "invalid_pass_threshold"

    async def test_update_rejects_blank_name(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), name="  ")
        assert result["error"] == "invalid_name"

    async def test_update_rejects_overlong_name(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), name="x" * 256)
        assert result["error"] == "invalid_name"

    async def test_update_rejects_invalid_ids(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_eval_definition(eval_id="nope")
        assert result["error"] == "invalid_id"
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), node_id="nope")
        assert result["error"] == "invalid_id"

    async def test_update_not_found(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=None)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), name="n")
        assert result["error"] == "eval_definition_not_found"

    async def test_update_full_success(self) -> None:
        eval_def = MagicMock()
        eval_def.eval_type = "llm_judge"
        eval_def.failure_behaviour = "warn"
        eval_def.config_json = {}
        eval_def.version = 1
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=eval_def)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.evals._validate_guardrail_request", return_value=None),
            patch("modulo.api.routes.evals._eval_def_to_dict", return_value={"id": "x"}),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await update_eval_definition(
                eval_id=str(uuid.uuid4()),
                node_id=str(uuid.uuid4()),
                name="new-name",
                eval_type="llm_judge",
                config_json={"k": "v"},
                failure_behaviour="warn",
                pass_threshold=0.5,
                suite_id="suite-1",
            )
        assert result == {"id": "x"}
        assert eval_def.version == 2

    async def test_update_guardrail_validation_failure(self) -> None:
        eval_def = MagicMock()
        eval_def.eval_type = "guardrail"
        eval_def.failure_behaviour = "warn"
        eval_def.config_json = {}
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=eval_def)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch(
                "modulo.api.routes.evals._validate_guardrail_request",
                side_effect=StarletteHTTPException(422, "bad guardrail"),
            ),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), config_json={"action": "bogus"})
        assert result["error"] == "validation_failed"

    async def test_update_outer_http_exception(self) -> None:
        eval_def = MagicMock()
        eval_def.eval_type = "llm_judge"
        eval_def.failure_behaviour = "warn"
        eval_def.config_json = {}
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=eval_def)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch(
                "modulo.api.routes.evals._stamp_eval_definition_version",
                side_effect=StarletteHTTPException(422, "stamp fail"),
            ),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), name="n")
        assert result["error"] == "validation_failed"

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (IntegrityError("s", {}, Exception()), "conflict"),
            (ProgrammingError("s", {}, Exception()), "migration_required"),
            (SQLAlchemyError("down"), "database_unavailable"),
            (RuntimeError("boom"), "internal_error"),
        ],
    )
    async def test_update_error_envelopes(self, exc: Exception, expected: str) -> None:
        session = _mock_session()
        if isinstance(exc, IntegrityError):
            session.execute.return_value = _make_execute_result(scalar_one_or_none=MagicMock())
            session.flush.side_effect = exc
        else:
            session.execute.side_effect = exc
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await update_eval_definition(eval_id=str(uuid.uuid4()), name="n")
        assert result["error"] == expected

    async def test_delete_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await delete_eval_definition(eval_id=str(uuid.uuid4()))
        assert result["error"] == "auth_expired"

    async def test_delete_invalid_id(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await delete_eval_definition(eval_id="nope")
        assert result["error"] == "invalid_id"

    async def test_delete_guardrail_soft_audit_failure_is_swallowed(self) -> None:
        eval_def = MagicMock()
        eval_def.eval_type = "guardrail"
        eval_def.name = "gatekeeper"
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=eval_def)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.core.audit_logger.append_audit_event", side_effect=RuntimeError("audit down")),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await delete_eval_definition(eval_id=str(uuid.uuid4()))
        assert result["soft_deleted"] is True
        assert result["hard_deleted"] is False

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (IntegrityError("s", {}, Exception()), "conflict"),
            (ProgrammingError("s", {}, Exception()), "migration_required"),
            (SQLAlchemyError("down"), "database_unavailable"),
            (RuntimeError("boom"), "internal_error"),
        ],
    )
    async def test_delete_error_envelopes(self, exc: Exception, expected: str) -> None:
        eval_def = MagicMock()
        eval_def.eval_type = "regex"
        eval_def.name = "regex-eval"
        session = _mock_session()
        if isinstance(exc, IntegrityError):
            session.execute.return_value = _make_execute_result(scalar_one_or_none=eval_def)
            session.delete.side_effect = exc
        else:
            session.execute.side_effect = exc
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await delete_eval_definition(eval_id=str(uuid.uuid4()))
        assert result["error"] == expected


# ---------------------------------------------------------------------------
# HITL helpers
# ---------------------------------------------------------------------------


class TestHitlHelpers(_AuthContext):
    async def test_load_hitl_run_missing(self) -> None:
        with patch.object(ms, "get_run", new=AsyncMock(return_value=None)):
            assert await _load_hitl_run(_mock_session(), uuid.uuid4()) is None

    async def test_dispatch_hitl_action_reject_with_reason(self) -> None:
        mgr = MagicMock()
        mgr.reject = AsyncMock(return_value=None)
        result = await _dispatch_hitl_action(
            mgr, _mock_session(), "reject", uuid.uuid4(), "gate", _ORG_ID, _USER_ID, "tok", None, "nope"
        )
        assert result["status"] == "rejected"
        payload = mgr.reject.await_args.kwargs["decision_payload"]
        assert payload["reason"] == "nope"

    async def test_review_impl_gate_not_found(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_run", new=AsyncMock(return_value=None)),
        ):
            result = await _review_hitl_impl(
                run_id=str(uuid.uuid4()), gate_id="gate", action="claim", claim_token=None, reason=None, output=None
            )
        assert result["error"] == "gate_not_found"

    async def test_review_impl_degenerate_parse(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_parse_hitl_action", return_value=(None, None)),
            pytest.raises(RuntimeError),
        ):
            await _review_hitl_impl(
                run_id=str(uuid.uuid4()),
                gate_id="gate",
                action="claim",
                claim_token=None,
                reason=None,
                output=None,
            )

    async def test_review_hitl_operational_error_propagates(self) -> None:
        from sqlalchemy.exc import OperationalError

        with (
            patch.object(ms, "_review_hitl_impl", side_effect=OperationalError("s", {}, Exception())),
            pytest.raises(OperationalError),
        ):
            await ms.review_hitl(run_id=str(uuid.uuid4()), gate_id="gate", action="claim")


# ---------------------------------------------------------------------------
# Trigger tools
# ---------------------------------------------------------------------------


class TestTriggerToolGaps(_AuthContext):
    async def test_create_impl_degenerate_validation(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_validate_trigger_create_inputs", return_value=(None, None)),
            pytest.raises(RuntimeError),
        ):
            await _create_trigger_impl(
                pipeline_id=str(uuid.uuid4()),
                trigger_type="manual",
                active=True,
                cron_expression=None,
                config_json=None,
                max_concurrent_runs=1,
                daily_spend_limit=None,
            )

    async def test_create_error_envelopes(self) -> None:
        with patch.object(ms, "_create_trigger_impl", side_effect=ProgrammingError("s", {}, Exception())):
            result = await create_trigger(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"
        with patch.object(ms, "_create_trigger_impl", side_effect=RuntimeError("boom")):
            result = await create_trigger(pipeline_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_get_trigger_defensive_invalid_id(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_parse_uuid_param", return_value=(None, None)),
        ):
            result = await get_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "invalid_id"

    async def test_get_trigger_team_boundary(self) -> None:
        other_team = uuid.uuid4()
        ms._ctx_team_id.set(other_team)
        trigger = MagicMock()
        trigger.pipeline_id = uuid.uuid4()
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_load_trigger_row", new=AsyncMock(return_value=trigger)),
            patch("modulo.db.crud.team_scope.pipeline_owner_team_id", new=AsyncMock(return_value=uuid.uuid4())),
        ):
            result = await get_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "team_boundary_violation"

    async def test_get_trigger_error_envelopes(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_load_trigger_row", side_effect=ProgrammingError("s", {}, Exception())),
        ):
            result = await get_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_load_trigger_row", side_effect=RuntimeError("boom")),
        ):
            result = await get_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_update_trigger_degenerate_validation(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_validate_trigger_update_inputs", return_value=(None, None)),
        ):
            result = await update_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_update_trigger_error_envelopes(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_load_trigger_for_update", side_effect=ProgrammingError("s", {}, Exception())),
        ):
            result = await update_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_load_trigger_for_update", side_effect=RuntimeError("boom")),
        ):
            result = await update_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_update_trigger_reenable_full_flow(self) -> None:
        trigger = MagicMock()
        trigger.id = uuid.uuid4()
        trigger.trigger_type = "cron"
        trigger.active = False
        trigger.cron_expression = None
        trigger.cron_timezone = "UTC"
        trigger.config_json = {}
        trigger.max_concurrent_runs = 1
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_load_trigger_for_update", new=AsyncMock(return_value=trigger)),
            patch.object(ms, "validate_cron_expression", return_value=None),
            patch.object(ms, "compute_next_fire", return_value=_NOW),
            patch.object(ms, "anchor_trigger_streak_epoch", new=AsyncMock(return_value=None)) as mock_anchor,
            patch.object(ms, "clear_trigger_streak_after_reenable", new=AsyncMock(return_value=None)) as mock_clear,
            patch.object(ms, "_streak_status_for", new=AsyncMock(return_value={"state": "ok"})),
        ):
            result = await update_trigger(
                trigger_id=str(trigger.id),
                active=True,
                max_concurrent_runs=2,
                daily_spend_limit=1.0,
                cron_expression="*/5 * * * *",
                cron_timezone="Europe/Berlin",
                config_json={"input_template": {"a": 1}},
            )
        assert result["id"] == str(trigger.id)
        mock_anchor.assert_awaited_once()
        mock_clear.assert_awaited_once()
        assert trigger.cron_timezone == "Europe/Berlin"

    async def test_validate_ongoing_config_change_noop(self) -> None:
        trigger = MagicMock()
        assert await _validate_ongoing_config_change(_mock_session(), trigger, None, None, None, None) is None

    async def test_validate_ongoing_trigger_update_scan_interval_change(self) -> None:
        trigger = MagicMock()
        trigger.trigger_type = "ongoing"
        trigger.config_json = {"scan_interval_seconds": 60}
        pipeline = MagicMock()
        pipeline.max_concurrent_runs = 5
        session = _mock_session()
        session.get.return_value = pipeline
        with patch("modulo.core.trigger_validation.validate_ongoing_config", return_value=None):
            changed, err = await _validate_ongoing_trigger_update(
                session, trigger, None, None, {"scan_interval_seconds": 90}, None, False
            )
        assert changed is True
        assert err is None

    def test_validate_cron_update_requires_expression(self) -> None:
        trigger = MagicMock()
        trigger.cron_expression = None
        next_fire, err = _validate_cron_update(trigger, None, "UTC")
        assert next_fire is None
        assert err is not None
        assert err["error"] == "invalid_cron"

    async def test_delete_trigger_soft_delete_returns_none(self) -> None:
        trigger = MagicMock()
        trigger.pipeline_id = uuid.uuid4()
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=trigger)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.team_scope.pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.trigger.soft_delete_trigger", new=AsyncMock(return_value=None)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await delete_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"

    async def test_delete_trigger_error_envelopes(self) -> None:
        session = _mock_session()
        session.execute.side_effect = ProgrammingError("s", {}, Exception())
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await delete_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "migration_required"
        session2 = _mock_session()
        session2.execute.side_effect = RuntimeError("boom")
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session2)),
        ):
            result = await delete_trigger(trigger_id=str(uuid.uuid4()))
        assert result["error"] == "internal_error"

    async def test_set_org_triggers_paused_audit_failure_swallowed(self) -> None:
        org = MagicMock()
        org.triggers_paused = False
        org.triggers_paused_at = None
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.organisation.get_organisation", new=AsyncMock(return_value=org)),
            patch("modulo.core.audit_logger.append_audit_event", side_effect=ValueError("audit down")),
            patch.object(ms, "_session", return_value=_make_session_context(_mock_session())),
        ):
            result = await set_org_triggers_paused(paused=True)
        assert result["paused"] is True

    async def test_set_org_triggers_paused_error_envelopes(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch(
                "modulo.db.crud.organisation.get_organisation",
                new=AsyncMock(side_effect=ProgrammingError("s", {}, Exception())),
            ),
        ):
            result = await set_org_triggers_paused(paused=True)
        assert result["error"] == "migration_required"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch(
                "modulo.db.crud.organisation.get_organisation",
                new=AsyncMock(side_effect=StarletteHTTPException(404, "no org")),
            ),
        ):
            result = await set_org_triggers_paused(paused=True)
        assert result["error"] == "not_found"
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.organisation.get_organisation", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = await set_org_triggers_paused(paused=True)
        assert result["error"] == "internal_error"

    async def test_list_trigger_events_error_envelopes(self) -> None:
        with patch.object(ms, "_list_trigger_events_impl", side_effect=ProgrammingError("s", {}, Exception())):
            result = await list_trigger_events()
        assert result["error"] == "migration_required"
        with patch.object(ms, "_list_trigger_events_impl", side_effect=RuntimeError("boom")):
            result = await list_trigger_events()
        assert result["error"] == "internal_error"

    async def test_paginate_trigger_events_with_cursor(self) -> None:
        paginator_cls = MagicMock()
        cp = MagicMock()
        cp.items = [MagicMock()]
        cp.next_cursor = "cursor-token"
        cp.has_more = True
        paginator_cls.return_value.paginate = AsyncMock(return_value=cp)
        with patch("modulo.db.crud.pagination.CursorPaginator", paginator_cls):
            items, next_cursor, has_more = await _paginate_trigger_events(
                _mock_session(), MagicMock(), cursor="abc", lim=10
            )
        assert len(items) == 1
        assert next_cursor == "cursor-token"
        assert has_more is True

    async def test_paginate_trigger_events_without_cursor_encodes_next(self) -> None:
        row = MagicMock()
        row.created_at = _NOW
        row.id = uuid.uuid4()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row, MagicMock()]
        session = _mock_session()
        session.execute.return_value = result
        items, next_cursor, has_more = await _paginate_trigger_events(session, MagicMock(), cursor=None, lim=1)
        assert len(items) == 1
        assert has_more is True
        decoded = base64.urlsafe_b64decode(next_cursor.encode()).decode()
        assert decoded == f"{_NOW.isoformat()}:{row.id}"


# ---------------------------------------------------------------------------
# Analytics + run-status helpers
# ---------------------------------------------------------------------------


class TestAnalyticsAndStatusHelpers(_AuthContext):
    def test_deep_link_includes_every_filter(self) -> None:
        pid = uuid.uuid4()
        fid = uuid.uuid4()
        params = AnalyticsParams(
            group_by=AnalyticsGroupBy.DAY,
            trigger_type=AnalyticsTriggerType.MANUAL,
            status=AnalyticsStatus.COMPLETE,
            pipeline_ids=(pid,),
            error_code="harness.unknown",
            folder_id=fid,
        )
        result = {"group_by": "week", "dimension": "status", "date_from": "2026-01-01", "date_to": "2026-01-31"}
        link = _analytics_deep_link(result, params)
        assert link.startswith("/analytics?")
        assert "group_by=week" in link
        assert "dimension=status" in link
        assert "trigger_type=manual" in link
        assert "status=complete" in link
        assert f"pipeline_id={pid}" in link
        assert "error_code=harness.unknown" in link
        assert f"folder_id={fid}" in link
        assert "date_from=2026-01-01" in link
        assert "date_to=2026-01-31" in link

    async def test_query_analytics_impl_degenerate_parse(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_settings", return_value=MagicMock()),
            patch.object(ms, "_require_analytics_feature", new=AsyncMock(return_value=None)),
            patch.object(ms, "_parse_analytics_params", return_value=(None, None)),
            pytest.raises(RuntimeError),
        ):
            await ms._query_analytics_impl(ms._AnalyticsQueryInput())

    async def test_query_analytics_concurrency_impl_degenerate_parse(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_settings", return_value=MagicMock()),
            patch.object(ms, "_require_analytics_feature", new=AsyncMock(return_value=None)),
            patch.object(ms, "_parse_analytics_concurrency_params", return_value=(None, None)),
            pytest.raises(RuntimeError),
        ):
            await ms._query_analytics_concurrency_impl(ms._AnalyticsQueryInput())

    def test_run_status_node_non_dict_usage_and_model_cost(self) -> None:
        node = _run_status_node(
            "n1",
            {"n1": "not-a-dict"},
            {},
            {"n1": {"status": "failed"}},
        )
        assert node["input_tokens"] == 0
        assert node["status"] == "failed"

        node2 = _run_status_node(
            "n2",
            {"n2": {"input_tokens": 3, "output_tokens": 4, "model_cost_display_usd": 0.01}},
            {},
            {},
        )
        assert node2["total_tokens"] == 7
        assert node2["model_cost_display_usd"] == pytest.approx(0.01)

    def test_detect_masked_fields_rejects_non_dict(self) -> None:
        assert not _detect_masked_fields("not-a-dict")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


class TestResourceGaps(_AuthContext):
    async def test_resource_pipeline_snapshots_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            assert "revoked" in await resource_pipeline_snapshots(str(uuid.uuid4()))

    async def test_resource_pipeline_snapshots_invalid_uuid(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await resource_pipeline_snapshots("nope")
        assert result.startswith("error:")

    async def test_resource_pipeline_snapshots_success(self) -> None:
        snap = MagicMock()
        snap.snapshot_version = 2
        snap.id = uuid.uuid4()
        snap.tag = "release"
        snap.created_at = _NOW
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.team_scope.pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.list_snapshots",
                new=AsyncMock(return_value=([snap], 1)),
            ),
        ):
            result = await resource_pipeline_snapshots(str(uuid.uuid4()))
        assert result.startswith("Snapshots for pipeline")
        assert "release" in result

    def test_render_snapshot_node_line(self) -> None:
        line = ms._render_snapshot_node_line(
            {"id": "n1", "node_type": "agent", "agent_id": "a1", "prompt_template": "do things"}
        )
        assert "n1" in line
        assert "do things" in line

    def test_render_snapshot_node_details_full(self) -> None:
        nodes = [
            {
                "id": "n1",
                "agent_prompt": "prompt text",
                "agent_command": "run it",
                "context_files": {"/a.py": "content"},
                "template_id": "opencode",
            },
            {"id": "n2", "agent_prompt": None},
        ]
        rendered = ms._render_snapshot_node_details(nodes)
        assert "agent_prompt: prompt text" in rendered
        assert "agent_command: run it" in rendered
        assert "context_file /a.py: 7 bytes" in rendered
        assert "template_id: opencode" in rendered

    async def test_resource_snapshot_detail_auth_expired(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=False)):
            result = await resource_pipeline_snapshot_detail(str(uuid.uuid4()), str(uuid.uuid4()))
        assert "revoked" in result

    async def test_resource_snapshot_detail_invalid_uuid(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await resource_pipeline_snapshot_detail("nope", str(uuid.uuid4()))
        assert result == "error: Invalid UUID format"

    async def test_resource_snapshot_detail_success(self) -> None:
        snap = MagicMock()
        snap.snapshot_version = 3
        snap.graph_json = {
            "nodes": [
                {"id": "n1", "node_type": "agent", "agent_prompt": "p", "agent_command": "c"},
                {"id": "n2", "node_type": "join"},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n2", "type": "normal"}],
        }
        snap.connector_bindings_json = {}
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.team_scope.pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.get_snapshot_detail",
                new=AsyncMock(return_value=snap),
            ),
        ):
            result = await resource_pipeline_snapshot_detail(str(uuid.uuid4()), str(uuid.uuid4()))
        assert "Snapshot" in result
        assert "Edges (1)" in result
        assert "Full node JSON" in result

    async def test_resource_snapshot_detail_not_found(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.db.crud.team_scope.pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.get_snapshot_detail",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await resource_pipeline_snapshot_detail(str(uuid.uuid4()), str(uuid.uuid4()))
        assert result.startswith("error: Snapshot")

    async def test_resource_run_invalid_uuid(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await resource_run("nope")
        assert result.startswith("error:")

    async def test_resource_run_not_found(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_run", new=AsyncMock(return_value=None)),
        ):
            result = await resource_run(str(uuid.uuid4()))
        assert result.endswith("not found.")

    async def test_resource_run_success_with_breakdown(self) -> None:
        run = MagicMock()
        run.id = uuid.uuid4()
        run.pipeline_id = uuid.uuid4()
        run.status = "complete"
        run.trigger_type = "manual"
        run.created_at = _NOW
        run.owner_team_id = None
        run.error_code = "task_failure"
        run.total_cost_usd = 0.5
        run.cost_breakdown = [{"component": "llm", "amount_usd": 0.5}]
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "get_run", new=AsyncMock(return_value=run)),
            patch("modulo.db.crud.team_scope.pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.run.get_child_run_rollup", new=AsyncMock(return_value={})),
        ):
            result = await resource_run(str(run.id))
        assert f"Run: {run.id}" in result
        assert "harness.worker_failed" in result
        assert "Cost breakdown:" in result

    async def test_resource_schemas_success(self) -> None:
        schema = MagicMock()
        schema.name = "my-schema"
        schema.id = uuid.uuid4()
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalars=[schema])
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await resource_schemas()
        assert result.startswith("Schemas (1):")
        assert "my-schema" in result

    async def test_resource_model_backends_success(self) -> None:
        backend = MagicMock()
        backend.name = "gpt"
        backend.id = uuid.uuid4()
        backend.provider = "openai"
        backend.model_id = "gpt-x"
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalars=[backend])
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await resource_model_backends()
        assert result.startswith("Model Backends (1):")
        assert "openai/gpt-x" in result

    async def test_resource_hitl_gate_invalid_uuid(self) -> None:
        with patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)):
            result = await resource_hitl_gate("nope", "gate")
        assert result.startswith("error:")

    async def test_resource_hitl_gate_not_found(self) -> None:
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=None)
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
        ):
            result = await resource_hitl_gate(str(uuid.uuid4()), "gate")
        assert "not found" in result

    async def test_resource_hitl_gate_success_with_team(self) -> None:
        gate = MagicMock()
        gate.pipeline_id = uuid.uuid4()
        gate.decision = None
        gate.account_id = None
        gate.required_team_id = uuid.uuid4()
        gate.expires_at = _NOW
        run = MagicMock()
        run.owner_team_id = None
        team = MagicMock()
        team.name = "platform"
        session = _mock_session()
        session.execute.side_effect = [
            _make_execute_result(scalar_one_or_none=gate),
            _make_execute_result(scalar_one_or_none=team),
        ]
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "get_run", new=AsyncMock(return_value=run)),
            patch("modulo.db.crud.team_scope.pipeline_owner_team_id", new=AsyncMock(return_value=None)),
        ):
            result = await resource_hitl_gate(str(uuid.uuid4()), "gate")
        assert "Gate: gate" in result
        assert "Required team name: platform" in result
        assert "Claim expires:" in result

    async def test_hitl_required_team_name_unknown_team(self) -> None:
        gate = MagicMock()
        gate.required_team_id = uuid.uuid4()
        session = _mock_session()
        session.execute.return_value = _make_execute_result(scalar_one_or_none=None)
        assert await _hitl_required_team_name(session, gate) is None

    async def test_hitl_required_team_name_none(self) -> None:
        gate = MagicMock()
        gate.required_team_id = None
        assert await _hitl_required_team_name(_mock_session(), gate) is None


# ---------------------------------------------------------------------------
# Remaining small auth-path gaps
# ---------------------------------------------------------------------------


class TestRemainingAuthPathGaps(_AuthContext):
    def test_get_session_factory_builds_shared_factory(self) -> None:
        settings = MagicMock()
        with (
            patch.object(ms, "get_settings", return_value=settings),
            patch.object(ms, "get_or_create_engine", return_value=MagicMock()) as mock_engine,
            patch.object(ms, "get_or_create_session_factory", return_value=MagicMock()) as mock_factory_fn,
        ):
            factory = ms._get_session_factory()
        assert factory is mock_factory_fn.return_value
        mock_engine.assert_called_once_with(settings)

    async def test_bind_connector_defensive_invalid_id(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "check_tool_scope", MagicMock()),
            patch.object(ms, "_parse_uuid_param", return_value=(None, None)),
        ):
            result = await bind_connector_to_node(
                pipeline_id=str(uuid.uuid4()),
                node_id=str(uuid.uuid4()),
                connector_type="t",
                connector_instance_id=str(uuid.uuid4()),
            )
        assert result["error"] == "invalid_id"

    async def test_parse_oauth_form_rejects_malformed_json(self) -> None:
        request = _make_starlette_request(
            method="POST",
            headers={"content-type": "application/json"},
            body=b"{not-json",
        )
        params, err = await ms._parse_oauth_form(request)
        assert params is None
        assert err is not None
        assert err.status_code == 400

    def test_extract_client_credentials_malformed_basic_header(self) -> None:
        request = MagicMock()
        request.headers = {"Authorization": "Basic %%%not-base64%%%"}
        creds, err = ms._extract_oauth_client_credentials(
            request,
            {
                "grant_type": "authorization_code",
                "code": "c",
                "redirect_uri": "r",
                "client_id": "cid",
                "code_verifier": "v",
            },
        )
        assert not creds
        assert err is not None

    async def test_token_impl_degenerate_exchange(self) -> None:
        request = _make_starlette_request(method="POST", path="/mcp/oauth/token")
        with (
            patch(
                "modulo.api.mcp_server._parse_oauth_form",
                new=AsyncMock(return_value=({"grant_type": "authorization_code"}, None)),
            ),
            patch("modulo.api.mcp_server._extract_oauth_client_credentials", return_value=({"code": "c"}, None)),
            patch("modulo.api.mcp_server.get_settings", return_value=MagicMock(modulo_public_url="https://x")),
            patch("modulo.api.mcp_server._exchange_authorization_code", new=AsyncMock(return_value=(None, None))),
            pytest.raises(RuntimeError),
        ):
            await _oauth_token_impl(request)

    def test_extract_refresh_credentials_malformed_basic_header(self) -> None:
        request = MagicMock()
        request.headers = {"Authorization": "Basic %%%not-base64%%%"}
        creds, err = ms._extract_oauth_refresh_credentials(
            request, {"grant_type": "refresh_token", "refresh_token": "r"}
        )
        assert not creds
        assert err is not None

    def test_extract_refresh_credentials_missing_params(self) -> None:
        request = MagicMock()
        request.headers = {}
        creds, err = ms._extract_oauth_refresh_credentials(request, {"grant_type": "refresh_token"})
        assert not creds
        assert err is not None

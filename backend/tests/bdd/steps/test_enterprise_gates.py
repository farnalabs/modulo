"""Step definitions for Team gate enforcement: SSO, RBAC, audit, spend limits."""

import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/licensing/enterprise_gates.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_scalars.first.return_value = None

    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = None
    mock_result.first.return_value = None

    session.execute = AsyncMock(return_value=mock_result)

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.flush = AsyncMock()

    return session


def _make_mock_page(**overrides: Any) -> MagicMock:
    return MagicMock(items=[], total=0, page=1, page_size=20, next_cursor=None, has_more=False, **overrides)


def _setup_client(license_key: str, client: Any, ctx: dict[str, Any]) -> None:
    from modulo.api.dependencies import _get_engine as _eng
    from modulo.api.dependencies import get_db_session, get_plan_context
    from modulo.api.main import app as _app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.core.feature_flags import CommunityTier, LicenseData, LicenseKeyTier
    from modulo.settings import Settings, get_settings

    if license_key == "":
        _plan = CommunityTier()
    else:
        _plan = LicenseKeyTier(
            LicenseData(
                tier="team",
                features=["sso", "team_rbac", "audit_viewer", "admin_spend_limits"],
                expires_at="",
                org_id="",
                raw_payload={},
                raw_key=license_key,
            )
        )

    mock_session = _make_mock_session()

    async def _override_session():
        yield mock_session

    _valid_32 = "a" * 32
    _settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_valid_32,
        fernet_key=_valid_32,
        modulo_admin_password="testpass",
        modulo_license_key=license_key,
    )

    _app.dependency_overrides[get_settings] = lambda: _settings
    _app.dependency_overrides[get_plan_context] = lambda: _plan
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_eng] = lambda: MagicMock()
    _app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )

    get_settings.cache_clear()
    from modulo.core.license import clear_license

    clear_license()


@given(parsers.parse("I do not have a team license"))
def no_team_license(ctx: dict[str, Any]) -> None:
    ctx["license_key"] = ""


@given(parsers.parse("I have a valid team license"))
def valid_team_license(ctx: dict[str, Any]) -> None:
    ctx["license_key"] = "valid-license-key"


@given(parsers.parse("I have an expired team license"))
def expired_team_license(ctx: dict[str, Any]) -> None:
    ctx["license_key"] = ""
    from modulo.core.license import clear_license

    clear_license()
    from modulo.settings import get_settings

    get_settings.cache_clear()


# ── SSO endpoints ─────────────────────────────────────────────────────────


@when(parsers.parse("I GET /api/v1/admin/sso/providers"))
def get_sso_providers(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx.get("license_key", ""), client, ctx)
    resp = client.get("/api/v1/admin/sso/providers")
    _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/teams"))
def get_teams(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx.get("license_key", ""), client, ctx)
    resp = client.get("/api/v1/teams")
    _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/admin/audit"))
def get_admin_audit(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx.get("license_key", ""), client, ctx)
    with (
        patch("modulo.api.routes.audit.set_rls_org"),
        patch(
            "modulo.api.routes.audit.list_audit_events",
            return_value={"items": [], "total": 0, "next_cursor": None, "prev_cursor": None, "limit": 50},
        ),
    ):
        resp = client.get("/api/v1/admin/audit")
        _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/admin/costs/limits"))
def get_admin_costs_limits(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx.get("license_key", ""), client, ctx)
    mock_page = _make_mock_page()
    with (
        patch("modulo.api.routes.costs.set_rls_org"),
        patch("modulo.api.routes.costs.get_organisation", return_value=MagicMock(id=_ORG_ID, daily_spend_limit=None)),
        patch("modulo.api.routes.costs.list_teams", return_value=mock_page),
    ):
        resp = client.get("/api/v1/admin/costs/limits")
        _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/pipelines"))
def get_pipelines(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx.get("license_key", ""), client, ctx)
    mock_page = _make_mock_page()
    with (
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines.list_pipelines", return_value=mock_page),
    ):
        resp = client.get("/api/v1/pipelines")
        _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/changelog"))
def get_changelog(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx.get("license_key", ""), client, ctx)
    resp = client.get("/api/v1/changelog")
    _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/admin/costs"))
def get_admin_costs(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx.get("license_key", ""), client, ctx)
    rows = [{"entity_id": str(_TEAM_ID), "entity_name": "Alpha Team", "total_spend_usd": 150.0, "total_runs": 12}]
    with (
        patch("modulo.api.routes.costs.get_cost_report", return_value=rows),
        patch("modulo.api.routes.costs.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/costs")
        _store_response(request, ctx, resp)


# ── Response assertions ───────────────────────────────────────────────────


@then(parsers.parse('the error detail mentions "{feature}"'))
def error_detail_mentions(feature: str, request: Any) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "")
    assert feature.lower() in detail.lower(), f"Expected detail to mention '{feature}', got '{detail}'"


@then("the response does not contain 402 error")
def response_no_402(request: Any) -> None:
    resp = request.node._resp
    assert resp.status_code != 402, f"Expected non-402 status, got {resp.status_code}"

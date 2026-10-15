"""Unit tests for GET /api/v1/plan — the caller-scoped plan bootstrap (FAR-1806).

The endpoint exists so any authenticated member of their own org can read the
effective plan/tier/feature state without the instance-admin-only
``/api/v1/admin/feature-flags`` / ``/api/v1/admin/license`` endpoints: a
non-admin caller must get 200, the payload must carry the FULL flag set
(inactive flags present with ``currently_active: false``), and the org
``feature_overrides`` overlay must be applied — the two gaps that made the
obvious ``viewmodel/current`` shortcut unusable.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlag
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_VIEWER = AuthenticatedPrincipal(
    username="viewer",
    organisation_id=_ORG_ID,
    account_id=_USER_ID,
    org_role="viewer",
    is_system_admin=False,
)
_ORGLESS_SYSTEM_ADMIN = AuthenticatedPrincipal(
    username="sysadmin",
    organisation_id=None,
    account_id=_USER_ID,
    org_role="",
    is_system_admin=True,
)


def _make_settings(modulo_dev_mode: bool = True) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_dev_mode=modulo_dev_mode,
    )


def _make_org(settings_json: dict[str, object] | None = None) -> MagicMock:
    org = MagicMock()
    org.id = _ORG_ID
    org.name = "Test Org"
    org.settings_json = settings_json if settings_json is not None else {}
    org.daily_spend_limit = None
    return org


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _flag(name: str, *, active: bool, tier: str = "community") -> FeatureFlag:
    return FeatureFlag(
        name=name,
        description=f"{name} description",
        tier=tier,
        currently_active=active,
    )


def _plan_context(flags: list[FeatureFlag], tier: str = "community") -> MagicMock:
    ctx = MagicMock()
    ctx.list_all_features = MagicMock(return_value=flags)
    ctx.tier = MagicMock(return_value=tier)
    return ctx


@contextmanager
def _plan_client_as(
    principal: AuthenticatedPrincipal,
    *,
    authenticated: bool = True,
) -> Generator[TestClient, None, None]:
    """A TestClient wired like the viewmodel unit tests, as ``principal``.

    ``authenticated=False`` leaves ``get_current_user`` un-overridden so the
    real (failing) auth dependency answers the request.
    """
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _patch_plan_deps(org: MagicMock | None, plan_ctx: MagicMock) -> object:
    """Patch the route's DB seams: org load, plan resolution, RLS setters."""
    return patch.multiple(
        "modulo.api.routes.viewmodel",
        get_organisation=AsyncMock(return_value=org),
        resolve_plan_context=AsyncMock(return_value=plan_ctx),
        set_rls_org=AsyncMock(),
        set_rls_user_context=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/plan
# ---------------------------------------------------------------------------


def test_plan_bootstrap_200_for_non_system_admin() -> None:
    """The whole point: a viewer (not a system admin) reads the plan, not a 403."""
    org = _make_org()
    plan_ctx = _plan_context([_flag("eval_system", active=True), _flag("mobile_sidebar_rail", active=False)])

    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(org, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    body = resp.json()
    assert body["license"]["tier"] == "community"
    assert body["dev_mode"] is True
    names = [f["name"] for f in body["flags"]]
    assert "eval_system" in names
    assert "mobile_sidebar_rail" in names


def test_plan_bootstrap_includes_inactive_flags_with_currently_active_false() -> None:
    """Inactive flags must be PRESENT (an active-only payload is the bug being fixed)."""
    org = _make_org()
    plan_ctx = _plan_context([_flag("eval_system", active=True), _flag("mobile_sidebar_rail", active=False)])

    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(org, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()["flags"]}
    mobile = by_name["mobile_sidebar_rail"]
    assert mobile["currently_active"] is False
    assert mobile["description"]
    assert mobile["tier"] == "community"
    assert by_name["eval_system"]["currently_active"] is True


def test_plan_bootstrap_applies_org_feature_overrides() -> None:
    """Org overrides flip BOTH directions: default-off -> on, default-on -> off."""
    org = _make_org(
        settings_json={
            "feature_overrides": {"mobile_sidebar_rail": True, "analytics_page": False},
        }
    )
    plan_ctx = _plan_context(
        [
            _flag("mobile_sidebar_rail", active=False),
            _flag("analytics_page", active=True),
        ]
    )

    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(org, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()["flags"]}
    assert by_name["mobile_sidebar_rail"]["currently_active"] is True
    assert by_name["analytics_page"]["currently_active"] is False


def test_plan_bootstrap_ignores_non_bool_and_unknown_overrides() -> None:
    """Only bool entries naming KNOWN flags overlay — junk settings never flip a flag."""
    org = _make_org(
        settings_json={
            "feature_overrides": {"analytics_page": 1, "no_such_flag": True},
        }
    )
    plan_ctx = _plan_context([_flag("analytics_page", active=True)])

    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(org, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()["flags"]}
    # 1 is not a bool -> not an override; unknown names are ignored entirely.
    assert by_name["analytics_page"]["currently_active"] is True
    assert "no_such_flag" not in by_name


def test_plan_bootstrap_license_block_reports_expiry() -> None:
    """The licence view (expires_at/org_id) rides the same payload so the client
    can drop its admin-only /admin/license call."""
    from modulo.core.license import LicenseData

    org = _make_org()
    plan_ctx = _plan_context([_flag("eval_system", active=True)], tier="team")
    licence = LicenseData(
        tier="team",
        features=["sso"],
        expires_at="2026-12-31T23:59:59Z",
        org_id="Acme Corp",
        raw_payload={},
        raw_key="signed-key",
    )

    with (
        _plan_client_as(_VIEWER) as client,
        _patch_plan_deps(org, plan_ctx),
        patch("modulo.core.license.get_license", return_value=licence),
    ):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    license_block = resp.json()["license"]
    assert license_block["tier"] == "team"
    assert license_block["expires_at"] == "2026-12-31T23:59:59Z"
    assert license_block["org_id"] == "Acme Corp"


def test_plan_bootstrap_unauthenticated_returns_4xx() -> None:
    with _plan_client_as(_VIEWER, authenticated=False) as client:
        resp = client.get("/api/v1/plan")
    assert resp.status_code in (401, 403)


def test_plan_bootstrap_orgless_system_admin_still_resolves() -> None:
    """A system admin with no org still gets a payload — no overlay, no crash."""
    plan_ctx = _plan_context([_flag("mobile_sidebar_rail", active=False)])

    with _plan_client_as(_ORGLESS_SYSTEM_ADMIN) as client, _patch_plan_deps(None, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()["flags"]}
    assert by_name["mobile_sidebar_rail"]["currently_active"] is False

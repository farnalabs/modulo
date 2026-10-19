"""FAR-1978: the pre-auth SSO provider list must never expose another tenant.

``GET /api/v1/auth/sso/providers`` is pre-auth by necessity — the login page
calls it before any principal exists — so it can never be gated. Its remedy is
scoping: only the single-org login path may see org-level provider rows, and
even then only that one org's. A multi-org instance must answer with
environment-level configuration alone, because ``display_name`` is admin-chosen
free text that can name a tenant.

These tests call the route anonymously (no ``Authorization`` header, exactly
what a browser sends from the logged-out login page) and assert on the wire
response — the only thing an anonymous caller can observe.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_system_db_session
from modulo.api.main import app
from modulo.settings import get_settings

_VALID_32 = "a" * 32
_SSO_PROVIDERS_URL = "/api/v1/auth/sso/providers"


def _make_settings() -> Any:
    from modulo.settings import Settings

    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
    )


def _org(slug: str) -> MagicMock:
    org = MagicMock()
    org.slug = slug
    org.name = f"{slug.title()} Inc"
    org.id = f"org-{slug}"
    return org


def _provider(provider_id: str, display_name: str, org: MagicMock) -> MagicMock:
    provider = MagicMock()
    provider.provider_id = provider_id
    provider.name = display_name
    provider.preset = "custom"
    provider.provider_type = "oidc"
    provider.enabled = True
    provider.organisation_id = org.id
    return provider


def _scalars_result(rows: list[MagicMock]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    return result


def _make_session(*, orgs: list[MagicMock] | None = None, providers: list[MagicMock] | None = None) -> AsyncMock:
    """Mock an ``AsyncSession`` whose ``execute`` answers by table.

    The handler reads organisations (login-active resolution) and
    sso_providers (provider rows) on the same session, so the result must be
    selected per statement rather than per call order.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.info = {}
    session.in_transaction = MagicMock(return_value=True)

    async def _execute(statement: Any, *args: Any, **kwargs: Any) -> MagicMock:
        sql = str(statement)
        if "FROM organisations" in sql:
            return _scalars_result(orgs or [])
        if "FROM sso_providers" in sql:
            return _scalars_result(providers or [])
        return _scalars_result([])

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _plan() -> MagicMock:
    plan = MagicMock()
    plan.feature_enabled.return_value = True
    return plan


def _anonymous_client(app_session: AsyncMock, system_session: AsyncMock) -> TestClient:
    """Anonymous TestClient (no credentials) wired to the given sessions."""

    async def _override_app_session() -> AsyncGenerator[AsyncMock, None]:
        yield app_session

    async def _override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield system_session

    app.dependency_overrides[get_db_session] = _override_app_session
    app.dependency_overrides[get_system_db_session] = _override_system_session
    app.dependency_overrides[get_settings] = _make_settings
    return TestClient(app)


def _get_providers(client: TestClient, *, env_providers: list[dict[str, str]] | None = None) -> Any:
    try:
        with (
            patch("modulo.api.routes.sso._anonymous_plan_context", new=AsyncMock(return_value=_plan())),
            patch("modulo.api.routes.sso.parse_oidc_providers", return_value=env_providers or []),
        ):
            return client.get(_SSO_PROVIDERS_URL)
    finally:
        app.dependency_overrides.clear()


class TestMultiOrgInstanceHidesOrgProviders:
    def test_anonymous_response_lists_no_org_provider_names(self) -> None:
        """Two login-active orgs -> env-only response; neither tenant is named.

        This is the FAR-1978 regression test: before the scoping fix the
        handler returned BOTH orgs' providers to an anonymous caller.
        """
        org_a = _org("acme")
        org_b = _org("globex")
        app_session = _make_session(orgs=[org_a, org_b])
        system_session = _make_session(
            providers=[
                _provider("acme-idp", "Acme Corp SSO", org_a),
                _provider("globex-idp", "Globex SSO", org_b),
            ]
        )
        client = _anonymous_client(app_session, system_session)

        resp = _get_providers(client)

        assert resp.status_code == 200
        assert not resp.json()["oidc"]
        assert "Acme Corp SSO" not in resp.text
        assert "Globex SSO" not in resp.text

    def test_zero_login_active_orgs_lists_no_org_provider_names(self) -> None:
        """Unconfigured instance (no login-active org) -> env-only response."""
        org_a = _org("acme")
        app_session = _make_session(orgs=[])
        system_session = _make_session(providers=[_provider("acme-idp", "Acme Corp SSO", org_a)])
        client = _anonymous_client(app_session, system_session)

        resp = _get_providers(client)

        assert resp.status_code == 200
        assert not resp.json()["oidc"]
        assert "Acme Corp SSO" not in resp.text

    def test_multi_org_instance_still_advertises_env_providers(self) -> None:
        """Environment-level providers are instance config, not tenant data."""
        org_a = _org("acme")
        org_b = _org("globex")
        client = _anonymous_client(
            _make_session(orgs=[org_a, org_b]),
            _make_session(providers=[_provider("acme-idp", "Acme Corp SSO", org_a)]),
        )

        resp = _get_providers(client, env_providers=[{"provider_id": "env-idp"}])

        assert resp.status_code == 200
        assert resp.json()["oidc"] == [{"provider_id": "env-idp", "display_name": "env-idp", "preset": "custom"}]
        assert "Acme Corp SSO" not in resp.text


class TestSingleOrgInstanceKeepsItsOwnProviders:
    def test_single_org_lists_its_providers_and_filters_siblings(self) -> None:
        """The single-org login path keeps working, and only sees its own org."""
        org_a = _org("acme")
        org_b = _org("globex")
        app_session = _make_session(orgs=[org_a])
        system_session = _make_session(
            providers=[
                _provider("acme-idp", "Acme Corp SSO", org_a),
                _provider("globex-idp", "Globex SSO", org_b),
            ]
        )
        client = _anonymous_client(app_session, system_session)

        resp = _get_providers(client)

        assert resp.status_code == 200
        assert resp.json()["oidc"] == [{"provider_id": "acme-idp", "display_name": "Acme Corp SSO", "preset": "custom"}]
        assert "Globex SSO" not in resp.text

    def test_app_session_fallback_is_scoped_to_the_single_org(self) -> None:
        """Unprovisioned system role (zero rows) -> app fallback is org-scoped.

        The fallback used to bind RLS to the FIRST org, which is not
        necessarily the resolved login org; it must bind to the same org the
        scoping decision resolved.
        """
        org_a = _org("acme")
        org_b = _org("globex")
        app_session = _make_session(
            orgs=[org_a],
            providers=[
                _provider("acme-idp", "Acme Corp SSO", org_a),
                _provider("globex-idp", "Globex SSO", org_b),
            ],
        )
        # System role unprovisioned -> the global read returns zero rows.
        system_session = _make_session(providers=[])
        client = _anonymous_client(app_session, system_session)

        resp = _get_providers(client)

        assert resp.status_code == 200
        assert resp.json()["oidc"] == [{"provider_id": "acme-idp", "display_name": "Acme Corp SSO", "preset": "custom"}]
        assert "Globex SSO" not in resp.text

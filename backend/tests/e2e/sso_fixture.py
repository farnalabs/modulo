"""Deterministic local IdP fixture serving the production FastAPI SSO routes.

This process imports the production app and overrides only dependency/provider
seams. The OIDC login and callback handlers, middleware, cookies and exception
handlers remain the production implementations used by the browser journey.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import uvicorn
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from modulo.api.dependencies import (
    get_anonymous_plan_context,
    get_db_session,
    get_system_db_session,
)
from modulo.api.main import app
from modulo.api.routes import org_login as org_login_routes
from modulo.api.routes import sso as sso_routes
from modulo.auth import sso as sso_auth
from modulo.core.feature_flags import FeatureFlag
from modulo.settings import get_settings

_LOG = logging.getLogger("e2e.sso_fixture")
_FIXTURE_PROVIDER = "ci-idp"
_FIXTURE_EMAIL = "sso-user@example.com"
_FIXTURE_ACCESS_TOKEN = "fixture-sso-access-token"
_FIXTURE_REFRESH_TOKEN = "fixture-sso-refresh-token"


class _FixtureSession:
    """Minimal async session seam for route transaction boundaries."""

    def begin(self) -> _FixtureTransaction:
        return _FixtureTransaction()


class _FixtureTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FixturePlan:
    def feature_enabled(self, name: str) -> bool:
        return name == "sso"

    def list_enabled_features(self) -> list[FeatureFlag]:
        return []

    def list_all_features(self) -> list[FeatureFlag]:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


@dataclass(frozen=True)
class _FixtureOrg:
    id: str = "00000000-0000-0000-0000-000000000001"
    slug: str = "default"
    name: str = "Acme"


_FIXTURE_SESSION = _FixtureSession()
_FIXTURE_ORG = _FixtureOrg()
_FIXTURE_PLAN = _FixturePlan()


async def _session_override() -> Any:
    yield _FIXTURE_SESSION


async def _plan_override() -> _FixturePlan:
    return _FIXTURE_PLAN


async def _resolve_login_tenant(_session: Any, _settings: Any) -> tuple[bool, _FixtureOrg]:
    return True, _FIXTURE_ORG


async def _resolve_anonymous_plan_with_tenant(_settings: Any, _session: Any) -> tuple[_FixturePlan, _FixtureOrg]:
    return _FIXTURE_PLAN, _FIXTURE_ORG


async def _list_enabled_oidc_for_org(_system: Any, _session: Any, _org_id: Any) -> list[Any]:
    return [
        SimpleNamespace(
            provider_id=_FIXTURE_PROVIDER,
            organisation_id=_FIXTURE_ORG.id,
            name="CI Identity Provider",
            preset="custom",
        )
    ]


async def _is_saml_available(*_args: Any, **_kwargs: Any) -> bool:
    return False


async def _authorize_url_for_route(
    provider_id: str,
    settings: Any,
    redirect_uri: str,
    _system_session: Any,
    _app_session: Any,
) -> tuple[str, str, str, str]:
    raw_state = "fixture-raw-state"
    nonce = "fixture-nonce"
    verifier = "fixture-code-verifier"
    signed_state = sso_auth.sign_state(f"{provider_id}:{raw_state}", settings.secret_key)
    idp_base = os.environ.get("E2E_IDP_BASE_URL", "http://127.0.0.1:8000")
    auth_url = f"{idp_base}/test-idp/authorize?" + urlencode(
        {"client_id": provider_id, "redirect_uri": redirect_uri, "state": signed_state, "mode": "success"}
    )
    return auth_url, raw_state, nonce, verifier


async def _process_callback(
    code: str,
    _state: str,
    _settings: Any,
    _system_session: Any,
    _app_session: Any,
    _redirect_uri: str,
    *,
    expected_nonce: str,
    code_verifier: str,
) -> dict[str, str]:
    if expected_nonce != "fixture-nonce" or code_verifier != "fixture-code-verifier":
        raise ValueError("fixture flow binding mismatch")
    if code == "ci-invalid-code":
        raise ValueError("fixture IdP rejected authorization code")
    if code != "ci-auth-code":
        raise ValueError("fixture IdP rejected authorization code")
    return {"access_token": _fixture_access_token(), "refresh_token": _FIXTURE_REFRESH_TOKEN}


async def _fixture_idp_authorize(request: Request) -> RedirectResponse | PlainTextResponse:
    redirect_uri = request.query_params.get("redirect_uri")
    state = request.query_params.get("state")
    mode = request.query_params.get("mode", "success")
    if not redirect_uri or not state:
        return PlainTextResponse("fixture IdP missing redirect_uri/state", status_code=400)
    code = "ci-invalid-code" if mode == "failure" else "ci-auth-code"
    separator = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{separator}code={code}&state={state}"
    return PlainTextResponse(
        f'<a href="{location}">Continue with fixture IdP</a>',
        media_type="text/html",
    )


def _fixture_access_token() -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "sub": _FIXTURE_EMAIL,
                    "org_id": "00000000-0000-0000-0000-000000000001",
                    "account_id": "00000000-0000-0000-0000-000000000002",
                    "org_role": "admin",
                    "is_system_admin": False,
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.fixture-signature"


def _install_fixture() -> None:
    """Install narrowly-scoped seams while retaining production route wiring."""
    app.dependency_overrides[get_db_session] = _session_override
    app.dependency_overrides[get_system_db_session] = _session_override
    app.dependency_overrides[get_anonymous_plan_context] = _plan_override

    org_login_routes.resolve_login_tenant = _resolve_login_tenant
    sso_routes.resolve_anonymous_plan_with_tenant = _resolve_anonymous_plan_with_tenant
    sso_routes._list_enabled_oidc_for_org = _list_enabled_oidc_for_org
    sso_routes.is_saml_available = _is_saml_available
    sso_routes.oidc_get_authorize_url = _authorize_url_for_route
    sso_routes.oidc_process_callback = _process_callback

    fixture_router = APIRouter()
    fixture_router.add_api_route(
        "/test-idp/authorize",
        _fixture_idp_authorize,
        methods=["GET"],
        response_model=None,
    )
    app.include_router(fixture_router)
    _LOG.info("SSO fixture installed: production routes + deterministic IdP seam")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    args = parser.parse_args()
    _install_fixture()
    settings = get_settings()
    settings.modulo_frontend_url = args.frontend_url
    settings.modulo_public_url = "http://127.0.0.1:8000"
    uvicorn.run(app, host="127.0.0.1", port=args.port, lifespan="off", log_level="warning")

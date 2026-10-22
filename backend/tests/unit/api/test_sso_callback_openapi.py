"""FAR-2012: the SSO callback routes publish the redirects they actually send.

``oidc_callback`` and both SAML ACS routes redirect the BROWSER — a 307 to the
SPA callback on success and a 303 to the login page on a handled failure — but
declared neither a ``status_code`` nor ``responses``, so FastAPI published them
as ``200 application/json`` and the generated frontend client
(``frontend/src/lib/api/schema.ts``) was built on that fiction.

These tests pin BOTH sides of the contract:

* the OpenAPI document must describe the redirects (and no JSON 200), and
* the statuses the handlers actually produce when driven must be a subset of
  what the document declares,

so a drift on either side fails here rather than shipping a false schema.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.api.main import app
from modulo.api.routes import sso
from modulo.auth.sso import SsoCallbackError, sign_state
from modulo.settings import Settings

_VALID_32 = "a" * 32
_FERNET_KEY = "b" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_OIDC_CALLBACK = "/api/v1/auth/oidc/{provider}/callback"
_SAML_ACS_PROVIDER = "/api/v1/auth/saml/acs/{provider_id}"
_SAML_ACS = "/api/v1/auth/saml/acs"

#: (OpenAPI path, HTTP method) for every browser-facing SSO callback route.
_CALLBACK_ROUTES = (
    (_OIDC_CALLBACK, "get"),
    (_SAML_ACS_PROVIDER, "post"),
    (_SAML_ACS, "post"),
)

_SUCCESS_TOKENS = {"access_token": "access-token", "refresh_token": "refresh-token"}


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _operation(path: str, method: str) -> dict[str, Any]:
    return app.openapi()["paths"][path][method]


def _documented_statuses(path: str, method: str) -> set[str]:
    return set(_operation(path, method)["responses"])


class _FormRequest:
    """Minimal ``Request`` double for the SAML ACS POST body."""

    def __init__(self, form: dict[str, object]) -> None:
        self._form = form
        # FAR-1516: the ACS routes publish the audit org onto ``request.state``.
        self.state = SimpleNamespace()

    async def form(self) -> dict[str, object]:
        return self._form


def _oidc_callback_request(settings: Settings) -> SimpleNamespace:
    """A complete OIDC callback request: code, signed state and bound flow cookie."""
    raw_state = "raw-state"
    return SimpleNamespace(
        query_params={"code": "c", "state": sign_state(f"p:{raw_state}", settings.secret_key)},
        cookies={sso._OIDC_FLOW_COOKIE: sign_state(f"{raw_state}:nonce:verifier", settings.secret_key)},
    )


# ---------------------------------------------------------------------------
# Drive the handlers and report the status they actually produce.
# ---------------------------------------------------------------------------


async def _oidc_callback_success_status() -> int:
    settings = _settings()
    with patch.object(sso, "_run_sso_callback_process", AsyncMock(return_value=dict(_SUCCESS_TOKENS))):
        response = await sso.oidc_callback(
            provider="p",
            request=_oidc_callback_request(settings),
            _=None,
            settings=settings,
            session=SimpleNamespace(),
            system_session=SimpleNamespace(),
        )
    return response.status_code


async def _oidc_callback_failure_status() -> int:
    # No code/state -> SsoCallbackError -> the registered 303 login redirect.
    with pytest.raises(SsoCallbackError) as excinfo:
        await sso.oidc_callback(
            provider="p",
            request=SimpleNamespace(query_params={}, cookies={}),
            _=None,
            settings=_settings(),
            session=SimpleNamespace(),
            system_session=SimpleNamespace(),
        )
    return excinfo.value.status_code


async def _saml_acs_provider_success_status() -> int:
    provider = SimpleNamespace(organisation_id=_ORG_ID)
    with (
        patch.object(sso, "_resolve_saml_for_route", AsyncMock(return_value=provider)),
        patch.object(sso, "_run_sso_callback_process", AsyncMock(return_value=dict(_SUCCESS_TOKENS))),
    ):
        response = await sso.saml_acs_provider(
            provider_id="p",
            request=_FormRequest({"SAMLResponse": "assertion"}),
            _=None,
            settings=_settings(),
            session=SimpleNamespace(),
            system_session=SimpleNamespace(),
        )
    return response.status_code


async def _saml_acs_provider_failure_status() -> int:
    # An unknown/disabled slug raises the uniform 404, which the ACS maps to 303.
    with (
        patch.object(sso, "_resolve_saml_for_route", AsyncMock(side_effect=sso._SAML_NOT_FOUND)),
        pytest.raises(SsoCallbackError) as excinfo,
    ):
        await sso.saml_acs_provider(
            provider_id="p",
            request=_FormRequest({"SAMLResponse": "assertion"}),
            _=None,
            settings=_settings(),
            session=SimpleNamespace(),
            system_session=SimpleNamespace(),
        )
    return excinfo.value.status_code


async def _saml_acs_success_status() -> int:
    with patch.object(sso, "_run_sso_callback_process", AsyncMock(return_value=dict(_SUCCESS_TOKENS))):
        response = await sso.saml_acs(
            request=_FormRequest({"SAMLResponse": "assertion"}),
            _=None,
            settings=_settings(),
            session=SimpleNamespace(),
            # None skips the legacy provider-resolution read; the process is mocked.
            system_session=None,
        )
    return response.status_code


async def _saml_acs_failure_status() -> int:
    # A body with no SAMLResponse -> SsoCallbackError -> the 303 login redirect.
    with pytest.raises(SsoCallbackError) as excinfo:
        await sso.saml_acs(
            request=_FormRequest({}),
            _=None,
            settings=_settings(),
            session=SimpleNamespace(),
            system_session=None,
        )
    return excinfo.value.status_code


# ---------------------------------------------------------------------------
# The document must describe redirects, never a JSON 200.
# ---------------------------------------------------------------------------


class TestCallbackOpenApiContract:
    @pytest.mark.parametrize(("path", "method"), _CALLBACK_ROUTES)
    def test_no_callback_documents_a_json_200(self, path: str, method: str) -> None:
        responses = _operation(path, method)["responses"]

        assert "200" not in responses, f"{path} still documents a 200 response"
        assert "307" in responses, f"{path} does not document its success redirect"
        assert "303" in responses, f"{path} does not document its failure redirect"

    @pytest.mark.parametrize(("path", "method"), _CALLBACK_ROUTES)
    def test_redirects_carry_no_json_body(self, path: str, method: str) -> None:
        responses = _operation(path, method)["responses"]

        for status_code in ("307", "303"):
            content = responses[status_code].get("content", {})
            assert "application/json" not in content, f"{path} {status_code} declares a JSON body"

    @pytest.mark.parametrize(("path", "method"), _CALLBACK_ROUTES)
    def test_redirects_document_their_location_header(self, path: str, method: str) -> None:
        responses = _operation(path, method)["responses"]

        for status_code in ("307", "303"):
            headers = responses[status_code].get("headers", {})
            assert "Location" in headers, f"{path} {status_code} does not document Location"


# ---------------------------------------------------------------------------
# Every status the handler produces must be declared (and vice versa for the
# two redirects). If a decorator drops its `responses` map the 303 disappears
# from the document and these fail.
# ---------------------------------------------------------------------------


class TestDocumentedStatusesCoverTheHandlers:
    async def test_oidc_callback(self) -> None:
        documented = _documented_statuses(_OIDC_CALLBACK, "get")

        success = await _oidc_callback_success_status()
        failure = await _oidc_callback_failure_status()

        assert success == 307
        assert failure == 303
        assert {str(success), str(failure)} <= documented

    async def test_saml_acs_provider(self) -> None:
        documented = _documented_statuses(_SAML_ACS_PROVIDER, "post")

        success = await _saml_acs_provider_success_status()
        failure = await _saml_acs_provider_failure_status()

        assert success == 307
        assert failure == 303
        assert {str(success), str(failure)} <= documented

    async def test_saml_acs(self) -> None:
        documented = _documented_statuses(_SAML_ACS, "post")

        success = await _saml_acs_success_status()
        failure = await _saml_acs_failure_status()

        assert success == 307
        assert failure == 303
        assert {str(success), str(failure)} <= documented

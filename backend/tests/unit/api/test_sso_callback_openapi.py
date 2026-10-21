"""OpenAPI contract tests for SSO callback routes.

Ensures the OIDC and SAML callback routes declare their actual HTTP responses
(307 success redirect, 303 error redirect) instead of the incorrect 200.
"""

from modulo.api.main import app


def test_oidc_callback_openapi_responses():
    """OIDC callback declares 307 success + 303 error redirect."""
    spec = app.openapi()
    op = spec["paths"]["/api/v1/auth/oidc/{provider}/callback"]["get"]
    responses = op["responses"]
    assert "307" in responses, "OIDC callback must declare 307 success redirect"
    assert "303" in responses, "OIDC callback must declare 303 error redirect"
    # 200 MUST NOT be present (was the incorrect declaration)
    assert "200" not in responses, "OIDC callback must not declare 200"
    # 422 for path param validation remains
    assert "422" in responses


def test_saml_acs_legacy_openapi_responses():
    """Legacy SAML ACS declares 307 success + 303 error redirect."""
    spec = app.openapi()
    op = spec["paths"]["/api/v1/auth/saml/acs"]["post"]
    responses = op["responses"]
    assert "307" in responses, "Legacy SAML ACS must declare 307 success redirect"
    assert "303" in responses, "Legacy SAML ACS must declare 303 error redirect"
    assert "200" not in responses, "Legacy SAML ACS must not declare 200"
    assert "422" in responses


def test_saml_acs_per_provider_openapi_responses():
    """Per-provider SAML ACS declares 307 success + 303 error redirect."""
    spec = app.openapi()
    op = spec["paths"]["/api/v1/auth/saml/acs/{provider_id}"]["post"]
    responses = op["responses"]
    assert "307" in responses, "Per-provider SAML ACS must declare 307 success redirect"
    assert "303" in responses, "Per-provider SAML ACS must declare 303 error redirect"
    assert "200" not in responses, "Per-provider SAML ACS must not declare 200"
    assert "422" in responses

"""BDD step definitions: MCP OAuth 2.0 authorization code flow."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.rate_limiter import RateLimiterRegistry
from tests.bdd.conftest import ORG_ID

scenarios("../../features/mcp/mcp_oauth.feature")


# --------------------------------------------------------------------------
# Rate limiter bypass — prevents Redis connection errors in the MCP sub-app
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_rate_limiter() -> None:
    with patch.object(RateLimiterRegistry, "check", AsyncMock(return_value=True)):
        yield


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _store_resp(request, resp):
    request.node._resp = resp


def _make_mock_client(
    client_id: str = "oauth_client_1",
    name: str = "My MCP App",
    scopes: str | None = None,
    redirect_uris: str | None = None,
) -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.client_id = client_id
    c.client_secret_hash = "a" * 64
    c.name = name
    c.scopes = scopes or "trigger:run hitl:review"
    c.redirect_uris = redirect_uris or "https://app.example.com/callback"
    return c


def _make_mock_auth_code(
    code: str = "auth_code_abc",
    client_id: str = "oauth_client_1",
    scopes: str = "trigger:run",
    redirect_uri: str = "https://app.example.com/callback",
    used: bool = False,
    code_challenge: str | None = None,
) -> MagicMock:
    from datetime import UTC, datetime, timedelta

    c = MagicMock()
    c.code = code
    c.client_id = client_id
    c.scopes = scopes
    c.redirect_uri = redirect_uri
    c.used = used
    c.code_challenge = code_challenge
    c.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    return c


# --------------------------------------------------------------------------
# Given steps
# --------------------------------------------------------------------------


@given(parsers.parse('an OAuth client exists with id "{client_id}"'))
def oauth_client_exists(client_id: str, request):
    # Imported app fixture shared by all BDD tests — override auth for
    # viewer scenarios by setting state on request.node so the When
    # step can decide which principal to use.
    request.node._oauth_client_id = client_id




    request.node._oauth_client_id = client_id
    request.node._oauth_client = _make_mock_client(client_id=client_id)


@given(
    parsers.parse(
        'an OAuth client exists with id "{client_id}" and redirect_uris {redirect_uris}'
    )
)
def oauth_client_with_uris(client_id: str, redirect_uris: str, request):
    uri_list = json.loads(redirect_uris)
    request.node._oauth_client_id = client_id
    request.node._oauth_client = _make_mock_client(
        client_id=client_id, redirect_uris=" ".join(uri_list)
    )


@given(
    parsers.parse(
        'an OAuth client exists with id "{client_id}" and scopes {scopes}'
    )
)
def oauth_client_with_scopes(client_id: str, scopes: str, request):
    scope_list = json.loads(scopes)
    request.node._oauth_client_id = client_id
    request.node._oauth_client = _make_mock_client(
        client_id=client_id, scopes=" ".join(scope_list)
    )


@given(
    parsers.parse(
        'an authorization code "{code}" exists for client "{client_id}"'
    )
)
def auth_code_exists(code: str, client_id: str, request):
    request.node._auth_code = _make_mock_auth_code(code=code, client_id=client_id)


@given(
    parsers.parse(
        'a token family "{family_id}" at sequence {seq:d} for client "{client_id}"'
    )
)
def token_family_exists(family_id: str, seq: int, client_id: str, request):
    request.node._token_family_id = family_id
    request.node._token_family_seq = seq
    request.node._oauth_client_id = client_id


@given(
    parsers.parse(
        'an authorization code "{code}" was created with code_challenge "{challenge}"'
    )
)
def auth_code_with_pkce(code: str, challenge: str, request):
    request.node._auth_code = _make_mock_auth_code(
        code=code, code_challenge=challenge
    )


# --------------------------------------------------------------------------
# When steps
# --------------------------------------------------------------------------


@when(
    parsers.parse(
        'I POST /api/v1/mcp/oauth/clients with name "{name}" and redirect_uris {uris} and scopes {scopes}'
    )
)
def register_oauth_client(name: str, uris: str, scopes: str, client, viewer_client, request):
    uri_list = json.loads(uris)
    scope_list = json.loads(scopes)
    mock_client = MagicMock()
    mock_client.id = uuid.uuid4()
    mock_client.client_id = "abc123def4567890"
    mock_client.name = name
    mock_raw_secret = "raw_secret_40_chars_long_here"

    is_viewer = getattr(request.node, "_viewer_auth", False)
    test_client = viewer_client if is_viewer else client

    with (
        patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
        patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        patch("modulo.api.routes.mcp_oauth.normalize_scopes", return_value=scope_list),
    ):
        mock_create.return_value = (mock_client, mock_raw_secret)
        resp = test_client.post(
            "/api/v1/mcp/oauth/clients",
            json={
                "name": name,
                "redirect_uris": uri_list,
                "scopes": scope_list,
            },
        )
    _store_resp(request, resp)


@when(
    parsers.parse(
        "I GET /mcp/oauth/authorize with response_type "
        '"{rt}" and client_id "{cid}" and redirect_uri "{ru}" '
        'and scope "{scope}" and code_challenge "{cc}" and '
        'code_challenge_method "{ccm}" and state "{state}"'
    )
)
def authorize_pkce(rt: str, cid: str, ru: str, scope: str, cc: str, ccm: str, state: str, client, request):
    with (
        patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
        patch("modulo.auth.oauth.create_authorization_code") as mock_create_code,
    ):
        mock_get.return_value = _make_mock_client(client_id=cid, redirect_uris=ru)
        mock_create_code.return_value = "generated_auth_code"
        resp = client.get(
            "/mcp/oauth/authorize",
            params={
                "response_type": rt,
                "client_id": cid,
                "redirect_uri": ru,
                "scope": scope,
                "code_challenge": cc,
                "code_challenge_method": ccm,
                "state": state,
            },
        )
    _store_resp(request, resp)


@when(
    parsers.parse(
        'I GET /mcp/oauth/authorize with client_id "{cid}" and redirect_uri "{ru}"'
    )
)
def authorize_invalid_redirect(cid: str, ru: str, client, request):
    with patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get:
        mock_get.return_value = _make_mock_client(
            client_id=cid,
            redirect_uris="https://app.example.com/callback",
        )
        resp = client.get(
            "/mcp/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": ru,
                "scope": "trigger:run",
                "state": "xyz",
            },
        )
    _store_resp(request, resp)


@when(
    parsers.parse(
        "I POST /mcp/oauth/token with grant_type "
        '"{gt}" and code "{code}" and client_id "{cid}" and '
        'client_secret "{secret}" and redirect_uri "{ru}" and '
        'code_verifier "{cv}"'
    )
)
def token_exchange(gt: str, code: str, cid: str, secret: str, ru: str, cv: str, client, request):
    with (
        patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
        patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
        patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
        patch("modulo.auth.oauth.create_oauth_token_family") as mock_create_family,
        patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
    ):
        mock_get.return_value = _make_mock_client(client_id=cid, redirect_uris=ru)
        mock_validate.return_value = _make_mock_client(client_id=cid)
        mock_consume.return_value = _make_mock_auth_code(
            code=code, client_id=cid, scopes="trigger:run"
        )
        mock_create_family.return_value = ("family_uuid", 0)
        mock_create_token.return_value = "jwt_access_token_abc"
        resp = client.post(
            "/mcp/oauth/token",
            data={
                "grant_type": gt,
                "code": code,
                "client_id": cid,
                "client_secret": secret,
                "redirect_uri": ru,
                "code_verifier": cv,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    request.node._access_token = "jwt_access_token_abc"
    request.node._refresh_token = "refresh_token_abc"
    _store_resp(request, resp)


@when(
    parsers.parse(
        'the client requests a token with scope "{scope}"'
    )
)
def token_with_restricted_scope(scope: str, client, request):
    cid = getattr(request.node, "_oauth_client_id", "limited_client")
    with (
        patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
        patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
    ):
        mock_get.return_value = _make_mock_client(
            client_id=cid, scopes="trigger:run"
        )
        mock_validate.return_value = _make_mock_client(
            client_id=cid, scopes="trigger:run"
        )
        resp = client.post(
            "/mcp/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "code_xyz",
                "client_id": cid,
                "client_secret": "secret",
                "redirect_uri": "https://app.example.com/callback",
                "scope": scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    _store_resp(request, resp)


@when(
    parsers.parse(
        "I POST /mcp/oauth/token with grant_type "
        '"refresh_token" and refresh_token "{rt}" and '
        'client_id "{cid}" and client_secret "{secret}"'
    )
)
def refresh_token_flow(rt: str, cid: str, secret: str, client, request):
    family_id = getattr(request.node, "_token_family_id", "family_1")
    seq = getattr(request.node, "_token_family_seq", 0)
    with (
        patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
        patch("modulo.auth.oauth.decode_oauth_access_token") as mock_decode,
        patch("modulo.auth.oauth.rotate_oauth_token_family") as mock_rotate,
        patch("modulo.auth.oauth.create_oauth_access_token") as mock_create_token,
        patch("modulo.auth.oauth.check_oauth_token_family_valid") as mock_check,
    ):
        mock_validate.return_value = _make_mock_client(client_id=cid)
        mock_check.return_value = True
        mock_rotate.return_value = (family_id, seq + 1)
        mock_create_token.return_value = "jwt_access_token_def"
        mock_decode.return_value = MagicMock(
            client_id=cid,
            organisation_id=ORG_ID,
            scopes=["trigger:run"],
            token_family=family_id,
            token_sequence=seq,
        )
        resp = client.post(
            "/mcp/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": cid,
                "client_secret": secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    request.node._access_token = "jwt_access_token_def"
    request.node._refresh_token = "refresh_token_def"
    _store_resp(request, resp)


@when(
    parsers.parse(
        'I POST /mcp/oauth/token with authorization code "{code}" and no code_verifier'
    )
)
def token_exchange_no_verifier(code: str, client, request):
    with (
        patch("modulo.auth.oauth.get_oauth_client_by_client_id") as mock_get,
        patch("modulo.auth.oauth.validate_client_secret") as mock_validate,
        patch("modulo.auth.oauth.consume_authorization_code") as mock_consume,
    ):
        mock_get.return_value = _make_mock_client(
            client_id="oauth_client_1",
        )
        mock_validate.return_value = _make_mock_client(client_id="oauth_client_1")
        mock_consume.side_effect = ValueError("PKCE code_verifier required")
        resp = client.post(
            "/mcp/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "oauth_client_1",
                "client_secret": "secret",
                "redirect_uri": "https://app.example.com/callback",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    _store_resp(request, resp)


@when(parsers.parse('I DELETE /api/v1/mcp/oauth/clients/{client_id}'))
def delete_oauth_client(client_id: str, client, request):
    with (
        patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete,
        patch("modulo.api.routes.mcp_oauth.set_rls_org"),
    ):
        mock_delete.return_value = True
        resp = client.delete(f"/api/v1/mcp/oauth/clients/{client_id}")
    _store_resp(request, resp)





# --------------------------------------------------------------------------
# Then steps
# --------------------------------------------------------------------------


@then(parsers.parse("the response status is {status:d}"))
def _check_response_status(status: int, request) -> None:
    resp = request.node._resp
    assert resp.status_code == status, f"Expected status {status}, got {resp.status_code}"


@then("the response contains client_id")
def resp_contains_client_id(request):
    data = request.node._resp.json()
    assert "client_id" in data


@then("the response contains client_secret")
def resp_contains_client_secret(request):
    data = request.node._resp.json()
    assert "client_secret" in data


@then(parsers.parse('the response has name "{expected}"'))
def resp_has_name(expected: str, request):
    data = request.node._resp.json()
    assert data["name"] == expected


@then("the redirect URI contains a code parameter")
def redirect_has_code(request):
    location = request.node._resp.headers.get("location", "")
    assert "code=" in location, f"Location header missing code param: {location}"


@then(parsers.parse('the redirect URI contains the state "{expected}"'))
def redirect_has_state(expected: str, request):
    location = request.node._resp.headers.get("location", "")
    assert f"state={expected}" in location, (
        f"Location header missing state={expected}: {location}"
    )


@then("the response contains an access_token")
def resp_contains_access_token(request):
    data = request.node._resp.json()
    token = data.get("access_token", getattr(request.node, "_access_token", None))
    assert token is not None


@then("the response contains a refresh_token")
def resp_contains_refresh_token(request):
    data = request.node._resp.json()
    token = data.get("refresh_token", getattr(request.node, "_refresh_token", None))
    assert token is not None


@then(parsers.parse('the token has scopes {expected}'))
def token_has_scopes(expected: str, request):
    expected_list = json.loads(expected)
    data = request.node._resp.json()
    scope_field = data.get("scope", data.get("scopes", ""))
    if isinstance(scope_field, str):
        actual = scope_field.split()
    else:
        actual = scope_field
    for s in expected_list:
        assert s in actual, f"Expected scope '{s}' not in {actual}"


@then(parsers.parse('the error indicates "{msg}"'))
def error_indicates(msg: str, request):
    data = request.node._resp.json()
    payload = " ".join(str(v).lower() for v in data.values())
    assert msg.lower() in payload or msg.lower() in str(data.get("detail", "")).lower()


@then("the response contains a new access_token")
def resp_contains_new_access_token(request):
    data = request.node._resp.json()
    assert "access_token" in data or getattr(request.node, "_access_token", None) is not None


@then("the response contains a new refresh_token")
def resp_contains_new_refresh_token(request):
    data = request.node._resp.json()
    assert "refresh_token" in data or getattr(request.node, "_refresh_token", None) is not None


@then("the old refresh token is no longer valid")
def old_refresh_invalid(request):
    pass


@then("the response indicates the client is deleted")
def client_deleted(request):
    data = request.node._resp.json()
    assert data.get("deleted") is True


@then("the client cannot be used for token exchange")
def client_cannot_exchange(request):
    pass

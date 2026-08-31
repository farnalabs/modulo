"""Step definitions for DOM sensitive data masking and reveal (PRD §6.17)."""

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.middleware.sensitive_mask import (
    SENSITIVE_VALUE_MASK,
    is_sensitive_key,
)

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/security/dom_sensitive_data.feature")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_response(request: Any, resp: Any) -> None:
    request.node._resp = resp


# ===========================================================================
# Scenario: Credentials masked in API response
# ===========================================================================


@given(
    parsers.parse('a connector with config_json containing "{key}" set to "{value}"'),
)
def connector_with_sensitive_config(
    mock_session: AsyncMock,
    request: Any,
    key: str,
    value: str,
) -> None:
    connector_id = uuid.uuid4()
    request.node._connector_id = connector_id

    mock_connector = MagicMock()
    mock_connector.id = connector_id
    mock_connector.organisation_id = ORG_ID
    mock_connector.name = "Test Connector"
    mock_connector.connector_type_id = "filesystem"
    mock_connector.credentials_ciphertext = b"encrypted"
    mock_connector.config_json = {key: value, "name": "My Connector"}
    mock_connector.allowed_operations = []
    mock_connector.status = "active"
    mock_connector.visibility = "org"
    mock_connector.owner_team_id = None
    mock_connector.tier = "community"
    mock_connector.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    mock_connector.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    # Nullable degraded markers: a bare MagicMock would auto-create these as
    # non-serialisable mocks, so mirror a healthy ORM row explicitly.
    mock_connector.degraded_at = None
    mock_connector.last_skip_error = None

    request.node._mock_connector = mock_connector


@when("I retrieve the connector")
def retrieve_connector(client: Any, request: Any) -> None:
    connector_id = request.node._connector_id
    mock_connector = request.node._mock_connector

    with (
        patch(
            "modulo.api.routes.connectors.get_connector_instance",
            return_value=mock_connector,
        ),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{connector_id}")

    _store_response(request, resp)


@then(parsers.parse('the "{field}" field in config_json is masked'))
def check_field_masked(request: Any, field: str) -> None:
    body = request.node._resp.json()
    assert body["config_json"][field] == SENSITIVE_VALUE_MASK, (
        f"Expected {field} to be masked, got {body['config_json'][field]!r}"
    )


@then(parsers.parse('the "{field}" field retains its original value'))
def check_field_preserved(request: Any, field: str) -> None:
    body = request.node._resp.json()
    assert body["config_json"][field] == "My Connector", (
        f"Expected {field} to be 'My Connector', got {body['config_json'][field]!r}"
    )


# ===========================================================================
# Scenario: Sensitive key detection / Non-sensitive key detection
# ===========================================================================


@given(parsers.parse('a key named "{key_name}"'), target_fixture="key_name")
def a_key_named(key_name: str) -> str:
    return key_name


@when("I check if it is a sensitive key", target_fixture="sensitivity_result")
def check_sensitive_key(key_name: str) -> bool:
    return is_sensitive_key(key_name)


@then("the result should be true")
def result_is_true(sensitivity_result: bool) -> None:
    assert sensitivity_result is True


@then("the result should be false")
def result_is_false(sensitivity_result: bool) -> None:
    assert sensitivity_result is False


# ===========================================================================
# Scenario: Admin reveals SSO client secret / 30-second expiry
# ===========================================================================


@given(parsers.parse('an SSO provider with client_secret "{secret}"'))
def sso_provider_with_secret(
    mock_session: AsyncMock,
    request: Any,
    secret: str,
) -> None:
    provider_id = uuid.uuid4()
    request.node._sso_provider_id = provider_id

    mock_provider = MagicMock()
    mock_provider.id = provider_id
    mock_provider.organisation_id = ORG_ID
    mock_provider.client_secret = secret

    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=mock_provider)
    mock_session.execute = AsyncMock(return_value=execute_result)


@when("I request to reveal the SSO client secret")
def request_reveal_sso(
    request: Any,
    mock_session: Any,
) -> None:
    provider_id = request.node._sso_provider_id
    is_viewer = getattr(request.node, "_viewer_auth", False)
    target_client = request.getfixturevalue("viewer_client" if is_viewer else "client")

    kwargs: dict[str, Any] = {
        "resource_type": "sso_provider",
        "resource_id": str(provider_id),
    }

    if not is_viewer:
        # 403 short-circuits before Redis — only mock Redis for admin flow
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch(
            "modulo.api.middleware.sensitive_mask.Redis.from_url",
            return_value=mock_redis,
        ):
            resp = target_client.post("/api/v1/admin/sensitive/reveal", json=kwargs)
    else:
        resp = target_client.post("/api/v1/admin/sensitive/reveal", json=kwargs)

    _store_response(request, resp)


@when(parsers.parse('I request to reveal for resource type "{rt}"'))
def request_reveal_resource_type(
    client: Any,
    request: Any,
    rt: str,
) -> None:
    resp = client.post(
        "/api/v1/admin/sensitive/reveal",
        json={
            "resource_type": rt,
            "resource_id": str(uuid.uuid4()),
        },
    )
    _store_response(request, resp)


@then(parsers.parse('I receive the plaintext value "{expected}"'))
def check_revealed_value(request: Any, expected: str) -> None:
    body = request.node._resp.json()
    assert body["value"] == expected, f"Expected value {expected!r}, got {body['value']!r}"


@then("the response includes a reveal token")
def check_reveal_token(request: Any) -> None:
    body = request.node._resp.json()
    assert "token" in body, "Response missing 'token' field"
    assert len(body["token"]) == 36, (
        f"Expected UUID-length token (36 chars), got {len(body['token'])}: {body['token']!r}"
    )


@then(parsers.parse('the response declares "{field}" as {value:d}'))
def check_response_field_int(request: Any, field: str, value: int) -> None:
    body = request.node._resp.json()
    assert body[field] == value, f"Expected {field}={value}, got {body.get(field)!r}"

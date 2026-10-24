"""Unit tests for the FAR-2046 base-url validation and read-path helpers.

These cover the defensive arms that the endpoint-level suite cannot reach
without contriving whole requests: malformed stored JSON, non-Mapping legacy
params, over-long/empty endpoints, a legacy read by a caller who may not manage
backends, and the lazy secrets-backend resolution used by multi-row reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.api.routes.model_backends import (
    ModelBackendUpdate,
    _base_urls_for_backends,
    _direct_update_base_url,
    _legacy_base_url,
    _redact_base_url,
    _requested_base_url,
    _run_health_check_on_save,
    _stored_base_url,
    _stored_secret_payload,
    _strip_legacy_base_url,
    _to_response,
    _validate_base_url,
)
from modulo.model_backends.base import HealthResult


def _http_body(exc: HTTPException) -> str:
    return str(exc.detail)


def test_validate_base_url_rejects_an_empty_value() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _validate_base_url("   ")
    assert "must not be empty" in _http_body(excinfo.value)


def test_validate_base_url_rejects_an_over_long_value() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _validate_base_url("https://example.com/" + "a" * 2048)
    assert "at most 2048 characters" in _http_body(excinfo.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://[::ffff:169.254.169.254]/v1",
        "https://[fd00:ec2::254]/v1",
        "https://100.100.100.200/v1",
    ],
    ids=["ipv4-mapped", "aws-imds-v6", "aliyun-imds"],
)
def test_validate_base_url_rejects_metadata_literals(endpoint: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        _validate_base_url(endpoint)
    assert "cloud metadata endpoint" in _http_body(excinfo.value)


def test_redact_base_url_returns_none_for_a_malformed_url() -> None:
    assert _redact_base_url("http://[bad") is None


def test_redact_base_url_without_a_value_is_none() -> None:
    assert _redact_base_url(None) is None


def test_redact_base_url_keeps_only_the_host() -> None:
    assert _redact_base_url("https://gateway.internal.example:8443/v1") == "gateway.internal.example"


def test_stored_secret_payload_logs_and_returns_empty_on_malformed_json(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="modulo.api.routes.model_backends"):
        assert not _stored_secret_payload("{not-json")
    assert "malformed_credential_json" in caplog.text


def test_stored_secret_payload_rejects_a_non_object() -> None:
    assert not _stored_secret_payload('["not", "an", "object"]')


def test_stored_secret_payload_parses_an_object() -> None:
    assert _stored_secret_payload('{"api_key": "k", "base_url": "https://x.example"}') == {
        "api_key": "k",
        "base_url": "https://x.example",
    }


def test_stored_base_url_ignores_a_non_mapping_payload() -> None:
    """A credential payload decoded to a non-object has no stored endpoint."""
    assert _stored_base_url(None) is None
    assert _stored_base_url("https://x.example") is None  # type: ignore[arg-type]


def test_legacy_base_url_ignores_a_non_mapping_value() -> None:
    assert _legacy_base_url(None) is None


def test_legacy_base_url_ignores_a_blank_value() -> None:
    assert _legacy_base_url({"base_url": "   "}) is None


def test_legacy_base_url_returns_a_non_blank_value() -> None:
    assert _legacy_base_url({"base_url": "https://legacy.example/v1"}) == "https://legacy.example/v1"


def test_requested_base_url_rejects_a_non_string_legacy_value() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _requested_base_url(None, {"base_url": 42})
    assert "must be a string" in _http_body(excinfo.value)


def test_requested_base_url_folds_in_the_legacy_value() -> None:
    assert _requested_base_url(None, {"base_url": "https://legacy.example/v1"}) == "https://legacy.example/v1"


def test_strip_legacy_base_url_handles_a_non_mapping_value() -> None:
    assert not _strip_legacy_base_url(None)


def test_strip_legacy_base_url_removes_only_the_endpoint_key() -> None:
    assert _strip_legacy_base_url({"base_url": "https://x.example", "temperature": 0.2}) == {"temperature": 0.2}


def test_direct_update_base_url_without_a_value_is_none() -> None:
    assert _direct_update_base_url(ModelBackendUpdate(base_url=None), None) is None


def test_direct_update_base_url_rejects_a_conflicting_legacy_value() -> None:
    req = ModelBackendUpdate(base_url="https://new.example/v1", allow_insecure_http=True)
    with pytest.raises(HTTPException) as excinfo:
        _direct_update_base_url(req, "https://old.example/v1")
    assert "base_url_conflict" in _http_body(excinfo.value)


@pytest.mark.anyio
async def test_base_urls_for_backends_returns_empty_for_no_rows() -> None:
    assert not await _base_urls_for_backends(MagicMock(), MagicMock(), [])


@pytest.mark.anyio
async def test_base_urls_for_backends_resolves_the_secrets_backend_once() -> None:
    first = MagicMock(id=uuid.uuid4(), provider="openai", default_params={})
    second = MagicMock(id=uuid.uuid4(), provider="ollama", default_params={})
    secrets_backend = AsyncMock()
    with (
        patch("modulo.api.routes.model_backends._try_secrets_backend", return_value=secrets_backend) as factory,
        patch("modulo.api.routes.model_backends._read_secret_payload", new=AsyncMock(return_value={})) as read,
    ):
        result = await _base_urls_for_backends(MagicMock(), MagicMock(), [first, second])
    assert set(result) == {first.id, second.id}
    factory.assert_called_once()
    assert read.await_count == 2


def _backend_row(provider: str, default_params: dict[str, Any]) -> SimpleNamespace:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        name="Backend",
        display_name="Backend",
        provider=provider,
        model_id="model-1",
        credentials_ciphertext=b"encrypted",
        default_params=default_params,
        visibility="org",
        owner_team_id=None,
        tier="native",
        fallback_backend_ids=None,
        account_id=uuid.uuid4(),
        created_at=now,
        updated_at=now,
        validation_level=None,
    )


def test_to_response_redacts_the_endpoint_for_a_non_manager() -> None:
    mb = _backend_row("openai", {"base_url": "https://legacy.internal.example/v1"})
    response = _to_response(mb, base_url="https://secret.internal.example/v1", can_manage=False)
    assert response.base_url == "secret.internal.example"
    assert response.default_params["base_url"] == "legacy.internal.example"


def test_to_response_non_manager_without_a_legacy_endpoint() -> None:
    mb = _backend_row("openai", {"temperature": 0.2})
    response = _to_response(mb, base_url=None, can_manage=False)
    assert response.base_url is None
    assert "base_url" not in response.default_params


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("api_key", "base_url"),
    [(None, None), ("sk-key", "https://gateway.internal.example/v1")],
    ids=["no-credential-no-endpoint", "credential-and-endpoint"],
)
async def test_health_check_on_save_forwards_only_supplied_inputs(
    api_key: str | None,
    base_url: str | None,
) -> None:
    backend = AsyncMock()
    backend.health_check = AsyncMock(return_value=HealthResult(ok=True))
    with patch("modulo.api.routes.model_backends._build_backend", return_value=backend) as build:
        status, detail = await _run_health_check_on_save("openai", "gpt-4o", api_key, {}, base_url=base_url)
    assert (status, detail) == ("ok", None)
    creds: dict[str, Any] = build.call_args.args[2]
    assert creds.get("api_key") == api_key
    assert creds.get("base_url") == base_url

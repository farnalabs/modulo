"""Health-check credential redaction regression tests (connector health_check layer).

Covers the ``health_check`` error paths of URL/query credential carriers:

* :class:`TrelloConnector` — passes ``key``/``token`` as query parameters, so a
  transport error raised inside ``health_check`` carries the live credentials in
  ``exc.request.url`` even when the error message itself is clean. The method
  must be wrapped by the ``@redacting`` decorator so the escaping exception is
  re-built with a scrubbed request URL (FAR-507 class) before it escapes.
* :class:`RestConnector` — the health-check detail string embeds the probe
  ``request.url``. A credential value rendered into the configured path/URL
  must be value-redacted from the detail, on both the ok and not-ok branches.

These tests FAIL if the ``health_check`` redaction wiring is removed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from modulo.connectors.rest import RestConnector
from modulo.connectors.trello import TrelloConnector
from tests.connectors._noop_guard import make_noop_security_guard as _noop_guard

_TRELLO_KEY = "tkey1234567890abcdef"
_TRELLO_TOKEN = "ttoken1234567890abcdef"
_REST_SECRET_KEY = "restapikey123456789"


def test_trello_connector_health_check_redacts_credentials_from_transport_error_url() -> None:
    """A transport error escaping Trello's health_check must have the key/token
    stripped from BOTH the message and the attached request URL (Trello passes
    the credentials as query parameters, so the clean-message URL still leaks)."""
    connector = TrelloConnector(api_key=_TRELLO_KEY, token=_TRELLO_TOKEN)
    mock_request = httpx.Request(
        "GET",
        f"https://api.trello.com/1/members/me?key={_TRELLO_KEY}&token={_TRELLO_TOKEN}",
    )

    with respx.mock:
        respx.get("https://api.trello.com/1/members/me").mock(
            side_effect=httpx.ConnectError("All connection attempts failed", request=mock_request),
        )
        with pytest.raises(httpx.RequestError) as exinfo:
            asyncio.run(connector.health_check())
    escaped = exinfo.value
    assert _TRELLO_KEY not in str(escaped)
    assert _TRELLO_TOKEN not in str(escaped)
    assert _TRELLO_KEY not in str(escaped.request.url)
    assert _TRELLO_TOKEN not in str(escaped.request.url)
    assert "***" in str(escaped.request.url)


def _make_rest_connector(config: dict[str, Any], creds: dict[str, Any], handler: Any) -> RestConnector:
    """Build a RestConnector against a stub HTTP transport (no real network)."""
    return RestConnector(
        config,
        creds,
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=_noop_guard(),
    )


@pytest.mark.parametrize("status", [200, 500])
def test_rest_health_check_redacts_credential_from_detail_url(status: int) -> None:
    """A credential value rendered into the configured probe URL must be
    value-redacted from the health-check detail on BOTH the ok and not-ok paths."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"status": "up"})

    connector = _make_rest_connector(
        {"base_url": "https://api.example.com", "path": f"/health?key={_REST_SECRET_KEY}"},
        {"auth_mode": "api_key", "api_key": _REST_SECRET_KEY, "in": "query", "query_param_name": "api_key"},
        handler,
    )
    health = asyncio.run(connector.health_check())
    assert health.ok is (status == 200)
    assert _REST_SECRET_KEY not in health.detail
    assert "key=***" in health.detail
    assert f"key={_REST_SECRET_KEY}" not in health.detail

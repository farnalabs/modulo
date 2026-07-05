"""Resilience tests for LinearConnector — HTTP/JSON error handling."""

import httpx
import pytest
import respx

from modulo.connectors.linear import LinearConnector

API_KEY = "lin_api_key_xxxx"
_GRAPHQL = "https://api.linear.app/graphql"


@pytest.fixture()
def connector():
    return LinearConnector(api_key=API_KEY)


@respx.mock
async def test_http_429_rate_limit_raises_valueerror(connector):
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(429, text="Rate limit exceeded"))
    with pytest.raises(ValueError, match="HTTP 429"):
        await connector._graphql("query { viewer { id } }")


@respx.mock
async def test_http_500_server_error_raises_valueerror(connector):
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(ValueError, match="HTTP 500"):
        await connector._graphql("query { viewer { id } }")


@respx.mock
async def test_connection_error_raises_valueerror(connector):
    respx.post(_GRAPHQL).mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(ValueError, match="connection error"):
        await connector._graphql("query { viewer { id } }")


@respx.mock
async def test_invalid_json_response_raises_valueerror(connector):
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(200, text="not-json"))
    with pytest.raises(ValueError, match="invalid response"):
        await connector._graphql("query { viewer { id } }")


@respx.mock
async def test_empty_response_returns_empty_dict(connector):
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(200, json={"data": None}))
    result = await connector._graphql("query { viewer { id } }")
    assert result == {}

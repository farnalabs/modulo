"""Additional vendor-client error and retry coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.product_analytics.vendor_client import MAX_ATTEMPTS, VendorClient


@pytest.mark.asyncio
async def test_vendor_client_retries_429_and_honours_retry_after():
    client = VendorClient("https://vendor.example", "secret")
    responses = []
    for _ in range(MAX_ATTEMPTS - 1):
        response = MagicMock(status_code=429, is_success=False, text="busy", headers={"Retry-After": "999"})
        responses.append(response)
    responses.append(MagicMock(status_code=200, is_success=True, text="", headers={}))
    client._http_client = AsyncMock(is_closed=False)
    client._http_client.post = AsyncMock(side_effect=responses)
    with patch("modulo.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await client.post_batch(b"{}", 1.0, 2)
    assert result == (True, 200, None)
    assert sleep.await_args_list[0].args == (60.0,)
    assert client._http_client.post.await_count == MAX_ATTEMPTS
    await client.close()


@pytest.mark.asyncio
async def test_vendor_client_invalid_retry_after_falls_back_and_timeout_is_described():
    client = VendorClient("https://vendor.example", "secret")
    response = MagicMock(status_code=429, is_success=False, text="busy", headers={"Retry-After": "not-a-number"})
    client._http_client = AsyncMock(is_closed=False)
    client._http_client.post = AsyncMock(side_effect=TimeoutError())
    with patch("modulo.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_batch(b"{}", 1.0, 2)
    assert result[0] is False
    assert result[2] == "Timeout after 30.0s"
    assert client._delay_for_429(response, 1) == 1.0
    await client.close()


@pytest.mark.asyncio
async def test_vendor_client_unexpected_exception_and_closed_client_recreation(monkeypatch):
    client = VendorClient("https://vendor.example", "secret")
    closed = AsyncMock(is_closed=True)
    replacement = AsyncMock(is_closed=False)
    client._http_client = closed
    monkeypatch.setattr(
        "modulo.core.product_analytics.vendor_client.pinned_async_client",
        AsyncMock(return_value=replacement),
    )
    replacement.post = AsyncMock(side_effect=RuntimeError("unexpected"))
    with patch("modulo.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock):
        success, code, error = await client.post_batch(b"{}", 1.0, 2)
    assert success is False
    assert code is None
    assert error == "Unexpected: unexpected"
    await client.close()

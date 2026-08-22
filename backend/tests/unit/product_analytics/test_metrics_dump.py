"""Unit tests for the product analytics metrics dump cron job."""

from __future__ import annotations

import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.product_analytics.metrics_dump import (
    _BACKFILL_MAX_DAYS,
    _WATERMARK_KEY,
    SCHEMA_VERSION,
    _get_consenting_orgs,
    metrics_dump,
)
from modulo.core.product_analytics.vendor_client import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    VendorClient,
    sign_outbound_batch,
)

# --- HMAC signing ---


class TestSignOutboundBatch:
    def test_deterministic(self) -> None:
        payload = b'{"test": true}'
        ts = 1700000000.0
        seq = 20260821
        secret = "test-secret-key-at-least-32-bytes!!"

        sig1 = sign_outbound_batch(secret, payload, ts, seq)
        sig2 = sign_outbound_batch(secret, payload, ts, seq)
        assert sig1 == sig2

    def test_different_secret_produces_different_sig(self) -> None:
        payload = b'{"test": true}'
        ts = 1700000000.0
        seq = 20260821

        sig1 = sign_outbound_batch("secret-one-at-least-32-bytes-long!!", payload, ts, seq)
        sig2 = sign_outbound_batch("secret-two-at-least-32-bytes-long!!", payload, ts, seq)
        assert sig1 != sig2

    def test_different_payload_produces_different_sig(self) -> None:
        secret = "test-secret-key-at-least-32-bytes!!"
        ts = 1700000000.0
        seq = 20260821

        sig1 = sign_outbound_batch(secret, b'{"a":1}', ts, seq)
        sig2 = sign_outbound_batch(secret, b'{"b":2}', ts, seq)
        assert sig1 != sig2

    def test_matches_manual_hmac(self) -> None:
        secret = "my-secret"
        payload = b"hello"
        ts = 100.0
        seq = 1
        message = payload + f"{ts}:{seq}".encode()
        expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        assert sign_outbound_batch(secret, payload, ts, seq) == expected


# --- Schema version ---


class TestSchemaVersion:
    def test_schema_version_is_int(self) -> None:
        assert isinstance(SCHEMA_VERSION, int)

    def test_schema_version_positive(self) -> None:
        assert SCHEMA_VERSION > 0

    def test_watermark_key_is_string(self) -> None:
        assert isinstance(_WATERMARK_KEY, str)

    def test_backfill_cap_is_14_days(self) -> None:
        assert _BACKFILL_MAX_DAYS == 14


# --- Consent filtering ---


class TestGetConsentingOrgs:
    @pytest.mark.asyncio
    async def test_filters_to_level_all(self) -> None:
        org_id_1 = "11111111-1111-1111-1111-111111111111"
        org_id_2 = "22222222-2222-2222-2222-222222222222"
        org_id_3 = "33333333-3333-3333-3333-333333333333"

        rows = [
            MagicMock(
                id=org_id_1,
                settings_json={"product_analytics": {"level": "all", "level_changed_at": "2026-08-15"}},
            ),
            MagicMock(
                id=org_id_2,
                settings_json={"product_analytics": {"level": "off"}},
            ),
            MagicMock(
                id=org_id_3,
                settings_json={},
            ),
        ]

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(rows))
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await _get_consenting_orgs(mock_session)
        assert len(result) == 1
        assert result[0]["id"] == org_id_1
        assert result[0]["level_changed_at"] == date(2026, 8, 15)

    @pytest.mark.asyncio
    async def test_empty_when_no_orgs(self) -> None:
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await _get_consenting_orgs(mock_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_parses_date_string(self) -> None:
        rows = [
            MagicMock(
                id="aaaa-1111",
                settings_json={"product_analytics": {"level": "all", "level_changed_at": "2026-07-01"}},
            ),
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(rows))
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await _get_consenting_orgs(mock_session)
        assert result[0]["level_changed_at"] == date(2026, 7, 1)

    @pytest.mark.asyncio
    async def test_handles_none_level_changed_at(self) -> None:
        rows = [
            MagicMock(
                id="bbbb-2222",
                settings_json={"product_analytics": {"level": "all"}},
            ),
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(rows))
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await _get_consenting_orgs(mock_session)
        assert len(result) == 1
        assert result[0]["level_changed_at"] is None

    @pytest.mark.asyncio
    async def test_skips_orgs_with_level_off(self) -> None:
        rows = [
            MagicMock(
                id="cccc-3333",
                settings_json={"product_analytics": {"level": "off"}},
            ),
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(rows))
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await _get_consenting_orgs(mock_session)
        assert result == []


# --- Helper to build a mock session factory ---


class _FakeSession:
    """Minimal fake session supporting async-with and begin()."""

    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.flush = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    @asynccontextmanager
    async def begin(self):
        yield None


class _FakeSessionFactory:
    """Fake factory: calling it returns a _FakeSession that supports async-with."""

    def __init__(self) -> None:
        self._session = _FakeSession()

    def __call__(self) -> _FakeSession:
        return self._session


# --- Skip conditions ---


class TestMetricsDumpSkipConditions:
    @pytest.mark.asyncio
    async def test_skips_when_instance_switch_off(self) -> None:
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump._check_instance_switch",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "modulo.core.saq_worker._make_system_session_factory",
                return_value=factory,
            ),
            patch(
                "modulo.settings.get_settings",
                return_value=MagicMock(),
            ),
        ):
            result = await metrics_dump({})
        assert result["skipped"] == "instance_switch_off"

    @pytest.mark.asyncio
    async def test_skips_when_no_consenting_orgs(self) -> None:
        factory = _FakeSessionFactory()

        with (
            patch(
                "modulo.core.product_analytics.metrics_dump._check_instance_switch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.core.saq_worker._make_system_session_factory",
                return_value=factory,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._get_consenting_orgs",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "modulo.settings.get_settings",
                return_value=MagicMock(),
            ),
        ):
            result = await metrics_dump({})
        assert result["skipped"] == "no_consenting_orgs"

    @pytest.mark.asyncio
    async def test_skips_when_missing_vendor_config(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": date(2026, 8, 1)}]

        with (
            patch(
                "modulo.core.product_analytics.metrics_dump._check_instance_switch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.core.saq_worker._make_system_session_factory",
                return_value=factory,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._get_consenting_orgs",
                new_callable=AsyncMock,
                return_value=orgs,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "modulo.settings.get_settings",
                return_value=MagicMock(
                    product_analytics_endpoint_url="",
                    product_analytics_instance_secret="",
                ),
            ),
        ):
            result = await metrics_dump({})
        assert result["skipped"] == "missing_vendor_config"


# --- Vendor client ---


class TestVendorClient:
    def test_retry_delays_count(self) -> None:
        assert len(RETRY_DELAYS) == MAX_ATTEMPTS - 1

    @pytest.mark.asyncio
    async def test_post_batch_returns_success(self) -> None:
        client = VendorClient("https://vendor.example.com", "test-secret")

        mock_response = AsyncMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._http_client = mock_http

        success, code, error = await client.post_batch(b'{"test":1}', 100.0, 1)
        assert success is True
        assert code == 200
        assert error is None

        await client.close()

    @pytest.mark.asyncio
    async def test_post_batch_400_is_terminal(self) -> None:
        client = VendorClient("https://vendor.example.com", "test-secret")

        mock_response = AsyncMock()
        mock_response.is_success = False
        mock_response.status_code = 400
        mock_response.text = "bad request"

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._http_client = mock_http

        success, code, error = await client.post_batch(b'{"test":1}', 100.0, 1)
        assert success is False
        assert code == 400
        assert "terminal" in error

        await client.close()

    @pytest.mark.asyncio
    async def test_post_batch_retries_on_500(self) -> None:
        client = VendorClient("https://vendor.example.com", "test-secret")

        fail_response = AsyncMock()
        fail_response.is_success = False
        fail_response.status_code = 500
        fail_response.text = "server error"

        success_response = AsyncMock()
        success_response.is_success = True
        success_response.status_code = 200

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=[fail_response, success_response])
        mock_http.is_closed = False
        client._http_client = mock_http

        with patch("modulo.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock):
            success, code, _error = await client.post_batch(b'{"test":1}', 100.0, 1)

        assert success is True
        assert code == 200
        assert mock_http.post.call_count == 2

        await client.close()

    @pytest.mark.asyncio
    async def test_post_batch_returns_failure_after_max_attempts(self) -> None:
        client = VendorClient("https://vendor.example.com", "test-secret")

        fail_response = AsyncMock()
        fail_response.is_success = False
        fail_response.status_code = 500
        fail_response.text = "server error"

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=fail_response)
        mock_http.is_closed = False
        client._http_client = mock_http

        with patch("modulo.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock):
            success, code, _error = await client.post_batch(b'{"test":1}', 100.0, 1)

        assert success is False
        assert code == 500
        assert mock_http.post.call_count == MAX_ATTEMPTS

        await client.close()

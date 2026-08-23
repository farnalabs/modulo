"""Unit tests for the product analytics metrics dump cron job."""

from __future__ import annotations

import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.product_analytics.metrics_dump import (
    _BACKFILL_MAX_DAYS,
    _DUMP_EXECUTION_WINDOW_MINUTES,
    _DUMP_WINDOW_MINUTES,
    _OFFSET_KEY,
    _WATERMARK_KEY,
    SCHEMA_VERSION,
    _get_consenting_orgs,
    _should_dump_now,
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


# --- Jitter gate (_should_dump_now) ---

# The cron ticks every 10 minutes (``*/10 * * * *``), so the offset MUST be a
# multiple of 10 to coincide with a fire. These tests pin the gate against that
# schedule.


class TestShouldDumpNow:
    @pytest.mark.asyncio
    async def test_creates_and_persists_aligned_offset_on_first_run(self) -> None:
        """First run draws an offset aligned to the 10-minute cron grid and
        persists it."""
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.write_system_config",
                new_callable=AsyncMock,
            ) as write,
            patch("secrets.randbelow", return_value=3),
        ):
            # offset = 3 * 10 = 30; 00:30 (minute 30) is inside [30, 40) -> True.
            now = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
            result = await _should_dump_now(factory, now=now)

        assert result is True
        write.assert_awaited_once()
        # write_system_config(session, _OFFSET_KEY, value)
        assert write.await_args.args[1] == _OFFSET_KEY
        assert int(write.await_args.args[2]) % _DUMP_EXECUTION_WINDOW_MINUTES == 0

    @pytest.mark.asyncio
    async def test_generated_offset_always_grid_aligned(self) -> None:
        """Every possible draw lands on the 10-minute grid (a real cron fire)."""
        for draw in range(_DUMP_WINDOW_MINUTES // _DUMP_EXECUTION_WINDOW_MINUTES):
            factory = _FakeSessionFactory()
            with (
                patch(
                    "modulo.core.product_analytics.metrics_dump.read_system_config",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
                patch(
                    "modulo.core.product_analytics.metrics_dump.write_system_config",
                    new_callable=AsyncMock,
                ) as write,
                patch("secrets.randbelow", return_value=draw),
            ):
                await _should_dump_now(factory, now=datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
            assert int(write.await_args.args[2]) % _DUMP_EXECUTION_WINDOW_MINUTES == 0

    @pytest.mark.asyncio
    async def test_persisted_offset_in_window_true(self) -> None:
        factory = _FakeSessionFactory()
        now = datetime(2026, 1, 1, 0, 35, tzinfo=UTC)  # minute 35 in [30, 40)
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value="30",
            ),
            patch("modulo.core.product_analytics.metrics_dump.write_system_config", new_callable=AsyncMock),
        ):
            assert await _should_dump_now(factory, now=now) is True

    @pytest.mark.asyncio
    async def test_persisted_offset_out_of_window_false(self) -> None:
        factory = _FakeSessionFactory()
        now = datetime(2026, 1, 1, 0, 40, tzinfo=UTC)  # 40 not in [30, 40)
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value="30",
            ),
            patch("modulo.core.product_analytics.metrics_dump.write_system_config", new_callable=AsyncMock),
        ):
            assert await _should_dump_now(factory, now=now) is False

    @pytest.mark.asyncio
    async def test_lower_boundary_inclusive_true(self) -> None:
        factory = _FakeSessionFactory()
        now = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)  # exactly offset 30
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value="30",
            ),
            patch("modulo.core.product_analytics.metrics_dump.write_system_config", new_callable=AsyncMock),
        ):
            assert await _should_dump_now(factory, now=now) is True

    @pytest.mark.asyncio
    async def test_upper_boundary_exclusive_false(self) -> None:
        factory = _FakeSessionFactory()
        now = datetime(2026, 1, 1, 0, 40, tzinfo=UTC)  # offset + window == 40, excluded
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value="30",
            ),
            patch("modulo.core.product_analytics.metrics_dump.write_system_config", new_callable=AsyncMock),
        ):
            assert await _should_dump_now(factory, now=now) is False

    @pytest.mark.asyncio
    async def test_offset_at_window_tail_fires_once_daily(self) -> None:
        """Offset 350 (a 05:50 slot) is True only at that tick, not at 06:00."""
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value="350",
            ),
            patch("modulo.core.product_analytics.metrics_dump.write_system_config", new_callable=AsyncMock),
        ):
            at_slot = datetime(2026, 1, 1, 5, 50, tzinfo=UTC)  # minute 350
            after_slot = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)  # minute 360
            assert await _should_dump_now(factory, now=at_slot) is True
            assert await _should_dump_now(factory, now=after_slot) is False

    @pytest.mark.asyncio
    async def test_legacy_unaligned_offset_is_realigned(self) -> None:
        """A pre-existing unaligned offset is realigned to the grid on read and
        persisted, without preventing the dump on the aligned tick."""
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value="37",
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.write_system_config",
                new_callable=AsyncMock,
            ) as write,
        ):
            # Realigned to 30; 00:30 -> True.
            now = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
            assert await _should_dump_now(factory, now=now) is True
        # The realigned value (30) is written back.
        written = [c.args[2] for c in write.await_args_list]
        assert any(int(v) % _DUMP_EXECUTION_WINDOW_MINUTES == 0 for v in written)


# --- Skip conditions ---


class TestMetricsDumpSkipConditions:
    @pytest.mark.asyncio
    async def test_skips_when_instance_switch_off(self) -> None:
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump._should_dump_now",
                new_callable=AsyncMock,
                return_value=True,
            ),
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
                "modulo.core.product_analytics.metrics_dump._should_dump_now",
                new_callable=AsyncMock,
                return_value=True,
            ),
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
                "modulo.core.product_analytics.metrics_dump._should_dump_now",
                new_callable=AsyncMock,
                return_value=True,
            ),
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


# --- RLS / system session factory ---


class TestSystemSessionFactory:
    """The metrics dump MUST build its payload through the SYSTEM session factory.

    ``_build_payload`` reads TEAM-SCOPED tables (pipelines, model_backends,
    connector_instances, environment_profiles, library_primitives) across all
    consenting orgs with no ``set_rls_org`` context. Those reads only return
    every org's rows because the system factory connects as the ``modulo_system``
    role (LOGIN, BYPASSRLS) — the strict ``rls_org_isolation`` policy is bypassed.
    Swapping to ``_make_session_factory`` (``modulo_app``, NOBYPASSRLS) would
    silently filter those reads to the empty ``app.organisation_id`` and return
    ZERO rows. These tests pin the system factory so a future swap is caught.
    """

    @pytest.mark.asyncio
    async def test_metrics_dump_uses_system_session_factory(self) -> None:
        """metrics_dump obtains its session factory from _make_system_session_factory."""
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.saq_worker._make_system_session_factory",
                return_value=factory,
            ) as system_factory,
            patch(
                "modulo.core.saq_worker._make_session_factory",
                side_effect=AssertionError(
                    "metrics_dump must use the SYSTEM session factory "
                    "(BYPASSRLS) — the app factory silently returns zero rows "
                    "on team-scoped tables without an org context"
                ),
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._should_dump_now",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._check_instance_switch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._get_consenting_orgs",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
        ):
            result = await metrics_dump({})

        system_factory.assert_called_once()
        assert result["skipped"] == "no_consenting_orgs"

    @pytest.mark.asyncio
    async def test_metrics_dump_never_uses_regular_session_factory(self) -> None:
        """Swapping to _make_session_factory must fail loudly, not silently.

        Drives the dump past the jitter and instance-switch gates with the app
        factory stubbed to raise; reaching _get_consenting_orgs proves the run
        used the system factory (the app factory path would have exploded).
        """
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.saq_worker._make_system_session_factory",
                return_value=factory,
            ),
            patch(
                "modulo.core.saq_worker._make_session_factory",
                side_effect=AssertionError("metrics_dump swapped to the app session factory"),
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._should_dump_now",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._check_instance_switch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._get_consenting_orgs",
                new_callable=AsyncMock,
                return_value=[],
            ) as get_orgs,
            patch("modulo.settings.get_settings", return_value=MagicMock()),
        ):
            result = await metrics_dump({})

        get_orgs.assert_awaited_once()
        assert result["skipped"] == "no_consenting_orgs"


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

"""Unit tests for the product analytics metrics dump cron job."""

from __future__ import annotations

import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.product_analytics.metrics_dump import (
    _BACKFILL_MAX_DAYS,
    _DUMP_EXECUTION_WINDOW_MINUTES,
    _DUMP_WINDOW_MINUTES,
    _OFFSET_KEY,
    _WATERMARK_KEY,
    SCHEMA_VERSION,
    _build_instance_metadata,
    _build_payload,
    _check_instance_switch,
    _dump_date_range,
    _get_consenting_orgs,
    _get_or_create_instance_id,
    _get_or_create_system_config,
    _resolve_start_date,
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


# --- _check_instance_switch ---


class TestCheckInstanceSwitch:
    @pytest.mark.asyncio
    async def test_enabled_returns_true(self) -> None:
        factory = _FakeSessionFactory()
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value="true",
        ):
            assert await _check_instance_switch(factory) is True

    @pytest.mark.asyncio
    async def test_disabled_returns_false(self) -> None:
        factory = _FakeSessionFactory()
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            assert await _check_instance_switch(factory) is False

    @pytest.mark.asyncio
    async def test_empty_string_returns_false(self) -> None:
        factory = _FakeSessionFactory()
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value="",
        ):
            assert await _check_instance_switch(factory) is False


# --- _should_dump_now with now=None ---


class TestShouldDumpNowDefault:
    @pytest.mark.asyncio
    async def test_now_none_uses_current_time(self) -> None:
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value="0",
            ),
            patch("modulo.core.product_analytics.metrics_dump.write_system_config", new_callable=AsyncMock),
        ):
            result = await _should_dump_now(factory, now=None)
        assert isinstance(result, bool)


# --- _resolve_start_date ---


class TestResolveStartDate:
    @pytest.mark.asyncio
    async def test_with_watermark_returns_next_day(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": date(2026, 8, 1)}]
        dump_date = date(2026, 8, 10)
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value="2026-08-05",
        ):
            start, last = await _resolve_start_date(factory, orgs, dump_date)
        assert start == date(2026, 8, 6)
        assert last == date(2026, 8, 5)

    @pytest.mark.asyncio
    async def test_with_string_watermark(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": date(2026, 8, 1)}]
        dump_date = date(2026, 8, 10)
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value="2026-08-05",
        ):
            start, _last = await _resolve_start_date(factory, orgs, dump_date)
        assert start == date(2026, 8, 6)

    @pytest.mark.asyncio
    async def test_no_watermark_uses_earliest_consent(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": date(2026, 8, 1)}]
        dump_date = date(2026, 8, 10)
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            start, last = await _resolve_start_date(factory, orgs, dump_date)
        assert start == date(2026, 8, 1)
        assert last is None

    @pytest.mark.asyncio
    async def test_no_watermark_string_consent_date(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": "2026-08-01"}]
        dump_date = date(2026, 8, 10)
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            start, _last = await _resolve_start_date(factory, orgs, dump_date)
        assert start == date(2026, 8, 1)

    @pytest.mark.asyncio
    async def test_no_watermark_backfill_cap(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": date(2020, 1, 1)}]
        dump_date = date(2026, 8, 10)
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            start, _last = await _resolve_start_date(factory, orgs, dump_date)
        expected_backfill = dump_date - timedelta(days=_BACKFILL_MAX_DAYS)
        assert start == expected_backfill

    @pytest.mark.asyncio
    async def test_no_watermark_all_none_changed_at(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": None}]
        dump_date = date(2026, 8, 10)
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            start, _last = await _resolve_start_date(factory, orgs, dump_date)
        assert start == dump_date


# --- _dump_date_range ---


class TestDumpDateRange:
    @pytest.mark.asyncio
    async def test_single_day_success(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": None}]
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump._build_payload",
                new_callable=AsyncMock,
                return_value={"schema_version": 1},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.VendorClient",
            ) as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.post_batch = AsyncMock(return_value=(True, 200, None))
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client
            result = await _dump_date_range(
                factory,
                orgs,
                date(2026, 8, 10),
                date(2026, 8, 10),
                "https://vendor.example.com",
                "secret",
            )
        assert result == [date(2026, 8, 10)]

    @pytest.mark.asyncio
    async def test_failure_stops_early(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": None}]
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump._build_payload",
                new_callable=AsyncMock,
                return_value={"schema_version": 1},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.VendorClient",
            ) as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.post_batch = AsyncMock(return_value=(False, 500, "error"))
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client
            result = await _dump_date_range(
                factory,
                orgs,
                date(2026, 8, 10),
                date(2026, 8, 12),
                "https://vendor.example.com",
                "secret",
            )
        assert result == []

    @pytest.mark.asyncio
    async def test_no_eligible_orgs_skips_day(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": date(2026, 8, 15)}]
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump.VendorClient",
            ) as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.post_batch = AsyncMock()
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client
            result = await _dump_date_range(
                factory,
                orgs,
                date(2026, 8, 10),
                date(2026, 8, 12),
                "https://vendor.example.com",
                "secret",
            )
        assert result == []
        mock_client.post_batch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multi_day_success(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": None}]
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump._build_payload",
                new_callable=AsyncMock,
                return_value={"schema_version": 1},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.VendorClient",
            ) as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.post_batch = AsyncMock(return_value=(True, 200, None))
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client
            result = await _dump_date_range(
                factory,
                orgs,
                date(2026, 8, 10),
                date(2026, 8, 12),
                "https://vendor.example.com",
                "secret",
            )
        assert result == [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]


# --- _build_payload ---


class TestBuildPayload:
    @pytest.mark.asyncio
    async def test_assembles_full_payload(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": None}]
        target_date = date(2026, 8, 10)
        mock_session = _FakeSession()

        empty_count_result = MagicMock()
        empty_count_result.scalar_one.return_value = 0

        empty_result = MagicMock()
        empty_result.one.return_value = MagicMock(
            total_runs=0,
            complete=0,
            failed=0,
            cancelled=0,
            stalled=0,
            total_cost_usd=0,
            total_tokens=0,
        )

        empty_group_result = MagicMock()
        empty_group_result.__iter__ = MagicMock(return_value=iter([]))

        call_count = {"n": 0}

        async def _execute(stmt, *args, **kwargs):
            call_count["n"] += 1
            select_from = getattr(stmt, "_select_from_element", None)  # noqa: F841
            return empty_count_result

        mock_session.execute = AsyncMock(side_effect=_execute)

        with (
            patch.object(mock_session, "begin", new_callable=lambda: asynccontextmanager(lambda: (yield None))),
            patch(
                "modulo.core.product_analytics.metrics_dump._count_entities",
                new_callable=AsyncMock,
                return_value={"pipelines": 0},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._aggregate_run_stats",
                new_callable=AsyncMock,
                return_value={"total_runs": 0},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._aggregate_error_stats",
                new_callable=AsyncMock,
                return_value={"total": 0},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._build_integration_inventory",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._get_or_create_instance_id",
                new_callable=AsyncMock,
                return_value="test-instance-id",
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._build_instance_metadata",
                new_callable=AsyncMock,
                return_value={"version": "1.0.0"},
            ),
        ):
            payload = await _build_payload(factory, orgs, target_date)

        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["date"] == "2026-08-10"
        assert payload["instance_id"] == "test-instance-id"
        assert payload["org_count"] == 1


# --- _get_or_create_instance_id ---


class TestGetOrCreateInstanceId:
    @pytest.mark.asyncio
    async def test_creates_new_id(self) -> None:
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
        ):
            result = await _get_or_create_instance_id(factory)
        assert isinstance(result, str)
        assert len(result) > 0
        write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_existing_id(self) -> None:
        factory = _FakeSessionFactory()
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value="existing-id",
        ):
            result = await _get_or_create_instance_id(factory)
        assert result == "existing-id"


# --- _build_instance_metadata ---


class TestBuildInstanceMetadata:
    def test_metadata_shape(self) -> None:
        factory = _FakeSessionFactory()
        with patch(
            "modulo.version.get_version",
            return_value="1.2.3",
        ):
            meta = _build_instance_metadata(factory)
        assert meta["version"] == "1.2.3"
        assert meta["schema_version"] == SCHEMA_VERSION
        assert isinstance(meta["git_sha"], str)
        assert isinstance(meta["deployment_mode"], str)


# --- metrics_dump success path ---


class TestMetricsDumpSuccess:
    @pytest.mark.asyncio
    async def test_full_success_advances_watermark(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": None}]
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
                "modulo.core.product_analytics.metrics_dump._dump_date_range",
                new_callable=AsyncMock,
                return_value=[date(2026, 8, 10)],
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.acquire_kv_lock",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.write_system_config",
                new_callable=AsyncMock,
            ),
            patch(
                "modulo.core.saq_worker._make_system_session_factory",
                return_value=factory,
            ),
            patch(
                "modulo.settings.get_settings",
                return_value=MagicMock(
                    product_analytics_endpoint_url="https://vendor.example.com",
                    product_analytics_instance_secret="secret",
                ),
            ),
        ):
            result = await metrics_dump({})
        assert result["dumped_dates"] == ["2026-08-10"]
        assert result["org_count"] == 1

    @pytest.mark.asyncio
    async def test_jitter_skip(self) -> None:
        factory = _FakeSessionFactory()
        with (
            patch(
                "modulo.core.product_analytics.metrics_dump._should_dump_now",
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
        assert result["skipped"] == "jitter_skip"

    @pytest.mark.asyncio
    async def test_up_to_date(self) -> None:
        factory = _FakeSessionFactory()
        today = datetime.now(UTC).date()
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
                "modulo.core.product_analytics.metrics_dump._get_consenting_orgs",
                new_callable=AsyncMock,
                return_value=[{"id": "org-1", "level_changed_at": None}],
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.read_system_config",
                new_callable=AsyncMock,
                return_value=today.isoformat(),
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
        assert result["skipped"] == "up_to_date"

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_advance_watermark(self) -> None:
        factory = _FakeSessionFactory()
        orgs = [{"id": "org-1", "level_changed_at": None}]
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
                "modulo.core.product_analytics.metrics_dump._dump_date_range",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump.write_system_config",
                new_callable=AsyncMock,
            ) as write_cfg,
            patch(
                "modulo.core.saq_worker._make_system_session_factory",
                return_value=factory,
            ),
            patch(
                "modulo.settings.get_settings",
                return_value=MagicMock(
                    product_analytics_endpoint_url="https://vendor.example.com",
                    product_analytics_instance_secret="secret",
                ),
            ),
        ):
            result = await metrics_dump({})
        assert not result["dumped_dates"]
        write_cfg.assert_not_awaited()


# --- _get_or_create_system_config ---


class TestGetOrCreateSystemConfig:
    @pytest.mark.asyncio
    async def test_existing_value_returned(self) -> None:
        factory = _FakeSessionFactory()
        with patch(
            "modulo.core.product_analytics.metrics_dump.read_system_config",
            new_callable=AsyncMock,
            return_value="existing",
        ):
            result = await _get_or_create_system_config(factory, "test_key", lambda: "new")
        assert result == "existing"

    @pytest.mark.asyncio
    async def test_creates_when_none(self) -> None:
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
        ):
            result = await _get_or_create_system_config(factory, "test_key", lambda: "created")
        assert result == "created"
        write.assert_awaited_once()


# --- _get_consenting_orgs edge cases ---


class TestGetConsentingOrgsExtended:
    @pytest.mark.asyncio
    async def test_settings_json_none(self) -> None:
        rows = [
            MagicMock(id="org-1", settings_json=None),
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(rows))
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        result = await _get_consenting_orgs(mock_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_product_analytics_key(self) -> None:
        rows = [
            MagicMock(id="org-1", settings_json={"other_key": True}),
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(rows))
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        result = await _get_consenting_orgs(mock_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_level_changed_at_as_date_object(self) -> None:
        rows = [
            MagicMock(
                id="org-1",
                settings_json={"product_analytics": {"level": "all", "level_changed_at": date(2026, 1, 1)}},
            ),
        ]
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(rows))
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        result = await _get_consenting_orgs(mock_session)
        assert len(result) == 1
        assert result[0]["level_changed_at"] == date(2026, 1, 1)


# --- _count_entities ---


class TestCountEntities:
    @pytest.mark.asyncio
    async def test_returns_all_counts(self) -> None:
        import uuid

        from modulo.core.product_analytics.metrics_dump import _count_entities

        org_ids = [uuid.UUID("11111111-1111-1111-1111-111111111111")]

        call_idx = {"n": 0}
        counts = [5, 3, 2, 1, 4, 6, 7, 8, 9]

        def _make_result(count_val):
            r = MagicMock()
            r.scalar_one.return_value = count_val
            return r

        mock_session = AsyncMock()

        async def _execute(stmt, *args, **kwargs):
            idx = call_idx["n"]
            call_idx["n"] += 1
            return _make_result(counts[idx] if idx < len(counts) else 0)

        mock_session.execute = AsyncMock(side_effect=_execute)

        result = await _count_entities(mock_session, org_ids)
        assert result["pipelines"] == 5
        assert result["agents"] == 3
        assert result["schemas"] == 2
        assert result["teams"] == 1
        assert result["model_backends"] == 4
        assert result["connector_instances"] == 6
        assert result["triggers"] == 7
        assert result["eval_definitions"] == 8
        assert result["environment_profiles"] == 9
        assert result["orgs"] == 1


# --- _aggregate_run_stats ---


class TestAggregateRunStats:
    @pytest.mark.asyncio
    async def test_returns_run_stats(self) -> None:
        import uuid

        from modulo.core.product_analytics.metrics_dump import _aggregate_run_stats

        org_ids = [uuid.UUID("11111111-1111-1111-1111-111111111111")]
        mock_session = AsyncMock()
        row = MagicMock()
        row.total_runs = 10
        row.complete = 7
        row.failed = 2
        row.cancelled = 1
        row.stalled = 0
        row.total_cost_usd = 1.23
        row.total_tokens = 50000
        result_mock = MagicMock()
        result_mock.one.return_value = row
        mock_session.execute = AsyncMock(return_value=result_mock)

        result = await _aggregate_run_stats(mock_session, org_ids, date(2026, 8, 10))
        assert result["total_runs"] == 10
        assert result["complete"] == 7
        assert result["failed"] == 2
        assert result["cancelled"] == 1
        assert result["stalled"] == 0
        assert result["total_cost_usd"] == "1.23"
        assert result["total_tokens"] == 50000

    @pytest.mark.asyncio
    async def test_null_values_default_to_zero(self) -> None:
        import uuid

        from modulo.core.product_analytics.metrics_dump import _aggregate_run_stats

        org_ids = [uuid.UUID("11111111-1111-1111-1111-111111111111")]
        mock_session = AsyncMock()
        row = MagicMock()
        row.total_runs = None
        row.complete = None
        row.failed = None
        row.cancelled = None
        row.stalled = None
        row.total_cost_usd = None
        row.total_tokens = None
        result_mock = MagicMock()
        result_mock.one.return_value = row
        mock_session.execute = AsyncMock(return_value=result_mock)

        result = await _aggregate_run_stats(mock_session, org_ids, date(2026, 8, 10))
        assert result["total_runs"] == 0
        assert result["complete"] == 0
        assert result["total_tokens"] == 0


# --- _aggregate_error_stats ---


class TestAggregateErrorStats:
    @pytest.mark.asyncio
    async def test_returns_error_stats_by_level(self) -> None:
        import uuid

        from modulo.core.product_analytics.metrics_dump import _aggregate_error_stats

        org_ids = [uuid.UUID("11111111-1111-1111-1111-111111111111")]
        mock_session = AsyncMock()

        row1 = MagicMock()
        row1.level_peak = "error"
        row1._mapping = {"count": 5}
        row2 = MagicMock()
        row2.level_peak = "warning"
        row2._mapping = {"count": 3}

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row1, row2]))
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await _aggregate_error_stats(mock_session, org_ids, date(2026, 8, 10))
        assert result["total"] == 8
        assert result["by_level"]["error"] == 5
        assert result["by_level"]["warning"] == 3

    @pytest.mark.asyncio
    async def test_empty_error_stats(self) -> None:
        import uuid

        from modulo.core.product_analytics.metrics_dump import _aggregate_error_stats

        org_ids = [uuid.UUID("11111111-1111-1111-1111-111111111111")]
        mock_session = AsyncMock()

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await _aggregate_error_stats(mock_session, org_ids, date(2026, 8, 10))
        assert result["total"] == 0
        assert not result["by_level"]


# --- _build_integration_inventory ---


class TestBuildIntegrationInventory:
    @pytest.mark.asyncio
    async def test_returns_inventory(self) -> None:
        import uuid

        from modulo.core.product_analytics.metrics_dump import _build_integration_inventory

        org_ids = [uuid.UUID("11111111-1111-1111-1111-111111111111")]
        mock_session = AsyncMock()

        call_count = {"n": 0}

        async def _execute(stmt, *args, **kwargs):
            call_count["n"] += 1
            mock_result = MagicMock()
            if call_count["n"] == 1:
                r1 = MagicMock()
                r1.provider = "openai"
                r1._mapping = {"count": 3}
                r2 = MagicMock()
                r2.provider = "anthropic"
                r2._mapping = {"count": 2}
                mock_result.__iter__ = MagicMock(return_value=iter([r1, r2]))
            elif call_count["n"] == 2:
                r1 = MagicMock()
                r1.connector_type_id = "github"
                r1._mapping = {"count": 1}
                mock_result.__iter__ = MagicMock(return_value=iter([r1]))
            else:
                r1 = MagicMock()
                r1.trigger_type = "cron"
                r1._mapping = {"count": 4}
                mock_result.__iter__ = MagicMock(return_value=iter([r1]))
            return mock_result

        mock_session.execute = AsyncMock(side_effect=_execute)

        result = await _build_integration_inventory(mock_session, org_ids)
        assert result["model_providers"]["openai"] == 3
        assert result["model_providers"]["anthropic"] == 2
        assert result["connector_types"]["github"] == 1
        assert result["trigger_types"]["cron"] == 4

    @pytest.mark.asyncio
    async def test_empty_inventory(self) -> None:
        import uuid

        from modulo.core.product_analytics.metrics_dump import _build_integration_inventory

        org_ids = [uuid.UUID("11111111-1111-1111-1111-111111111111")]
        mock_session = AsyncMock()

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await _build_integration_inventory(mock_session, org_ids)
        assert not result["model_providers"]
        assert not result["connector_types"]
        assert not result["trigger_types"]

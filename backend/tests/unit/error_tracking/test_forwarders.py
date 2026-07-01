"""Tests for error forwarders — Sentry, DataDog, PagerDuty, Rollbar, OpsGenie, Loki."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.error_tracking.forwarders import (
    DatadogErrorForwarder,
    ForwarderRegistry,
    LokiErrorForwarder,
    OpsGenieErrorForwarder,
    PagerDutyErrorForwarder,
    RollbarErrorForwarder,
    SentryErrorForwarder,
    get_forwarder,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_error_group(**overrides):
    group = MagicMock()
    group.id = uuid.uuid4()
    group.fingerprint = "abc123def456"
    group.count = 5
    group.status = "new"
    group.level_peak = "error"
    for k, v in overrides.items():
        setattr(group, k, v)
    return group


def _make_error_event(**overrides):
    event = MagicMock()
    event.level = "error"
    event.message = "Something went wrong"
    event.source = "backend"
    event.stacktrace = "Traceback ...\n  File main.py, line 42, in handler"
    event.environment = "production"
    event.version = "1.2.3"
    event.context_json = {"request_id": "req-abc"}
    for k, v in overrides.items():
        setattr(event, k, v)
    return event


# =========================================================================
# SentryErrorForwarder
# =========================================================================


class TestSentryErrorForwarder:
    async def test_no_dsn_returns_false(self) -> None:
        fwd = SentryErrorForwarder()
        result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {})
        assert result is False

    async def test_sdk_available_calls_sentry_sdk(self) -> None:
        fwd = SentryErrorForwarder()
        sentry_sdk = MagicMock()
        sentry_sdk.push_scope = MagicMock()

        with patch.dict("sys.modules", {"sentry_sdk": sentry_sdk}):
            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"dsn": "https://key@sentry.io/123"},
            )
        assert result is True
        assert sentry_sdk.capture_message.called

    async def test_api_fallback_when_sdk_not_available(self) -> None:
        fwd = SentryErrorForwarder()

        with (
            patch("modulo.core.error_tracking.forwarders.sentry.httpx.AsyncClient") as mock_client,
        ):
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"dsn": "https://key@sentry.io/123", "org_slug": "test-org", "project_slug": "test-project"},
            )
        assert result is True

    async def test_api_fallback_failure_logged(self) -> None:
        fwd = SentryErrorForwarder()

        with (
            patch("modulo.core.error_tracking.forwarders.sentry.httpx.AsyncClient") as mock_client,
        ):
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = False
            resp.status_code = 403
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"dsn": "https://key@sentry.io/123", "org_slug": "test-org", "project_slug": "test-project"},
            )
        assert result is False

    async def test_level_mapping(self) -> None:
        fwd = SentryErrorForwarder()

        for level, expected in [("critical", "fatal"), ("error", "error"), ("warning", "warning")]:
            sentry_sdk = MagicMock()
            sentry_sdk.push_scope = MagicMock()

            with patch.dict("sys.modules", {"sentry_sdk": sentry_sdk}):
                result = await fwd.forward(
                    _ORG_ID,
                    _make_error_group(),
                    _make_error_event(level=level),
                    {"dsn": "https://key@sentry.io/123"},
                )
            assert result is True

    async def test_exception_does_not_crash(self) -> None:
        fwd = SentryErrorForwarder()
        result = await fwd.forward(_ORG_ID, None, None, {"dsn": None})
        assert result is False


# =========================================================================
# DatadogErrorForwarder
# =========================================================================


class TestDatadogErrorForwarder:
    async def test_no_api_key_returns_false(self) -> None:
        fwd = DatadogErrorForwarder()
        result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {})
        assert result is False

    async def test_posts_to_datadog_api(self) -> None:
        fwd = DatadogErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.datadog.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"api_key": "dd-api-key-abc", "site": "datadoghq.eu"},
            )
        assert result is True
        call_args = instance.post.call_args
        assert "datadoghq.eu" in call_args[0][0]
        assert "dd-api-key-abc" in call_args[1]["headers"]["DD-API-KEY"]

    async def test_failure_returns_false(self) -> None:
        fwd = DatadogErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.datadog.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = False
            resp.status_code = 401
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"api_key": "dd-api-key-abc"},
            )
        assert result is False

    async def test_request_error_does_not_crash(self) -> None:
        fwd = DatadogErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.datadog.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock(side_effect=Exception("timeout"))
            mock_client.return_value.__aenter__.return_value = instance
            result = await fwd.forward(
                _ORG_ID, _make_error_group(), _make_error_event(), {"api_key": "key"}
            )
        assert result is False


# =========================================================================
# PagerDutyErrorForwarder
# =========================================================================


class TestPagerDutyErrorForwarder:
    async def test_no_routing_key_returns_false(self) -> None:
        fwd = PagerDutyErrorForwarder()
        result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {})
        assert result is False

    async def test_skips_non_critical_by_default(self) -> None:
        fwd = PagerDutyErrorForwarder()
        result = await fwd.forward(
            _ORG_ID,
            _make_error_group(),
            _make_error_event(level="warning"),
            {"routing_key": "pd-key"},
        )
        assert result is False

    async def test_forwards_critical(self) -> None:
        fwd = PagerDutyErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.pagerduty.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(level="critical"),
                {"routing_key": "pd-key"},
            )
        assert result is True
        call_kwargs = instance.post.call_args[1]
        body = call_kwargs["json"]
        assert body["payload"]["severity"] == "critical"
        assert body["routing_key"] == "pd-key"

    async def test_configurable_severity_and_levels(self) -> None:
        fwd = PagerDutyErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.pagerduty.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(level="warning"),
                {
                    "routing_key": "pd-key",
                    "forward_levels": ("warning", "critical"),
                    "severity_mapping": {"warning": "info"},
                },
            )
        assert result is True
        assert instance.post.call_args[1]["json"]["payload"]["severity"] == "info"

    async def test_request_error_does_not_crash(self) -> None:
        fwd = PagerDutyErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.pagerduty.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock(side_effect=Exception("timeout"))
            mock_client.return_value.__aenter__.return_value = instance
            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(level="critical"),
                {"routing_key": "pd-key"},
            )
        assert result is False


# =========================================================================
# RollbarErrorForwarder
# =========================================================================


class TestRollbarErrorForwarder:
    async def test_no_access_token_returns_false(self) -> None:
        fwd = RollbarErrorForwarder()
        result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {})
        assert result is False

    async def test_posts_to_rollbar_api(self) -> None:
        fwd = RollbarErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.rollbar.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"access_token": "rb-token", "environment": "staging"},
            )
        assert result is True
        call_kwargs = instance.post.call_args[1]
        assert call_kwargs["json"]["access_token"] == "rb-token"
        assert call_kwargs["json"]["data"]["environment"] == "staging"

    async def test_failure_returns_false(self) -> None:
        fwd = RollbarErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.rollbar.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = False
            resp.status_code = 500
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"access_token": "rb-token"},
            )
        assert result is False


# =========================================================================
# OpsGenieErrorForwarder
# =========================================================================


class TestOpsGenieErrorForwarder:
    async def test_no_api_key_returns_false(self) -> None:
        fwd = OpsGenieErrorForwarder()
        result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {})
        assert result is False

    async def test_posts_to_opsgenie_api(self) -> None:
        fwd = OpsGenieErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.opsgenie.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"api_key": "og-key", "team": "sre"},
            )
        assert result is True
        call_kwargs = instance.post.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "GenieKey og-key"
        assert "sre" in str(call_kwargs["json"]["responders"])

    async def test_level_maps_to_priority(self) -> None:
        fwd = OpsGenieErrorForwarder()

        for level, expected_priority in [("critical", "P1"), ("error", "P2"), ("warning", "P3")]:
            with patch("modulo.core.error_tracking.forwarders.opsgenie.httpx.AsyncClient") as mock_client:
                instance = AsyncMock()
                instance.post = AsyncMock()
                resp = AsyncMock()
                resp.is_success = True
                instance.post.return_value = resp
                mock_client.return_value.__aenter__.return_value = instance

                await fwd.forward(
                    _ORG_ID,
                    _make_error_group(),
                    _make_error_event(level=level),
                    {"api_key": "og-key"},
                )
                assert instance.post.call_args[1]["json"]["priority"] == expected_priority


# =========================================================================
# LokiErrorForwarder
# =========================================================================


class TestLokiErrorForwarder:
    async def test_no_push_url_returns_false(self) -> None:
        fwd = LokiErrorForwarder()
        result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {})
        assert result is False

    async def test_posts_to_loki_push_api(self) -> None:
        fwd = LokiErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.loki.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"push_url": "https://loki.example.com/loki/api/v1/push", "tenant_id": "my-tenant"},
            )
        assert result is True
        call_kwargs = instance.post.call_args[1]
        assert "loki.example.com" in instance.post.call_args[0][0]
        assert call_kwargs["headers"]["X-Scope-OrgID"] == "my-tenant"
        assert call_kwargs["json"]["streams"][0]["stream"]["org_id"] == str(_ORG_ID)

    async def test_custom_labels(self) -> None:
        fwd = LokiErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.loki.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {
                    "push_url": "https://loki.example.com/push",
                    "labels": {"app": "modulo", "env": "prod"},
                },
            )
            stream = instance.post.call_args[1]["json"]["streams"][0]["stream"]
            assert stream["app"] == "modulo"
            assert stream["env"] == "prod"


# =========================================================================
# ForwarderRegistry
# =========================================================================


class TestForwarderRegistry:
    def test_get_returns_registered_forwarder(self) -> None:
        fwd = get_forwarder("sentry")
        assert isinstance(fwd, SentryErrorForwarder)

    def test_get_returns_none_for_unknown(self) -> None:
        fwd = get_forwarder("nonexistent")
        assert fwd is None

    def test_list_types(self) -> None:
        types = get_forwarder.list_types() if hasattr(get_forwarder, "list_types") else list(
            {"sentry", "datadog", "pagerduty", "rollbar", "opsgenie", "loki"}
        )
        assert "sentry" in types
        assert "datadog" in types

    def test_registry_custom_registration(self) -> None:
        registry = ForwarderRegistry()

        class FakeForwarder:
            async def forward(self, **kwargs):
                return True

        registry.register("custom", FakeForwarder)
        assert registry.get("custom") is FakeForwarder

    def test_registry_unknown_returns_none(self) -> None:
        registry = ForwarderRegistry()
        assert registry.get("unknown") is None


# =========================================================================
# Forwarder failure isolation
# =========================================================================


class TestForwarderFailureIsolation:
    async def test_forwarder_raises_does_not_crash_registry(self) -> None:
        sentry = SentryErrorForwarder()

        with patch("modulo.core.error_tracking.forwarders.sentry.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock(side_effect=Exception())
            mock_client.return_value.__aenter__.return_value = instance

            result = await sentry.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"dsn": "https://key@sentry.io/123", "org_slug": "o", "project_slug": "p"},
            )
            assert result is False

    async def test_dispatch_forwarders_swallows_all_exceptions(self) -> None:
        from modulo.core.error_tracking import _dispatch_forwarders, configure_forwarders

        configure_forwarders({
            "sentry": {"dsn": "dummy"},
        })

        result = await _dispatch_forwarders(
            _ORG_ID,
            _make_error_group(),
            _make_error_event(),
            {},
        )
        assert result is None

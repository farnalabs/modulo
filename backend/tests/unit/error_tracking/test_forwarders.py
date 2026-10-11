"""Tests for error forwarders — Sentry, DataDog, PagerDuty, Rollbar, OpsGenie, Loki."""

from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError

from modulo.core.error_tracking import (
    _dispatch_forwarders,
    _dispatch_one_forwarder,
    _load_per_org_configs,
)
from modulo.core.error_tracking.forwarders import (
    DatadogErrorForwarder,
    ForwarderRegistry,
    LokiErrorForwarder,
    OpsGenieErrorForwarder,
    PagerDutyErrorForwarder,
    RollbarErrorForwarder,
    SentryErrorForwarder,
    get_default_registry,
    get_forwarder,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _patch_pinned(module: str, **kwargs: object) -> object:
    """Patch a forwarder module's pinned-client SSRF seam with an async mock."""
    return patch(
        f"modulo.core.error_tracking.forwarders.{module}.pinned_async_client",
        new_callable=AsyncMock,
        **kwargs,  # type: ignore[arg-type]
    )


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
            _patch_pinned("sentry") as mock_client,
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
        # Pins the API-fallback path: the pinned client's POST was actually
        # awaited (the result alone could come from the SDK path).
        mock_client.return_value.__aenter__.return_value.post.assert_awaited_once()

    async def test_api_fallback_omits_authorization_header_without_userinfo(self) -> None:
        fwd = SentryErrorForwarder()

        with _patch_pinned("sentry") as mock_client:
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
                {"dsn": "https://sentry.io/123", "org_slug": "o", "project_slug": "p"},
            )

        assert result is True
        # A DSN with no userinfo yields no auth token, so no Authorization header
        # (pins ``_parse_dsn_token`` returning None).
        headers = mock_client.return_value.__aenter__.return_value.post.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    async def test_sdk_capture_failure_returns_false(self) -> None:
        fwd = SentryErrorForwarder()
        sentry_sdk = MagicMock()
        sentry_sdk.capture_message.side_effect = RuntimeError("boom")

        with patch.dict("sys.modules", {"sentry_sdk": sentry_sdk}):
            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"dsn": "https://key@sentry.io/123"},
            )
        assert result is False

    async def test_api_fallback_failure_logged(self) -> None:
        fwd = SentryErrorForwarder()

        with (
            _patch_pinned("sentry") as mock_client,
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
            assert sentry_sdk.capture_message.call_args.kwargs["level"] == expected

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

        with _patch_pinned("datadog") as mock_client:
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
        # The pinned SSRF seam is used (validate + pin), not a raw httpx client.
        assert mock_client.call_args.kwargs["connector_type"] == "error_forwarder"

    async def test_failure_returns_false(self) -> None:
        fwd = DatadogErrorForwarder()

        with _patch_pinned("datadog") as mock_client:
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

        with _patch_pinned("datadog") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock(side_effect=Exception("timeout"))
            mock_client.return_value.__aenter__.return_value = instance
            result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {"api_key": "key"})
        assert result is False

    @pytest.mark.parametrize(
        ("level", "expected"),
        [("error", "error"), ("critical", "error"), ("warning", "warning")],
    )
    async def test_alert_type_follows_level(self, level: str, expected: str) -> None:
        fwd = DatadogErrorForwarder()

        with _patch_pinned("datadog") as mock_client:
            instance = AsyncMock()
            instance.post = AsyncMock()
            resp = AsyncMock()
            resp.is_success = True
            instance.post.return_value = resp
            mock_client.return_value.__aenter__.return_value = instance

            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(level=level),
                {"api_key": "k"},
            )
        assert result is True
        assert instance.post.call_args.kwargs["json"]["alert_type"] == expected

    def test_build_text_includes_group_count_env_version_and_stacktrace(self) -> None:
        group = _make_error_group(fingerprint="fp123", count=7)
        event = _make_error_event(environment="prod", version="9.9.9")

        text = DatadogErrorForwarder._build_text(_ORG_ID, group, event)

        assert "fp123" in text
        assert "7" in text
        assert "prod" in text
        assert "9.9.9" in text
        assert event.stacktrace in text

        # An empty stacktrace must not render the "Stacktrace" section.
        event.stacktrace = ""
        text_without_stacktrace = DatadogErrorForwarder._build_text(_ORG_ID, group, event)
        assert "Stacktrace" not in text_without_stacktrace


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

        with _patch_pinned("pagerduty") as mock_client:
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

        with _patch_pinned("pagerduty") as mock_client:
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

        with _patch_pinned("pagerduty") as mock_client:
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

        with _patch_pinned("rollbar") as mock_client:
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

        with _patch_pinned("rollbar") as mock_client:
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

        with _patch_pinned("opsgenie") as mock_client:
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
            with _patch_pinned("opsgenie") as mock_client:
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

        with _patch_pinned("loki") as mock_client:
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

        with _patch_pinned("loki") as mock_client:
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


# =========================================================================
# SSRF fail-closed: pinned client refuses to construct -> forward returns False
# =========================================================================


class TestForwarderSsrfFailClosed:
    """Every forwarder must fail closed when the pinned-client seam refuses its URL."""

    _BLOCKED = ValueError("outbound URL blocked: private/metadata address")

    async def test_sentry_api_fallback_refuses_blocked_url(self) -> None:
        fwd = SentryErrorForwarder()
        with _patch_pinned("sentry", side_effect=self._BLOCKED):
            result = await fwd.forward(
                _ORG_ID,
                _make_error_group(),
                _make_error_event(),
                {"dsn": "https://key@169.254.169.254/123", "org_slug": "o", "project_slug": "p"},
            )
        assert result is False

    async def test_datadog_refuses_blocked_url(self) -> None:
        fwd = DatadogErrorForwarder()
        with _patch_pinned("datadog", side_effect=self._BLOCKED):
            result = await fwd.forward(
                _ORG_ID, _make_error_group(), _make_error_event(), {"api_key": "k", "site": "localhost"}
            )
        assert result is False

    async def test_pagerduty_refuses_blocked_url(self) -> None:
        fwd = PagerDutyErrorForwarder()
        with _patch_pinned("pagerduty", side_effect=self._BLOCKED):
            result = await fwd.forward(
                _ORG_ID, _make_error_group(), _make_error_event(level="critical"), {"routing_key": "k"}
            )
        assert result is False

    async def test_rollbar_refuses_blocked_url(self) -> None:
        fwd = RollbarErrorForwarder()
        with _patch_pinned("rollbar", side_effect=self._BLOCKED):
            result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {"access_token": "k"})
        assert result is False

    async def test_opsgenie_refuses_blocked_url(self) -> None:
        fwd = OpsGenieErrorForwarder()
        with _patch_pinned("opsgenie", side_effect=self._BLOCKED):
            result = await fwd.forward(_ORG_ID, _make_error_group(), _make_error_event(), {"api_key": "k"})
        assert result is False

    async def test_loki_refuses_blocked_url(self) -> None:
        fwd = LokiErrorForwarder()
        with _patch_pinned("loki", side_effect=self._BLOCKED):
            result = await fwd.forward(
                _ORG_ID, _make_error_group(), _make_error_event(), {"push_url": "http://127.0.0.1:9090/push"}
            )
        assert result is False


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
        types = get_default_registry().list_types()
        for expected in ("sentry", "datadog", "pagerduty", "rollbar", "opsgenie", "loki"):
            assert expected in types

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

        with _patch_pinned("sentry") as mock_client:
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
        from modulo.core.error_tracking import _DEFAULT_FORWARDER_CONFIGS, _dispatch_forwarders, configure_forwarders

        original_configs = _DEFAULT_FORWARDER_CONFIGS
        try:
            configure_forwarders(
                {
                    "sentry": {"dsn": "dummy"},
                }
            )

            with _patch_pinned("sentry") as mock_client:
                instance = AsyncMock()
                instance.post = AsyncMock(side_effect=Exception("network down"))
                mock_client.return_value.__aenter__.return_value = instance

                result = await _dispatch_forwarders(
                    _ORG_ID,
                    _make_error_group(),
                    _make_error_event(),
                    {},
                )
        finally:
            configure_forwarders(original_configs)
        assert result is None


# =========================================================================
# Non-success responses -> False (PagerDuty / Loki / OpsGenie)
# =========================================================================


class TestForwarderNonSuccessResponse:
    """The ``is_success is False -> return False`` branch of each forwarder."""

    @pytest.mark.parametrize(
        ("forwarder_cls", "module", "config"),
        [
            (PagerDutyErrorForwarder, "pagerduty", {"routing_key": "pd-key"}),
            (LokiErrorForwarder, "loki", {"push_url": "https://loki.example.com/loki/api/v1/push"}),
            (OpsGenieErrorForwarder, "opsgenie", {"api_key": "og-key"}),
        ],
        ids=["pagerduty", "loki", "opsgenie"],
    )
    async def test_returns_false_on_non_success_response(self, forwarder_cls, module, config) -> None:
        fwd = forwarder_cls()

        with _patch_pinned(module) as mock_client:
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
                _make_error_event(level="critical"),
                config,
            )
        assert result is False


# =========================================================================
# Org-config forwarder dispatch (_load_per_org_configs / _dispatch_forwarders)
# =========================================================================


class TestForwarderDispatchConfig:
    async def test_dispatch_one_forwarder_unknown_type_is_noop(self, caplog: pytest.LogCaptureFixture) -> None:
        # An unregistered forwarder type must be skipped (logged), never raised.
        await _dispatch_one_forwarder(_ORG_ID, _make_error_group(), _make_error_event(), "nope", {})
        assert "dispatch_forwarders.unknown_type" in caplog.text

    async def test_load_per_org_configs_reads_enabled_org_rows(self) -> None:
        enabled_row = types.SimpleNamespace(forwarder_type="datadog", config_json={"api_key": "x"})
        no_config_row = types.SimpleNamespace(forwarder_type="loki", config_json=None)
        result = MagicMock()
        result.scalars.return_value.all.return_value = [enabled_row, no_config_row]
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)

        configs = await _load_per_org_configs(session, _ORG_ID)

        assert configs == {"datadog": {"api_key": "x"}}
        # The query must be org-scoped and enabled-filtered.
        stmt_str = str(session.execute.call_args[0][0])
        assert "error_forwarder_configs" in stmt_str
        assert "organisation_id" in stmt_str
        assert "enabled" in stmt_str

    async def test_dispatch_forwarders_prefers_org_config_over_default(self) -> None:
        with (
            patch(
                "modulo.core.error_tracking._load_per_org_configs",
                new=AsyncMock(return_value={"datadog": {"api_key": "org"}}),
            ),
            patch("modulo.core.error_tracking._dispatch_one_forwarder", new=AsyncMock()) as dispatch,
        ):
            await _dispatch_forwarders(_ORG_ID, _make_error_group(), _make_error_event(), {}, session=object())

        dispatch.assert_awaited_once()
        assert dispatch.call_args.args[3] == "datadog"
        assert dispatch.call_args.args[4] == {"api_key": "org"}

    async def test_dispatch_forwarders_falls_back_to_default_without_session(self) -> None:
        default_config = {"loki": {"push_url": "https://loki.example.com/push"}}
        with (
            patch("modulo.core.error_tracking._DEFAULT_FORWARDER_CONFIGS", default_config),
            patch("modulo.core.error_tracking._dispatch_one_forwarder", new=AsyncMock()) as dispatch,
        ):
            await _dispatch_forwarders(_ORG_ID, _make_error_group(), _make_error_event(), {})

        dispatch.assert_awaited_once()
        assert dispatch.call_args.args[3] == "loki"
        assert dispatch.call_args.args[4] == default_config["loki"]

    async def test_load_per_org_configs_reraises_programming_error(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=ProgrammingError("SELECT 1", {}, Exception("boom")))

        with pytest.raises(ProgrammingError):
            await _load_per_org_configs(session, _ORG_ID)

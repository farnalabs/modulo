"""Tests for ``modulo_alert_delivery_failed_total`` (FAR-151 §15.14).

Covers the counter's registration/firing and the two places it is emitted from:
the ``AlertEngine.dispatch_all`` forwarder-error path and the webhook notifier's
request/HTTP failure paths in ``alert_dispatcher``.
"""

from __future__ import annotations

import sys
import types
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import modulo.core.error_tracking.metrics as metrics_mod
from modulo.core.error_tracking.alerting import AlertEngine, TriggeredAlert

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


@pytest.fixture(autouse=True)
def _reset_metric_handles() -> Iterator[None]:
    saved = (
        metrics_mod._alert_delivery_failed_total,
        metrics_mod._alerts_suppressed_total,
    )
    metrics_mod._alert_delivery_failed_total = None
    metrics_mod._alerts_suppressed_total = None
    yield
    (
        metrics_mod._alert_delivery_failed_total,
        metrics_mod._alerts_suppressed_total,
    ) = saved


@pytest.fixture
def fake_otel() -> Iterator[tuple[MagicMock, MagicMock]]:
    fake_metrics = types.ModuleType("opentelemetry.metrics")
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock()
    fake_metrics.get_meter_provider = MagicMock(return_value=None)
    fake_otel = types.ModuleType("opentelemetry")
    fake_otel.metrics = fake_metrics
    patcher = patch.dict(
        sys.modules,
        {"opentelemetry": fake_otel, "opentelemetry.metrics": fake_metrics},
    )
    patcher.start()
    try:
        yield meter, fake_metrics
    finally:
        patcher.stop()


class TestRecordAlertDeliveryFailed:
    def test_lazily_initializes_and_records(self, fake_otel: tuple[MagicMock, MagicMock]) -> None:
        meter, fake_metrics = fake_otel
        provider = MagicMock()
        provider.get_meter.return_value = meter
        fake_metrics.get_meter_provider.return_value = provider

        metrics_mod.record_alert_delivery_failed("rule-1", "webhook")

        counter = meter.create_counter.return_value
        counter.add.assert_called_once_with(
            1,
            attributes={"rule_id": "rule-1", "action_type": "webhook"},
        )
        meter.create_counter.assert_called_once_with(
            name="modulo_alert_delivery_failed_total",
            description="Total number of alert dispatches that failed to reach a notifier",
            unit="1",
        )

    def test_noop_when_no_meter(self) -> None:
        with patch.object(metrics_mod, "_get_meter", return_value=None):
            metrics_mod.record_alert_delivery_failed("rule-1", "email")
        assert metrics_mod._alert_delivery_failed_total is None

    def test_records_without_reinitializing(self) -> None:
        counter = MagicMock()
        metrics_mod._alert_delivery_failed_total = counter
        with patch.object(metrics_mod, "_get_meter") as get_meter:
            metrics_mod.record_alert_delivery_failed("rule-1", "in_app")
        get_meter.assert_not_called()
        counter.add.assert_called_once_with(
            1,
            attributes={"rule_id": "rule-1", "action_type": "in_app"},
        )


class TestDispatchAllForwarderError:
    async def test_failed_dispatch_fires_metric(self) -> None:
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Fail Rule",
            action_type="webhook",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        engine = AlertEngine(redis_client=None)
        session = AsyncMock(spec=AsyncSession)

        with (
            patch("modulo.core.error_tracking.alerting.dispatch_alert", AsyncMock(side_effect=RuntimeError("boom"))),
            patch("modulo.core.error_tracking.alerting.record_error_alert") as recorded,
            patch("modulo.core.error_tracking.alerting.record_alert_delivery_failed") as failed,
        ):
            await engine.dispatch_all(_ORG_ID, [alert], session)

        failed.assert_called_once_with(str(alert.rule_id), "webhook")
        recorded.assert_called_once_with("error", "webhook")

    async def test_successful_dispatch_does_not_fire_failure_metric(self) -> None:
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Good Rule",
            action_type="in_app",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        engine = AlertEngine(redis_client=None)
        session = AsyncMock(spec=AsyncSession)

        with (
            patch("modulo.core.error_tracking.alerting.dispatch_alert", AsyncMock()) as dispatched,
            patch("modulo.core.error_tracking.alerting.record_alert_delivery_failed") as failed,
        ):
            await engine.dispatch_all(_ORG_ID, [alert], session)

        dispatched.assert_awaited_once()
        failed.assert_not_called()


class TestWebhookFailurePath:
    async def test_request_failure_fires_metric(self) -> None:
        from modulo.core.error_tracking import alert_dispatcher as dispatcher_mod

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Webhook Rule",
            action_type="webhook",
            webhook_url="https://hooks.example.com/alert",
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        session = AsyncMock(spec=AsyncSession)
        error_group = MagicMock()
        error_group.sample_event = None

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = httpx.ConnectError("connection refused")

        with (
            patch.object(dispatcher_mod, "record_alert_delivery_failed") as failed,
            patch.object(dispatcher_mod.httpx, "AsyncClient", return_value=client),
        ):
            from modulo.core.error_tracking.alert_dispatcher import dispatch_alert

            await dispatch_alert(_ORG_ID, alert, session, error_group)

        client.post.assert_awaited_once()
        failed.assert_called_once_with(str(alert.rule_id), "webhook")

    async def test_http_error_fires_metric(self) -> None:
        from modulo.core.error_tracking import alert_dispatcher as dispatcher_mod

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Webhook Rule",
            action_type="webhook",
            webhook_url="https://hooks.example.com/alert",
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        session = AsyncMock(spec=AsyncSession)
        error_group = MagicMock()
        error_group.sample_event = None

        response = MagicMock()
        response.is_success = False
        response.status_code = 500

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = response

        with (
            patch.object(dispatcher_mod, "record_alert_delivery_failed") as failed,
            patch.object(dispatcher_mod.httpx, "AsyncClient", return_value=client),
        ):
            from modulo.core.error_tracking.alert_dispatcher import dispatch_alert

            await dispatch_alert(_ORG_ID, alert, session, error_group)

        client.post.assert_awaited_once()
        failed.assert_called_once_with(str(alert.rule_id), "webhook")

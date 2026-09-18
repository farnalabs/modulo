"""FAR-526D: shared egress-layer OTel metrics (pinned / rejected).

Exercises ``modulo.core.egress_metrics`` directly (instrument registration +
record helpers) and the wiring through ``modulo.core.ssrf``'s pinned-egress
factory (that a real egress build emits ``modulo_egress_pinned_total`` and a
rejected build emits ``modulo_egress_rejected_total`` with the correct
host/connector_type/reason attributes).

These are pure unit tests: no network, no DB, no real meter provider. The OTel
meter is injected via ``_get_meter`` the same way
``test_rest_observability.py`` / ``test_error_metrics.py`` stub OTel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from modulo.core import egress_metrics, ssrf


@pytest.fixture(autouse=True)
def _reset_egress_metric_handles() -> Iterator[None]:
    """Save/restore egress_metrics module handles so tests never leak state."""
    saved = tuple(getattr(egress_metrics, name, None) for name in _HANDLE_NAMES)
    for name in _HANDLE_NAMES:
        setattr(egress_metrics, name, None)
    yield
    for name, value in zip(_HANDLE_NAMES, saved, strict=False):
        setattr(egress_metrics, name, value)


_HANDLE_NAMES = ("_pinned_total", "_rejected_total")


def _storage_meter() -> tuple[MagicMock, dict[str, MagicMock]]:
    """A meter whose ``create_counter`` stores handles by instrument name."""
    counters: dict[str, MagicMock] = {}
    meter = MagicMock()

    def mk_counter(name: str, *, description: str = "", unit: str = "1") -> MagicMock:
        handle = MagicMock()
        handle.name = name
        counters[name] = handle
        return handle

    meter.create_counter.side_effect = mk_counter
    meter.create_histogram.side_effect = MagicMock
    return meter, counters


# ── instrument registration + record helpers ────────────────────────────────


class TestEgressMetricsRecords:
    def test_record_pinned_emits_host_and_connector_type(self) -> None:
        meter, counters = _storage_meter()
        with patch.object(egress_metrics, "_get_meter", return_value=meter):
            egress_metrics.record_pinned("github", "api.github.com")
        counters["modulo_egress_pinned_total"].add.assert_called_once_with(
            1, attributes={"host": "api.github.com", "connector_type": "github"}
        )

    def test_record_rejected_emits_host_connector_and_reason(self) -> None:
        meter, counters = _storage_meter()
        with patch.object(egress_metrics, "_get_meter", return_value=meter):
            egress_metrics.record_rejected("rest", "169.254.169.254", egress_metrics.REASON_BLOCKED)
        counters["modulo_egress_rejected_total"].add.assert_called_once_with(
            1,
            attributes={
                "host": "169.254.169.254",
                "connector_type": "rest",
                "reason": "blocked",
            },
        )

    def test_record_rejected_defaults_empty_connector(self) -> None:
        meter, counters = _storage_meter()
        with patch.object(egress_metrics, "_get_meter", return_value=meter):
            egress_metrics.record_rejected("", "h", egress_metrics.REASON_UNPINNED)
        _, kwargs = counters["modulo_egress_rejected_total"].add.call_args
        assert kwargs["attributes"]["connector_type"] == egress_metrics.DEFAULT_CONNECTOR_TYPE

    def test_noop_when_no_meter_provider(self) -> None:
        with patch.object(egress_metrics, "_get_meter", return_value=None):
            egress_metrics.record_pinned("github", "api.github.com")
            egress_metrics.record_rejected("rest", "h", egress_metrics.REASON_BLOCKED)
        assert egress_metrics._pinned_total is None
        assert egress_metrics._rejected_total is None


# ── egress factory wiring: success + reject emit the exact metrics ──────────


class TestEgressFactoryWiring:
    def test_success_build_emits_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pinned client build emits ``modulo_egress_pinned_total``."""
        meter, counters = _storage_meter()
        monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["93.184.216.34"])
        client = None
        try:
            with patch.object(egress_metrics, "_get_meter", return_value=meter):
                client = ssrf.pinned_async_client_sync("https://pinned.example.com/", connector_type="github")
        finally:
            if client is not None:
                asyncio.run(client.aclose())
        pinned = counters["modulo_egress_pinned_total"]
        assert pinned.add.call_count == 1
        _, kwargs = pinned.add.call_args
        assert kwargs["attributes"] == {"host": "pinned.example.com", "connector_type": "github"}
        # No rejection on the success path (the rejected handle exists after _init,
        # but must never have been incremented).
        assert counters["modulo_egress_rejected_total"].add.call_count == 0

    def test_blocked_literal_emits_rejected(self) -> None:
        """A blocked (private/link-local) destination emits ``rejected`` with reason=blocked."""
        meter, counters = _storage_meter()
        with (
            patch.object(egress_metrics, "_get_meter", return_value=meter),
            pytest.raises(ValueError, match="private/internal network"),
        ):
            ssrf.pinned_async_client_sync("http://169.254.169.254/latest/meta-data/", connector_type="rest")
        rejected = counters["modulo_egress_rejected_total"]
        assert rejected.add.call_count == 1
        _, kwargs = rejected.add.call_args
        assert kwargs["attributes"] == {
            "host": "169.254.169.254",
            "connector_type": "rest",
            "reason": "blocked",
        }
        # No pin was built on a rejected path (the pinned handle exists after
        # _init, but must never have been incremented).
        assert counters["modulo_egress_pinned_total"].add.call_count == 0

    def test_bad_scheme_emits_rejected_bad_scheme(self) -> None:
        meter, counters = _storage_meter()
        with (
            patch.object(egress_metrics, "_get_meter", return_value=meter),
            pytest.raises(ValueError, match="must use http"),
        ):
            ssrf.pinned_async_client_sync("ftp://example.com/file", connector_type="rest")
        _, kwargs = counters["modulo_egress_rejected_total"].add.call_args
        assert kwargs["attributes"]["reason"] == "bad-scheme"

    def test_unpinned_host_emits_rejected_unpinned(self) -> None:
        """A pinned transport asked to connect outside its pin map emits reason=unpinned."""
        meter, counters = _storage_meter()
        backend = ssrf._PinnedAsyncNetworkBackend({"pinned.example.com": ("93.184.216.34",)}, connector_type="rest")
        with patch.object(egress_metrics, "_get_meter", return_value=meter), pytest.raises(ssrf.UnpinnedHostError):
            asyncio.run(backend.connect_tcp("evil.example", 443))
        _, kwargs = counters["modulo_egress_rejected_total"].add.call_args
        assert kwargs["attributes"] == {
            "host": "evil.example",
            "connector_type": "rest",
            "reason": "unpinned",
        }

    def test_dns_timeout_emits_rejected_dns_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hung resolver times out (fail-closed) and emits reason=dns-timeout.

        The autouse DNS shim in ``tests/conftest.py`` stubs ``_resolve_all_sync``
        to a public address for fast validation, so this test re-stubs it to the
        exact fail-closed ValueError the real resolver path raises (see
        ``tests/unit/core/test_ssrf.py`` for the real timeout-path exercise).
        """
        meter, counters = _storage_meter()

        def _timeout(_host: str) -> list[str]:
            raise ValueError("DNS resolution timed out for slow.example. Cannot verify the target is not internal.")

        monkeypatch.setattr(ssrf, "_resolve_all_sync", _timeout)

        with (
            patch.object(egress_metrics, "_get_meter", return_value=meter),
            pytest.raises(ValueError, match="timed out"),
        ):
            ssrf.pinned_async_client_sync("http://slow.example/", connector_type="rest")
        _, kwargs = counters["modulo_egress_rejected_total"].add.call_args
        assert kwargs["attributes"]["reason"] == "dns-timeout"

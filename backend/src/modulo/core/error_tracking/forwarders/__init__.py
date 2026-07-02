from __future__ import annotations

import logging

from modulo.core.error_tracking.forwarders.base import BaseForwarder
from modulo.core.error_tracking.forwarders.datadog import DatadogErrorForwarder
from modulo.core.error_tracking.forwarders.loki import LokiErrorForwarder
from modulo.core.error_tracking.forwarders.opsgenie import OpsGenieErrorForwarder
from modulo.core.error_tracking.forwarders.pagerduty import PagerDutyErrorForwarder
from modulo.core.error_tracking.forwarders.rollbar import RollbarErrorForwarder
from modulo.core.error_tracking.forwarders.sentry import SentryErrorForwarder

_log = logging.getLogger(__name__)

_FORWARDERS: dict[str, type[BaseForwarder]] = {
    "sentry": SentryErrorForwarder,
    "datadog": DatadogErrorForwarder,
    "pagerduty": PagerDutyErrorForwarder,
    "rollbar": RollbarErrorForwarder,
    "opsgenie": OpsGenieErrorForwarder,
    "loki": LokiErrorForwarder,
}


class ForwarderRegistry:
    """Maps forwarder type names to their implementation classes."""

    def __init__(self) -> None:
        self._forwarders: dict[str, type[BaseForwarder]] = dict(_FORWARDERS)

    def register(self, name: str, cls: type[BaseForwarder]) -> None:
        self._forwarders[name] = cls

    def get(self, type_name: str) -> type[BaseForwarder] | None:
        return self._forwarders.get(type_name)

    def list_types(self) -> list[str]:
        return list(self._forwarders)


def get_forwarder(type_name: str) -> BaseForwarder | None:
    cls = _FORWARDERS.get(type_name)
    if cls is None:
        return None
    return cls()


__all__ = [
    "BaseForwarder",
    "DatadogErrorForwarder",
    "ForwarderRegistry",
    "LokiErrorForwarder",
    "OpsGenieErrorForwarder",
    "PagerDutyErrorForwarder",
    "RollbarErrorForwarder",
    "SentryErrorForwarder",
    "get_forwarder",
]

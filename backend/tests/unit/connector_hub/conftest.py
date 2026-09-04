"""Shared fixtures for the connector-hub unit tests.

Pins ``modulo.settings.get_settings`` to a settings read with the shared
Redis budget unconfigured. The hub's tenant path (``org_id`` set) is
settings-reading AND fail-closed (FAR-439): without this pin a bare tenant
fixture would raise ``SharedBudgetUnavailableError`` in any environment
where Settings() cannot build from env vars. Tests that specifically patch
``get_settings`` themselves re-patch and win as usual.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _pin_settings_for_tenant_paths() -> None:
    # MagicMock default is truthy for every attribute; explicitly make the
    # shared Redis budget look unconfigured so the tenant path resolves None.
    settings = MagicMock()
    settings.redis_url = None
    patcher = patch("modulo.settings.get_settings", return_value=settings)
    patcher.start()
    yield
    patcher.stop()

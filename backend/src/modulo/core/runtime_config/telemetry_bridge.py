"""Telemetry opt-in bridge (FAR-1131).

Bridges the runtime config store (which supports hot-reloadable overrides
via ``PUT /api/v1/admin/runtime-config``) with the Settings object (which
reads from env vars via pydantic-settings) for the
``MODULO_TELEMETRY_ENABLED`` key.

The function checks the store first, then falls back to the cached
``Settings`` value so that onboarding opt-in and admin toggles take
effect without a restart.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def is_telemetry_enabled() -> bool:
    """Check whether OTel telemetry is enabled, respecting runtime overrides.

    The runtime config store (``PUT /api/v1/admin/runtime-config``) can override
    ``MODULO_TELEMETRY_ENABLED`` at hot-reload time.  This function checks the
    store first, then falls back to the cached ``Settings`` value so that
    onboarding opt-in and admin toggles take effect without a restart.
    """
    try:
        from modulo.core.runtime_config.store import get_runtime_config_store

        store_val = get_runtime_config_store().get("MODULO_TELEMETRY_ENABLED")
        if store_val is not None:
            return store_val.lower() in ("true", "1", "yes")
    except Exception:  # pragma: no cover – store may not be initialised yet
        pass
    from modulo.settings import get_settings

    return get_settings().modulo_telemetry_enabled

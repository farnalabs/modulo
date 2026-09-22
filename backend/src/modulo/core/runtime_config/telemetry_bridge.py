"""Telemetry opt-in bridge (FAR-1131).

Bridges the runtime config store (which supports hot-reloadable overrides
via ``PUT /api/v1/admin/runtime-config``) with the Settings object (which
reads from env vars via pydantic-settings) for the
``MODULO_TELEMETRY_ENABLED`` key.

The function checks the store first, then falls back to the cached
``Settings`` value so that onboarding opt-in and admin toggles take
effect without a restart.

.. note:: **Cross-process consistency.**  The RuntimeConfigStore is a
   per-process singleton.  The web process and the SAQ worker each hold
   their own copy.  A toggle via the admin API takes effect in the web
   process immediately (the ``toggle_telemetry`` helper reconfigures the
   OTel pipeline in-process), but the SAQ worker only sees the change on
   its next startup or if it re-reads the store.  For consistent
   cross-process behaviour, set the environment variable
   ``MODULO_TELEMETRY_ENABLED`` in the deployment configuration rather
   than relying on the runtime override.
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
    except RuntimeError:
        # Defensive arm: get_runtime_config_store() normally lazily
        # constructs the singleton and never raises, so this only covers a
        # store that is explicitly unavailable (e.g. torn down for test
        # isolation, or a future init failure).  Catching only RuntimeError
        # keeps the fallback narrow — any other exception signals a real bug
        # and must propagate.
        _log.debug("RuntimeConfigStore unavailable, falling back to Settings")
    # defence-in-depth: Settings reads the env var directly.  In normal
    # operation the store default is never None so this path is unreachable,
    # but it covers the case where the store was reset or the singleton was
    # torn down for test isolation.
    from modulo.settings import get_settings

    return get_settings().modulo_telemetry_enabled


def toggle_telemetry(enabled: bool) -> None:
    """Set the telemetry override and reconfigure the OTel exporter pipeline.

    Calling this with ``enabled=False`` immediately tears down exporters so
    that no further spans are exported — the decline is effective in-flight,
    not just after a restart.
    """
    from modulo.core.runtime_config.store import get_runtime_config_store

    store = get_runtime_config_store()
    store.set_override("MODULO_TELEMETRY_ENABLED", "true" if enabled else "false")

    # Reconfigure the global OTel TracerProvider so that exporters are
    # added or torn down immediately.  setup_otel is idempotent (calls
    # shutdown_otel first) and safe to invoke from an async context.
    from modulo.otel_bridge.export import setup_otel
    from modulo.settings import get_settings

    settings = get_settings()
    setup_otel(
        service_name=settings.modulo_otel_service_name,
        telemetry_enabled=enabled,
    )

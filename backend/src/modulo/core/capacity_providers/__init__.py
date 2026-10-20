"""Provider/platform capacity probes (FAR-1992).

The infra/provider layer that implements the provider-agnostic
:class:`modulo.db.capacity.PlatformCapacityProbe` port. ``modulo.db`` owns the
port and the FAR-1882 source chain; this package owns the concrete providers and
is wired in at application startup (the API lifespan and the SAQ worker startup
hook), so the DB layer never imports ``modulo.core``.

:func:`build_platform_capacity_probe` is the single factory: it maps
``DB_CAPACITY_PLATFORM_PROVIDER`` to a provider and returns ``None`` when the
provider is disabled, unknown, or its config is incomplete - in which case the
source chain falls through to ``none`` (the hard-stop stays inert). Adding a new
provider means adding a module here and a branch in the factory; nothing in
``modulo.db`` changes.
"""

from __future__ import annotations

import logging

from modulo.db.capacity import PlatformCapacityProbe
from modulo.settings import Settings

_log = logging.getLogger(__name__)

# Provider names that select the Fly.io volumes probe. "auto" builds it when its
# config is complete and is a no-op otherwise.
_FLY_PROVIDERS = frozenset({"auto", "fly"})

# Explicit off-switches (the pre-FAR-1992 behaviour).
_DISABLED_PROVIDERS = frozenset({"", "none", "disabled", "off"})


def build_platform_capacity_probe(settings: Settings) -> PlatformCapacityProbe | None:
    """Build the configured provider probe, or ``None`` (FAR-1992).

    ``None`` is returned - never a guessing probe - when the provider is
    disabled, unknown, or its config is incomplete (missing app name or token).
    The caller installs the result via
    ``modulo.db.capacity.set_platform_capacity_probe``.
    """
    provider = (settings.db_capacity_platform_provider or "").strip().lower()
    if provider in _DISABLED_PROVIDERS:
        return None
    if provider not in _FLY_PROVIDERS:
        _log.warning(
            "capacity.platform.unknown_provider - DB_CAPACITY_PLATFORM_PROVIDER=%r "
            "is not a known provider; no platform probe installed",
            provider,
        )
        return None

    app = (settings.db_capacity_fly_app or "").strip()
    token = (settings.db_capacity_fly_api_token or "").strip()
    if not app or not token:
        # Config absent (the common self-hosted case): no probe. Do not warn -
        # this is a supported, inert configuration on every non-Fly deployment.
        # Log PRESENCE only (never the value) so this line can never leak a
        # credential; the boolean is bound to a non-secret name for the same
        # reason.
        app_configured = bool(app)
        credentials_configured = bool(token)
        _log.debug(
            "capacity.platform.fly_config_incomplete - app_configured=%s credentials_configured=%s",
            app_configured,
            credentials_configured,
        )
        return None

    from modulo.core.capacity_providers.fly import FlyVolumeCapacityProbe

    return FlyVolumeCapacityProbe(
        app=app,
        token=token,
        volume=(settings.db_capacity_fly_volume or "").strip(),
    )

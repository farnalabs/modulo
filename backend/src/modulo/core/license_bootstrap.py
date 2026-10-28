"""The single definition of the licence trust-anchor bootstrap (FAR-2093).

Licence verification consults process-global state held in
``modulo.core.license``: the configured Ed25519 public key (the deployment's
trust anchor) and whether the built-in DEVELOPMENT anchor may be trusted at
all. That state does not cross a process boundary, so EVERY process that
resolves licence state must apply it at startup.

Before FAR-2093 only the API lifespan did. The SAQ worker never did, so it
verified licences against the built-in development public key, failed every
production-signed licence, and silently resolved to the Community tier — while
the API, same instance and same licence, reported the real tier.

``api.main._configure_license_and_otel`` and the SAQ worker startup hook
(``core.saq_worker._startup_event_bus_hook``) both call
:func:`configure_license_trust_anchor`: one definition, two call sites.

FAR-2153 adds the INSTANCE-SCOPE persistence half to this module, because it is
the one place both startup paths already share:

* :func:`restore_persisted_license` loads the licence an operator installed
  through ``/settings/license`` (persisted in ``system_config``) into the
  process's in-memory slot at boot, so a rolling deploy cannot discard it.

A licence installed WHILE a process is running is picked up by
``core.license.refresh_persisted_system_license``: the API calls it on its
plan-resolution and admin-licence paths, and the SAQ worker calls it at the
start of every job (``core.saq_worker._before_process_hook``). Reads are
TTL-throttled, so this is at most one indexed read per window per process.

The restore is fail-open on a DB blip (the request-scoped / per-job refresh
self-heals), but FAIL-CLOSED on the trust anchor: a persisted licence that can
only verify against the built-in development anchor outside debug refuses to
boot rather than silently running a paying customer on Community — the same rule
``MODULO_LICENSE_KEY`` already has (FAR-2091).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from modulo.core import license as license_mod

_log = logging.getLogger(__name__)


def configure_license_trust_anchor(settings: Any) -> None:
    """Apply this deployment's licence trust anchor to the calling process.

    The policy is identical for the API and the worker (FAR-2061 / FAR-2091):

    * ``set_dev_anchor_trusted(settings.debug)`` — the shipped development key
      is a trust anchor only in debug.
    * ``MODULO_LICENSE_PUBLIC_KEY``, when configured, replaces the built-in
      development anchor; a malformed key fails the boot.
    * Outside debug, a configured ``MODULO_LICENSE_KEY`` with no usable anchor
      raises ``DEV_ANCHOR_STARTUP_ERROR``: such a licence can never verify, so
      starting the process would silently run on the Community tier.
    * Booting with no licence at all stays valid — the ordinary self-hosted
      Community deployment.
    * ``check_production_public_key`` is the advisory backstop for a Community
      instance that later receives a licence at runtime; it is fail-open WITH
      a log.

    Raises:
        RuntimeError: the configured anchor is malformed, or a licence is
            configured while only the development anchor is available.
    """
    debug = bool(getattr(settings, "debug", False))

    license_mod.set_dev_anchor_trusted(debug)

    public_key = getattr(settings, "modulo_license_public_key", "") or ""
    if public_key:
        try:
            license_mod.set_public_key(public_key)
        except ValueError as exc:
            raise RuntimeError(
                f"MODULO_LICENSE_PUBLIC_KEY is not a valid Ed25519 public key (expected 64 hex characters): {exc}"
            ) from exc
        _log.info("startup.license_public_key_configured")
    elif not debug:
        _log.warning("startup.default_license_key_in_use")

    # A configured licence that can only be checked against the development
    # anchor can never verify, so fail the boot with the FIX rather than start
    # a process that silently runs on Community. Booting WITHOUT a licence key
    # stays valid — that is the ordinary self-hosted Community deployment.
    license_key = getattr(settings, "modulo_license_key", "") or ""
    if not debug and license_key and license_mod.is_dev_anchor_in_use():
        raise RuntimeError(license_mod.DEV_ANCHOR_STARTUP_ERROR)

    try:
        license_mod.check_production_public_key(settings)
    except Exception:
        _log.warning("startup.production_public_key_check_failed", exc_info=True)


async def restore_persisted_license(settings: Any) -> license_mod.PersistedLicenseState:
    """Restore the persisted instance-scope licence at startup (FAR-2153).

    Called by the API lifespan (after the schema is at head) and by the SAQ
    worker startup hook. A DB failure is FAIL-OPEN with a log: booting must not
    depend on the licence store, and the licence is re-read later by
    :func:`start_persisted_license_refresh_loop` (worker) or by the plan/admin
    resolution paths (API). A trust-anchor miss stays FAIL-CLOSED and propagates
    :class:`modulo.core.license.PersistedLicenseAnchorError`.
    """
    from modulo.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session, session.begin():
            state = await license_mod.restore_persisted_system_license(session, settings)
    except license_mod.PersistedLicenseAnchorError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("startup.license_persistence_restore_failed", exc_info=True)
        return license_mod.PersistedLicenseState()

    if state.is_empty():
        _log.info("startup.no_persisted_license")
    else:
        _log.info(
            "startup.persisted_license_restored active=%s parked=%s",
            state.active_key is not None,
            state.parked_key is not None,
        )
    return state

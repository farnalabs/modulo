"""Cross-org connector health-check sweep (FAR-699).

The connector health-check mechanism (``ConnectorBase.health_check``) and the
status surfacing (``connector_instances.last_health_check_at`` /
``last_health_check_error``, read by ``get_integration_status``) both existed,
but NOTHING ever executed a check outside the manual per-instance API
endpoint — so every integration showed ``last_check: "never"`` forever.

This module owns the sweep the SAQ system cron ticks every 15 minutes:

* lists every ACTIVE connector instance cross-org (system role, BYPASSRLS),
* builds each instance through :class:`ConnectorHub` and calls its
  ``health_check`` (cheap credential probe — never ``query``/``write``),
* persists ``last_health_check_at`` + ``last_health_check_error``
  + ``validation_level`` (FAR-935).

Contract:

* NO data mutation — the only writes are the health columns on the aired
  row (a plain UPDATE, plain columns, no config mutation).
* Credential redaction (FAR-1651Fix3) — every detail persisted into
  ``last_health_check_error`` is credential-scrubbed through the connector's
  own redactor (hub ``_record_skip`` summaries, connector ``HealthResult``
  details, escalated exceptions); with no redactor bound the persisted detail
  degrades to ``_FALLBACK_DETAIL`` (fail-closed), the full message stays in
  the log only.
* Per-instance isolation — one poisoned instance (undecryptable credentials,
  unknown type, exploding connector) is recorded as that instance's error and
  never aborts the rest of the sweep.
* FAR-442 rate budget — the hub is built with the instance's ``org_id`` so
  rate-limited connectors (REST) draw from the org's SHARED Redis budget, not
  an untenant'd per-process bucket.
* FAR-1526 per-org secret read — the instance's org context is bound on the
  session for the check's transaction, so the Fernet secrets backend can
  resolve the ``secrets`` row (it refuses to read without an org context) and
  every secret read stays scoped to that instance's organisation.
* FAR-935 validation level — the sweep computes and writes the current
  validation level for each instance, mirroring the ``last_health_check_at``
  pattern: a failed canary degrades the level automatically.
* FAR-1564 ACL denials are not health failures — a ``ConnectorPermissionError``
  from ``health_check()`` (an allowlist without ``read``) is counted
  ``skipped``: no health columns are written, because the probe never ran and
  a permission answer is not a transport/upstream failure.
* FAR-1682 shell connector truth — the sweep threads a ``RuntimeProviderHub``
  (built the same way the run executor builds one) into every ``ConnectorHub``
  it constructs. ``ShellConnector.health_check()`` reports on whether a runtime
  provider is bound; the runtime provider IS available at runtime, but the sweep
  previously built its hub with none, so every active ``shell`` instance was
  falsely recorded ``Runtime provider not configured``. ADR 044 deprecates
  ShellConnector but keeps existing shell pipelines executable, so the health
  signal must be truthful rather than suppressed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.connectors.base import ConnectorPermissionError
from modulo.core.connector_hub import ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.core.validation_level import connector_baseline_level, resolve_validation_level
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.rls import set_rls_org

logger = logging.getLogger(__name__)

_ERR_DETAIL_LIMIT = 2000

_FALLBACK_DETAIL = "health check failed"


def _bound_detail(detail: str) -> str:
    """NUL-strip and clamp a health-check error detail to the column size."""
    return detail.replace("\x00", "")[:_ERR_DETAIL_LIMIT]


def _build_default_runtime_provider_hub() -> Any:
    """Build the runtime-provider hub the sweep binds to each ``ConnectorHub`` (FAR-1682).

    Mirrors the run executor's construction (``create_default_hub()`` — the
    env-gated ``build_hub`` factory that always registers the ``local`` provider
    and registers ``e2b`` / ``runner_docker`` / ``kubernetes`` when configured).
    The import is lazy so the connector-hub module does not couple to the
    runtime-provider package at import time. The returned hub is disposed by
    ``run_connector_health_checks`` via ``aclose()`` once the sweep finishes.
    """
    from modulo.core.runtime_provider import create_default_hub

    return create_default_hub()


async def _check_instance(
    ci: ConnectorInstance,
    *,
    session: AsyncSession,
    fernet_key: str | None,
    runtime_provider_hub: Any = None,
) -> str:
    """Health-check one instance. Returns "" for ok, else a redacted failure detail.

    FAR-1526: the sweep runs on the cross-org ``modulo_system`` session factory,
    which deliberately carries NO ``app.organisation_id`` (system crons are
    BYPASSRLS and never call ``set_rls_org``). ``FernetSecretsBackend.get_secret``
    requires an org context before it will read the ``secrets`` table — without
    one it raises ``RuntimeError: FernetSecretsBackend: RLS organisation context
    not set``, the hub skips every instance, and the sweep records
    ``ConnectorNotFoundError`` for all of them (observed in prod: all five
    active connectors failed their 2026-10-06 16:00 tick). Binding the
    INSTANCE's org for this transaction scopes the secret read per-org — on
    Postgres via ``set_config(..., is_local=true)`` (cleared at commit, and
    irrelevant to the BYPASSRLS role's own row visibility), on generic
    backends via ``session.info`` — so tenancy is tightened, never weakened.

    When the hub skips an instance (bad credentials, unknown type, exploding
    connector) the recorded detail is the hub's own skip reason (already
    redacted by ``ConnectorHub._record_skip``) rather than the bare
    ``ConnectorNotFoundError`` from the subsequent ``get()``, so the health
    column stays actionable.

    FAR-1651Fix3 redaction boundary: everything this function returns is
    persisted into ``last_health_check_error``. Three layers:

    1. `hub.skipped` summaries are redacted inside the hub.
    2. ``result.detail`` — the connector's own HealthResult detail — is
       additionally scrubbed through the hub's per-instance redactor before
       persistence (connectors redact at source; this is the
       persistence-boundary backstop).
    3. Any exception escaping the hub block (a connector that raises from
       ``health_check()`` despite the contract, a hub ``__aenter__`` failure)
       is redacted through the hub's redactor; with NO redactor bound (the
       failure predated the creds parse — structurally credential-free) the
       detail FALLS CLOSED to ``_FALLBACK_DETAIL`` rather than echoing
       unredacted ``str(exc)``. ``ConnectorPermissionError`` re-raises (the
        FAR-1564 skip class) and ``asyncio.CancelledError`` re-raises as-is, so
        the run-loop semantics for those are unchanged.

    FAR-1682: ``runtime_provider_hub`` is threaded into the ``ConnectorHub`` so
    a ``shell`` instance's ``health_check()`` sees a bound runtime provider
    (the hub the sweep builds once per tick). Without it the deprecated-but-
    still-executable ShellConnector always reported ``Runtime provider not
    configured``.
    """
    await set_rls_org(session, ci.organisation_id)
    secrets_backend = create_secrets_backend(fernet_key=fernet_key, session=session)
    async with ConnectorHub(
        secrets_backend=secrets_backend,
        org_id=str(ci.organisation_id),
        runtime_provider_hub=runtime_provider_hub,
    ) as hub:
        await hub.initialise([ci])
        redactor = hub.credential_redactor_for(ci.id)
        try:
            skip_reason = hub.skipped.get(ci.id)
            if skip_reason is not None:
                return _bound_detail(skip_reason)
            connector = hub.get(ci.id)
            result = await connector.health_check()
        except asyncio.CancelledError:
            raise
        except ConnectorPermissionError:
            raise
        except Exception as exc:
            logger.exception(
                "connector_health_sweep.escalated_failure connector_id=%s type=%s",
                ci.id,
                ci.connector_type_id,
            )
            if redactor is not None:
                return _bound_detail(f"{type(exc).__name__}: {redactor.redact(str(exc))}")
            # No redactor is bound only when the failure crossed the
            # fail-closed propagations before the creds dict was parsed — those
            # exception types carry no credential content, but we cannot prove
            # it from the value, so the persisted detail degrades to the
            # placeholder (fail-closed) and the full message stays in the log.
            return _bound_detail(_FALLBACK_DETAIL)
    if result.ok:
        return ""
    # Fail CLOSED (FAR-1651Fix3): with no redactor bound we cannot scrub the
    # connector's own detail, so persist the placeholder rather than echo it
    # unredacted. This mirrors the escalated-exception arm above; unreachable
    # today (the hub binds the redactor before building a connector), but the
    # persistence boundary must not fall open if that ever changes.
    detail = redactor.redact(result.detail) if redactor is not None else _FALLBACK_DETAIL
    return _bound_detail(detail or _FALLBACK_DETAIL)


async def run_connector_health_checks(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fernet_key: str | None = None,
) -> dict[str, int | str]:
    """Run one sweep across every active connector instance, cross-org.

    Returns a summary dict: ``checked`` / ``healthy`` / ``unhealthy`` /
    ``skipped`` / ``last_run_at``. Never raises for a single bad instance —
    each failure is recorded on that instance's row only.

    An ACL denial (a non-empty ``allowed_operations`` allowlist without
    ``read``, raised by ``_TracedConnector.health_check``) is NOT a health
    failure: it is a PERMISSION answer about that connector's configuration,
    the same reasoning the REST ``GET /connectors/{id}/health`` route uses to
    answer 403 rather than 502. Such an instance is counted ``skipped``, its
    health columns are left untouched (the probe never ran, so the sweep must
    not claim a new health state), and the denial is logged.

    FAR-1682: a single ``RuntimeProviderHub`` is built for the tick (the same
    way the run executor builds one) and threaded into every instance's
    ``ConnectorHub``, then disposed in a ``finally`` so provider-owned clients
    are released. This is what makes a ``shell`` connector's ``health_check()``
    report truthfully instead of always seeing no runtime provider bound.
    """
    checked = 0
    healthy = 0
    skipped = 0
    checked_at = datetime.now(UTC)

    async with session_factory() as session, session.begin():
        result = await session.execute(select(ConnectorInstance).where(ConnectorInstance.status == "active"))
        instances = list(result.scalars().all())

    runtime_provider_hub = _build_default_runtime_provider_hub()
    try:
        for ci in instances:
            checked += 1
            detail = ""
            async with session_factory() as write_session, write_session.begin():
                try:
                    detail = await _check_instance(
                        ci,
                        session=write_session,
                        fernet_key=fernet_key,
                        runtime_provider_hub=runtime_provider_hub,
                    )
                    if detail == "":
                        healthy += 1
                except asyncio.CancelledError:
                    raise
                except ConnectorPermissionError as exc:
                    # FAR-1564: an ACL denial is a PERMISSION answer about this
                    # connector's allowed_operations, not a transport/upstream
                    # failure — recording it as last_health_check_error would mark
                    # a perfectly healthy connector unhealthy and mask the real
                    # cause. Skip it distinctly (no health columns written).
                    skipped += 1
                    logger.warning(
                        "connector_health_sweep.acl_denied connector_id=%s: %s",
                        ci.id,
                        exc,
                    )
                    continue
                except Exception as exc:
                    # Residual backstop (FAR-1651Fix3): after _check_instance's own
                    # redaction boundary, only DB-layer failures can reach here
                    # (set_rls_org / session factory / secrets-backend
                    # construction — no connector credential is ever bound at this
                    # layer). Fail closed anyway: the full message goes to the log,
                    # the PERSISTED detail degrades to the placeholder, because this
                    # column is an operator-visible persistence boundary.
                    logger.exception(
                        "connector_health_sweep.residual_failure connector_id=%s type=%s",
                        ci.id,
                        ci.connector_type_id,
                    )
                    detail = _bound_detail(f"{type(exc).__name__}: {_FALLBACK_DETAIL}")
                await write_session.execute(
                    update(ConnectorInstance)
                    .where(ConnectorInstance.id == ci.id)
                    .values(
                        last_health_check_at=checked_at,
                        last_health_check_error=None if detail == "" else detail,
                        validation_level=resolve_validation_level(
                            connector_baseline_level(ci.connector_type_id),
                            last_health_check_at=checked_at,
                            last_health_check_error=None if detail == "" else detail,
                        ),
                    )
                )
    finally:
        # FAR-1682: dispose the per-tick runtime-provider hub so provider-owned
        # clients (e.g. E2B / Docker) are released — the hub is shared across
        # every instance in this sweep, so it must be closed exactly once here.
        await runtime_provider_hub.aclose()

    return {
        "checked": checked,
        "healthy": healthy,
        "unhealthy": checked - healthy - skipped,
        "skipped": skipped,
        "last_run_at": checked_at.isoformat(),
    }

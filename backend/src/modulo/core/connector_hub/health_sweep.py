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
* persists ``last_health_check_at`` + ``last_health_check_error``.

Contract:

* NO data mutation — the only writes are the two health columns on the aired
  row (a plain UPDATE, plain columns, no config mutation).
* Per-instance isolation — one poisoned instance (undecryptable credentials,
  unknown type, exploding connector) is recorded as that instance's error and
  never aborts the rest of the sweep.
* FAR-442 rate budget — the hub is built with the instance's ``org_id`` so
  rate-limited connectors (REST) draw from the org's SHARED Redis budget, not
  an untenant'd per-process bucket.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.connector_hub import ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.models.connector_instance import ConnectorInstance

_ERR_DETAIL_LIMIT = 2000

_FALLBACK_DETAIL = "health check failed"


def _bound_detail(detail: str) -> str:
    """NUL-strip and clamp a health-check error detail to the column size."""
    return detail.replace("\x00", "")[:_ERR_DETAIL_LIMIT]


async def _check_instance(
    ci: ConnectorInstance,
    *,
    session: AsyncSession,
    fernet_key: str | None,
) -> str:
    """Health-check one instance. Returns "" for ok, else the failure detail."""
    secrets_backend = create_secrets_backend(fernet_key=fernet_key, session=session)
    async with ConnectorHub(secrets_backend=secrets_backend, org_id=str(ci.organisation_id)) as hub:
        await hub.initialise([ci])
        connector = hub.get(ci.id)
        result = await connector.health_check()
    return "" if result.ok else _bound_detail(result.detail or _FALLBACK_DETAIL)


async def run_connector_health_checks(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fernet_key: str | None = None,
) -> dict[str, int | str]:
    """Run one sweep across every active connector instance, cross-org.

    Returns a summary dict: ``checked`` / ``healthy`` / ``unhealthy`` /
    ``last_run_at``. Never raises for a single bad instance — each failure is
    recorded on that instance's row only.
    """
    checked = 0
    healthy = 0
    checked_at = datetime.now(UTC)

    async with session_factory() as session, session.begin():
        result = await session.execute(select(ConnectorInstance).where(ConnectorInstance.status == "active"))
        instances = list(result.scalars().all())

    for ci in instances:
        checked += 1
        detail = ""
        async with session_factory() as write_session, write_session.begin():
            try:
                detail = await _check_instance(ci, session=write_session, fernet_key=fernet_key)
                if detail == "":
                    healthy += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = _bound_detail(f"{type(exc).__name__}: {exc}")
            await write_session.execute(
                update(ConnectorInstance)
                .where(ConnectorInstance.id == ci.id)
                .values(
                    last_health_check_at=checked_at,
                    last_health_check_error=None if detail == "" else detail,
                )
            )

    return {
        "checked": checked,
        "healthy": healthy,
        "unhealthy": checked - healthy,
        "last_run_at": checked_at.isoformat(),
    }

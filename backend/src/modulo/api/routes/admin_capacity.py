"""Admin DB capacity routes (FAR-425/426) — the live capacity monitor.

Exposes ``GET /api/v1/admin/db-capacity`` returning the live DB capacity status
(``capacity_percent`` / ``mode`` / ``alert_level`` / ``used_bytes`` /
``capacity_bytes``) from :func:`modulo.db.capacity.db_capacity_status`. This is
the source the frontend banner polls to alarm on a running-out DB volume before
it hits the 98% hard-stop.

Authz: system OR org admin (via ``require_system_or_org_admin``). The read is
instance-wide capacity, so the same permission set as the housekeeping surface
applies. Fail-open: a measurement error returns ``capacity_percent=None`` /
``alert_level="ok"`` rather than a 5xx, so the banner degrades to quiet.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from modulo.api.dependencies import require_system_or_org_admin
from modulo.auth.jwt import TenantPrincipal
from modulo.db.capacity import db_capacity_status

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/db-capacity", tags=["admin-capacity"])


class DbCapacityResponse(BaseModel):
    capacity_percent: float | None
    mode: str
    alert_level: str
    used_bytes: int
    capacity_bytes: int | None


@router.get("", response_model=DbCapacityResponse)
async def get_db_capacity(
    principal: TenantPrincipal = require_system_or_org_admin("housekeeping.manage"),
) -> DbCapacityResponse:
    """Return the live DB capacity status (the monitoring source of truth)."""
    # Lazy import — the shared engine is only built once settings are present,
    # not at app-import time.
    from modulo.db.session import get_shared_engine

    try:
        status: dict[str, Any] = await db_capacity_status(get_shared_engine())
    except Exception:
        # Fail-open: the monitor must never 5xx over its own measurement.
        _log.exception("admin_capacity.get_db_capacity")
        status = {
            "capacity_percent": None,
            "mode": "disabled",
            "alert_level": "ok",
            "used_bytes": 0,
            "capacity_bytes": None,
        }
    return DbCapacityResponse(**status)

"""Deployment info endpoint — returns build metadata."""

import os
import time
from datetime import UTC, datetime

from fastapi import APIRouter

from modulo.version import get_version

router = APIRouter(prefix="/api/v1/deployment", tags=["deployment"])

_start_time = time.time()
_started_at = datetime.now(UTC)


@router.get("")
async def deployment_info():
    """Return deployment metadata for operational visibility."""
    return {
        "version": get_version(),
        "uptime_seconds": int(time.time() - _start_time),
        "started_at": _started_at.isoformat(),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "hostname": os.environ.get("HOSTNAME", ""),
        "environment": os.environ.get("MODULO_ENV", "development"),
    }

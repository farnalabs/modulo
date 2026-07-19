"""Deployment info endpoint — returns build and runtime metadata."""

import logging
import os
import time
from datetime import UTC, datetime

from fastapi import APIRouter

from modulo.api.db_error_handling import handle_db_errors
from modulo.version import get_version

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/deployment", tags=["deployment"])

_start_time = time.time()
_started_at = datetime.now(UTC)


@router.get("")
@handle_db_errors("deployment.deployment_info")
async def deployment_info() -> dict[str, object]:
    """Return deployment metadata for operational visibility.

    Build-time values (git_sha, git_branch, build_timestamp, etc.) are injected
    via Docker build args in the CI/CD pipeline.  If absent they fall back to
    empty strings so the endpoint is always safe to call.
    """
    return {
        "version": get_version(),
        "uptime_seconds": int(time.time() - _start_time),
        "started_at": _started_at.isoformat(),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "hostname": os.environ.get("HOSTNAME", ""),
        "environment": os.environ.get("MODULO_ENV", "development"),
        "git_sha": os.environ.get("GIT_SHA", ""),
        "git_branch": os.environ.get("GIT_BRANCH", ""),
        "git_commit_timestamp": os.environ.get("GIT_COMMIT_TIMESTAMP", ""),
        "git_commit_message": os.environ.get("GIT_COMMIT_MESSAGE", ""),
        "build_timestamp": os.environ.get("BUILD_TIMESTAMP", ""),
        "ci_job_url": os.environ.get("CI_JOB_URL", ""),
    }

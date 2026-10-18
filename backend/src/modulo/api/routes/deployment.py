"""Deployment info endpoint — returns build and runtime metadata."""

import os
import time
from datetime import UTC, datetime

from fastapi import APIRouter

from modulo.api.db_error_handling import handle_db_errors
from modulo.version import get_version

router = APIRouter(prefix="/api/v1/deployment", tags=["deployment"])

_start_time = time.time()
_started_at = datetime.now(UTC)


@router.get("")
@handle_db_errors("deployment.deployment_info")
async def deployment_info() -> dict[str, object]:
    """Return deployment metadata for operational visibility.

    Served unauthenticated on purpose: the CI/CD deploy pipelines
    (deploy.yml, rc-validate.yml, deploy-watchdog.yml,
    deploy-staleness-check.yml) call it without a principal to verify which
    build is live. The repo is public, so the git metadata is already
    world-readable. Only genuinely non-public locals (hostname, ci_job_url)
    are omitted. If values are absent they fall back to empty strings so the
    endpoint is always safe to call.
    """
    # The deploy pipelines consume only ``git_sha`` / ``build_timestamp`` /
    # ``version`` (deploy.yml, rc-validate.yml, deploy-watchdog.yml,
    # deploy-staleness-check.yml). ``git_branch`` and ``git_commit_message``
    # are deliberately NOT returned: they are free text from the DEPLOYMENT's
    # source repository, which is not necessarily public (a self-hosted
    # customer may build from a private fork), and no consumer reads them
    # (FAR-1975). Keep the response to build identifiers that are useless
    # without the repo itself.
    return {
        "version": get_version(),
        "uptime_seconds": int(time.time() - _start_time),
        "started_at": _started_at.isoformat(),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "environment": os.environ.get("MODULO_ENV", "development"),
        "git_sha": os.environ.get("GIT_SHA", ""),
        "git_commit_timestamp": os.environ.get("GIT_COMMIT_TIMESTAMP", ""),
        "build_timestamp": os.environ.get("BUILD_TIMESTAMP", ""),
    }

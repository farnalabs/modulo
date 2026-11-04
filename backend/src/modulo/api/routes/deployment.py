"""Deployment info endpoint — returns build and runtime metadata."""

import hmac
import os
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_system_db_session
from modulo.core.system_audit_logger import append_system_audit_event
from modulo.db.crud._gate_advisory_lock import acquire_deployment_drain_lock
from modulo.db.crud.run import count_active_runner_dispatches_for_deployment
from modulo.db.crud.system_config import update_config
from modulo.db.models.system_config import SystemConfig
from modulo.version import get_version

_DRAIN_KEY = "deployment_drain"
_DRAIN_TOKEN_ENV = "MODULO_DEPLOY_DRAIN_TOKEN"


def _require_drain_token(authorization: str | None) -> None:
    expected = os.environ.get(_DRAIN_TOKEN_ENV, "")
    if not expected or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="deployment drain control unavailable",
        )
    supplied = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid deployment drain credential")


def _drain_value(active: bool) -> dict[str, object]:
    return {"active": active, "updated_at": datetime.now(UTC).isoformat(), "source": "deploy-workflow"}


async def _set_drain(session: AsyncSession, active: bool) -> None:
    await acquire_deployment_drain_lock(session)
    await update_config(session, _DRAIN_KEY, _drain_value(active))
    await append_system_audit_event(
        session,
        event_type="deployment_drain_changed",
        resource_type="deployment",
        payload_json={"active": active, "source": "deploy-workflow"},
    )


router = APIRouter(prefix="/api/v1/deployment", tags=["deployment"])

_start_time = time.time()
_started_at = datetime.now(UTC)


@router.get("", include_in_schema=True)
@handle_db_errors("deployment.deployment_info")
async def deployment_info(
    session: AsyncSession = Depends(get_system_db_session),
) -> dict[str, object]:
    """Return deployment metadata for operational visibility.

    Served unauthenticated on purpose: the CI/CD deploy pipelines
    (deploy.yml, rc-validate.yml, deploy-watchdog.yml,
    deploy-staleness-check.yml) call it without a principal to verify which
    build is live. Only public-safe build identifiers are returned; source-repo
    free text (git_branch, git_commit_message) and other non-public locals
    (hostname, ci_job_url) are omitted. If values are absent they fall back to
    empty strings so the endpoint is always safe to call.
    """
    # The deploy pipelines consume only ``git_sha`` / ``build_timestamp`` /
    # ``version`` (deploy.yml, rc-validate.yml, deploy-watchdog.yml,
    # deploy-staleness-check.yml). ``git_branch`` and ``git_commit_message``
    # are deliberately NOT returned: they are free text from the DEPLOYMENT's
    # source repository, which is not necessarily public (a self-hosted
    # customer may build from a private fork), and no consumer reads them
    # (FAR-1975). Keep the response to build identifiers that are useless
    # without the repo itself.
    active_sandbox_runs = await count_active_runner_dispatches_for_deployment(session)
    drain = await session.execute(select(SystemConfig.value).where(SystemConfig.key == _DRAIN_KEY))
    drain_value = drain.scalar_one_or_none()
    return {
        "version": get_version(),
        "uptime_seconds": int(time.time() - _start_time),
        "started_at": _started_at.isoformat(),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "environment": os.environ.get("MODULO_ENV", "development"),
        "git_sha": os.environ.get("GIT_SHA", ""),
        "git_commit_timestamp": os.environ.get("GIT_COMMIT_TIMESTAMP", ""),
        "build_timestamp": os.environ.get("BUILD_TIMESTAMP", ""),
        "active_sandbox_runs": active_sandbox_runs,
        "deployment_drain": bool(isinstance(drain_value, dict) and drain_value.get("active") is True),
    }


@router.post("/drain")
@handle_db_errors("deployment.drain")
async def set_deployment_drain(
    active: bool,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_system_db_session),
) -> dict[str, object]:
    """Enter or leave the deploy drain; workflow failures must fail closed."""
    _require_drain_token(authorization)
    async with session.begin():
        await _set_drain(session, active)
    return {"active": active}

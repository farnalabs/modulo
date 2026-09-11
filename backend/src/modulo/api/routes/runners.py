"""Runners aggregate REST API (FAR-591, D5) — the Runners page's read model.

``GET /api/v1/runners/status`` is the ONE endpoint the page's persistent
status strip + Profiles tab consume: per-machine cached probe rows (mapped
to strip states, staleness computed server-side), the worst-of aggregate,
per-profile health + template-drift, and the org's sandbox-concurrency
contract with the engine-resource preflight. EVERYTHING here reads the
``runner_probe_cache`` table — never a synchronous engine probe on the
request path.

``POST /api/v1/runners/profiles/{profile_id}/apply-template`` refreshes the
template-owned fields of a drifted seeded Bundled Runner row (D4's live
drift helper decides; this route only applies).

Authz: the page carries the admin-scoped concurrency setting — the status
read requires ``environment_profile.list`` (profiles are readable by
operators); apply-template requires ``environment_profile.update``.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import (
    MSG_DATABASE_ERROR_OCCURRED_PLEASE,
    MSG_ENVIRONMENT_PROFILE_NOT_FOUND,
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_UNEXPECTED_ERROR,
)
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.bundled_runner import template_drift_status
from modulo.core.bundled_runner.health_probe import (
    PER_CONTAINER_CPU,
    PER_CONTAINER_MEM_MB,
    StripState,
    aggregate_strip_state,
    strip_state_for_row,
)
from modulo.db.crud.environment_profile import (
    apply_bundled_runner_template,
    list_environment_profiles,
)
from modulo.db.crud.run import count_active_runner_dispatches_for_org, get_sandbox_concurrency_limit
from modulo.db.crud.runner_probe import (
    PROBE_INTERVAL_SECONDS,
    PROBE_RETENTION_SECONDS,
    PROBE_STALENESS_THRESHOLD_SECONDS,
    list_runner_probe_cache,
    probe_age_seconds,
)
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

_CODE_RUNNERS_STATUS = "runners.status"
_CODE_RUNNERS_APPLY_TEMPLATE = "runners.apply_template"

#: qa F17: the preflight states are a closed contract — Literal types pin
#: the wire shape AND the frontend derives its switches from the same union.
PreflightState = Literal["ok", "exceeds_cpu", "exceeds_mem", "exceeds_cpu_and_mem", "uncapped", "unknown"]


router = APIRouter(
    prefix="/api/v1/runners",
    tags=["runners"],
    dependencies=[require_feature("environment_profiles")],
)


class MachineProbeResponse(BaseModel):
    machine_id: str
    state: StripState
    engine_reachable: bool
    images_present: bool | None = None
    probed_at: datetime
    age_seconds: int
    engine_info: dict[str, Any] = Field(default_factory=dict)
    image_checks: dict[str, Any] = Field(default_factory=dict)
    probe_error: str | None = None


class ProfileDriftResponse(BaseModel):
    is_seeded: bool = False
    drifted: bool = False
    drifted_fields: list[str] = Field(default_factory=list)


class ConcurrencyPreflightResponse(BaseModel):
    state: PreflightState
    detail: str | None = None
    engine_cpu_count: int | None = None
    engine_mem_total_mb: int | None = None
    needed_cpu: float | None = None
    needed_mem_mb: int | None = None


class ConcurrencyContractResponse(BaseModel):
    sandbox_concurrency_limit: int | None = None
    is_default: bool = False
    preflight: ConcurrencyPreflightResponse


class ProfileHealthResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    provider_type: str
    image_ref: str | None = None
    #: The resolved profile config (resource limits, hardening, timeout) —
    #: the Bundled Runner detail panel renders its resource-limit summary.
    config_json: dict[str, Any] = Field(default_factory=dict)
    network_policy: str
    persistence_policy: str
    status: str
    #: Worst-of machine strip state for runner_docker rows; null when no
    #: probe applies (E2B / local tiers are not engine-probed).
    health_state: StripState | None = None
    #: True when the row can dispatch right now (healthy OR an un-probed
    #: tier); the editor greys/hides rows where this is False.
    available: bool
    placeholder_digest: bool = False
    drift: ProfileDriftResponse = Field(default_factory=ProfileDriftResponse)
    #: How many live workspaces are currently provisioned against this
    #: profile. Markers carry NO profile id (node-level binding is post-GA),
    #: so this is the ORG-scoped count of active (running) runs bearing a
    #: live ``runner_docker`` dispatch marker — the same value on every
    #: Bundled profile of the org until per-profile markers land.
    active_workspaces: int = 0


class RunnersStatusResponse(BaseModel):
    aggregate_state: StripState
    probe_interval_seconds: int
    staleness_threshold_seconds: int
    machines: list[MachineProbeResponse] = Field(default_factory=list)
    profiles: list[ProfileHealthResponse] = Field(default_factory=list)
    concurrency: ConcurrencyContractResponse


def _preflight(limit_cap: int | None, engine_infos: list[dict[str, Any]]) -> ConcurrencyPreflightResponse:
    """Engine-resource preflight (D5): limit x per-container limits vs ``/info``.

    ``null`` cap → ``uncapped`` (cannot assess host headroom); no engine info
    (engine unreachable / never probed) → ``unknown``.

    qa F7: multi-machine deployments aggregate WORST-OF across machines —
    the minimum reported CPU count and the minimum reported memory — since
    dispatches can land on ANY machine; sizing against the biggest engine
    would overcommit the smallest one.
    """
    if limit_cap is None:
        return ConcurrencyPreflightResponse(
            state="uncapped",
            detail="No concurrency cap is set — host headroom cannot be assessed.",
        )
    cpu_counts = [info["cpu_count"] for info in engine_infos if info.get("cpu_count") is not None]
    mem_totals = [info["mem_total_mb"] for info in engine_infos if info.get("mem_total_mb") is not None]
    cpu_count = min(cpu_counts) if cpu_counts else None
    mem_total_mb = min(mem_totals) if mem_totals else None
    if cpu_count is None and mem_total_mb is None:
        return ConcurrencyPreflightResponse(
            state="unknown",
            detail="Engine resources are unknown (no recent probe result).",
        )
    needed_cpu = limit_cap * PER_CONTAINER_CPU
    needed_mem_mb = limit_cap * PER_CONTAINER_MEM_MB
    over_cpu = cpu_count is not None and needed_cpu > cpu_count
    over_mem = mem_total_mb is not None and needed_mem_mb > mem_total_mb
    if over_cpu and over_mem:
        state = "exceeds_cpu_and_mem"
    elif over_cpu:
        state = "exceeds_cpu"
    elif over_mem:
        state = "exceeds_mem"
    else:
        state = "ok"
    return ConcurrencyPreflightResponse(
        state=state,
        detail=None if state == "ok" else "The concurrency cap exceeds the engine's reported resources.",
        engine_cpu_count=cpu_count,
        engine_mem_total_mb=mem_total_mb,
        needed_cpu=needed_cpu,
        needed_mem_mb=needed_mem_mb,
    )


def _profile_health(
    profile: EnvironmentProfile,
    worst_state: StripState | None,
    active_workspaces: int = 0,
) -> ProfileHealthResponse:
    from modulo.db.bundled_runner_template import is_placeholder_bundled_runner_image_ref

    drift = template_drift_status(profile)
    is_runner = profile.provider_type == "runner_docker"
    state = worst_state if is_runner else None
    placeholder = is_placeholder_bundled_runner_image_ref(profile.image_ref)
    # Availability: healthy (or an un-probed tier) dispatches; the
    # placeholder digest can NEVER provision (no registry image exists).
    if placeholder:
        available = False
    elif state is None:
        available = True
    else:
        available = state == "healthy"
    return ProfileHealthResponse(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        provider_type=profile.provider_type,
        image_ref=profile.image_ref,
        config_json=profile.config_json or {},
        network_policy=profile.network_policy,
        persistence_policy=profile.persistence_policy,
        status=profile.status,
        health_state=state,
        available=available,
        placeholder_digest=placeholder,
        drift=ProfileDriftResponse(**drift),
        active_workspaces=active_workspaces if is_runner else 0,
    )


@router.get("/status", response_model=RunnersStatusResponse)
@handle_db_errors(_CODE_RUNNERS_STATUS)
async def get_runners_status(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.list"),
) -> RunnersStatusResponse:
    now = datetime.now(UTC)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            all_rows = await list_runner_probe_cache(session, org_id=principal.organisation_id)
            # qa F8: orphaned machine rows are pruned by the probe tick, but
            # the read ALSO bounds by the same retention window so a corpse
            # row (a machine decommissioned mid-window) never pins the
            # worst-of aggregate to "stale" between ticks.
            retention_cutoff = now - timedelta(seconds=PROBE_RETENTION_SECONDS)
            rows = [row for row in all_rows if row.probed_at and row.probed_at >= retention_cutoff]
            # qa F18: paginate the profile read — the single page-1 call
            # silently capped at 100 profiles; loop until the org's rows are
            # exhausted.
            profiles_page: list[EnvironmentProfile] = []
            page = 1
            while True:
                result_page = await list_environment_profiles(session, page=page, page_size=100)
                profiles_page.extend(result_page.items)
                if page * 100 >= result_page.total or not result_page.items:
                    break
                page += 1
            contract = await get_sandbox_concurrency_limit(session, principal.organisation_id)
            # FAR-771: active workspace count = org-scoped running runs with
            # a live runner_docker dispatch marker. Markers carry NO profile
            # id (node-level binding is post-GA), so the value is the same on
            # every Bundled profile of the org — computed once here and
            # threaded through the per-profile detail response.
            active_workspaces = await count_active_runner_dispatches_for_org(session, principal.organisation_id)
    except ProgrammingError:
        _log.exception(_CODE_RUNNERS_STATUS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_RUNNERS_STATUS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unexpected error reading runners status: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    # qa F18: the per-row strip state is computed once here and reused via
    # ``machine_states`` below for both the worst-of aggregate AND the
    # per-profile health — the old code computed the same states three times.
    machines = [
        MachineProbeResponse(
            machine_id=row.machine_id,
            state=strip_state_for_row(
                engine_reachable=row.engine_reachable,
                images_present=row.images_present,
                probed_at=row.probed_at,
                now=now,
            ),
            engine_reachable=row.engine_reachable,
            images_present=row.images_present,
            probed_at=row.probed_at,
            age_seconds=probe_age_seconds(row.probed_at, now),
            engine_info=row.engine_info_json or {},
            image_checks=row.image_checks_json or {},
            probe_error=row.probe_error,
        )
        for row in rows
    ]
    machine_states = [m.state for m in machines]
    aggregate = aggregate_strip_state(machine_states)

    # Per-profile health: runner_docker rows inherit the worst-of machine
    # state (per-machine rows collapse per-profile — the profile executes on
    # whichever machine dispatches, so worst-of is the safe display). With
    # NO machine rows at all (the probe never ran) the aggregate reads
    # "stale" — absent cache must NOT read healthy (the strip shows
    # "status unknown", and the profile row shows the same unknown state).
    profiles = [
        _profile_health(p, aggregate if p.provider_type == "runner_docker" else None, active_workspaces)
        for p in profiles_page
    ]

    engine_infos = [m.engine_info for m in machines if m.engine_info]
    concurrency = ConcurrencyContractResponse(
        sandbox_concurrency_limit=contract.cap,
        is_default=contract.is_default,
        preflight=_preflight(contract.cap, engine_infos),
    )

    return RunnersStatusResponse(
        aggregate_state=aggregate,
        probe_interval_seconds=PROBE_INTERVAL_SECONDS,
        staleness_threshold_seconds=PROBE_STALENESS_THRESHOLD_SECONDS,
        machines=machines,
        profiles=profiles,
        concurrency=concurrency,
    )


class ApplyTemplateResponse(BaseModel):
    id: uuid.UUID
    name: str
    image_ref: str | None = None
    network_policy: str
    persistence_policy: str
    config_json: dict[str, Any]
    drift: ProfileDriftResponse


def _apply_response(p: EnvironmentProfile) -> ApplyTemplateResponse:
    drift = template_drift_status(p)
    return ApplyTemplateResponse(
        id=p.id,
        name=p.name,
        image_ref=p.image_ref,
        network_policy=p.network_policy,
        persistence_policy=p.persistence_policy,
        config_json=p.config_json,
        drift=ProfileDriftResponse(**drift),
    )


@router.post("/profiles/{profile_id}/apply-template", response_model=ApplyTemplateResponse)
@handle_db_errors(_CODE_RUNNERS_APPLY_TEMPLATE)
async def apply_template(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.update"),
) -> ApplyTemplateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            profile = await apply_bundled_runner_template(session, profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ProgrammingError:
        _log.exception(_CODE_RUNNERS_APPLY_TEMPLATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_RUNNERS_APPLY_TEMPLATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception as exc:
        _log.exception("Unexpected error applying runner template: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=MSG_ENVIRONMENT_PROFILE_NOT_FOUND + " (or not a seeded template row)",
        )
    return _apply_response(profile)

"""Admin license endpoint — view, update, and issue the deployment license key."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_permission, require_system_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.feature_flags import resolve_effective_license
from modulo.core.license import (
    DEFAULT_RENEWAL_MODE,
    LICENCE_GRACE_DAYS,
    REASON_EXPIRED,
    REASON_MALFORMED,
    REASON_SIGNATURE_MISMATCH,
    LicenseData,
    LicenseError,
    LicenseValidation,
    parse_and_verify,
    store_license,
)
from modulo.core.license_signing import TEAM_FEATURES, generate_team_license
from modulo.core.stripe_fulfilment import email_team_license
from modulo.db.crud.organisation import get_organisation
from modulo.db.models.organisation import Organisation
from modulo.settings import Settings, get_settings

_CODE_ORG_LICENSE_MANAGE = "org.license.manage"
_CODE_LICENSE_GET_FAILED = "license.get_failed"

#: Redis key prefix for the per-org licence-status cache (FAR-1719).
_LICENSE_CACHE_KEY_PREFIX = "license:"
#: TTL for the per-org licence-status cache, in seconds.
_LICENSE_CACHE_TTL_SECONDS = 60

# ---------------------------------------------------------------------------
# Renewal state (FAR-2075)
#
# The BACKEND is the single source of truth for what state a licence is in.
# The frontend maps :class:`LicenseStatusResponse.licence_state` to copy and
# MUST NOT re-derive it from ``expires_at``: two implementations of the same
# 14-day boundary will diverge, and the boundary is the whole feature.
# ---------------------------------------------------------------------------

#: No licence at all — the instance runs on the Community tier.
LICENCE_STATE_NONE = "none"
#: More than ``LICENCE_GRACE_DAYS`` before expiry.
LICENCE_STATE_ACTIVE = "active"
#: Within ``LICENCE_GRACE_DAYS`` before expiry — the renewal countdown.
LICENCE_STATE_RENEWAL_DUE = "renewal_due"
#: Past expiry but still inside the fixed delivery grace; capability is on.
LICENCE_STATE_GRACE = "grace"
#: Past expiry + grace; Team capability is off.
LICENCE_STATE_LAPSED = "lapsed"

#: The complete ``licence_state`` vocabulary. A consumer may branch on these
#: five values only; an unrecognised value is a backend defect.
LICENCE_STATES: frozenset[str] = frozenset(
    {
        LICENCE_STATE_NONE,
        LICENCE_STATE_ACTIVE,
        LICENCE_STATE_RENEWAL_DUE,
        LICENCE_STATE_GRACE,
        LICENCE_STATE_LAPSED,
    }
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/license", tags=["admin-license"])


def derive_licence_state(
    expires_at: str | None,
    *,
    now: datetime | None = None,
) -> tuple[int | None, str]:
    """Return ``(days_until_expiry, licence_state)`` for a licence expiry.

    The renewal countdown and the delivery grace are derived HERE, once:

    - ``active``      — more than ``LICENCE_GRACE_DAYS`` before expiry
    - ``renewal_due`` — within ``LICENCE_GRACE_DAYS`` before expiry, and at it
    - ``grace``       — past expiry, still inside the delivery grace
    - ``lapsed``      — past ``expires_at + LICENCE_GRACE_DAYS`` (capability off)

    The state is derived from the exact ``timedelta``, never from the integer,
    so a fractional day at a boundary cannot round the state the wrong way.

    ``days_until_expiry`` is whole days rounded DOWN (``13`` when 13.9 days
    remain), so it turns negative as soon as the licence is past its expiry.
    It is DISPLAY ONLY — ``licence_state`` is authoritative at a boundary.

    An absent/empty ``expires_at`` is a perpetual licence: ``(None, active)``.
    An unparseable one cannot be counted down and its capability cannot be
    established; it is logged and resolves to ``lapsed`` so the admin is told
    to install a valid key rather than shown a silently reassuring "active".
    """
    if not expires_at:
        return None, LICENCE_STATE_ACTIVE

    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        logger.warning("license.expiry_unparseable value=%r", expires_at)
        return None, LICENCE_STATE_LAPSED

    reference = now if now is not None else datetime.now(expiry.tzinfo or UTC)
    remaining = expiry - reference
    days_until_expiry = math.floor(remaining.total_seconds() / 86_400)

    grace = timedelta(days=LICENCE_GRACE_DAYS)
    if remaining > grace:
        return days_until_expiry, LICENCE_STATE_ACTIVE
    if remaining >= timedelta(0):
        return days_until_expiry, LICENCE_STATE_RENEWAL_DUE
    if remaining >= -grace:
        return days_until_expiry, LICENCE_STATE_GRACE
    return days_until_expiry, LICENCE_STATE_LAPSED


def licence_grace_ends_at(expires_at: str | None) -> str | None:
    """ISO timestamp at which the fixed delivery grace ends, or ``None``.

    The UI renders this deadline instead of adding ``LICENCE_GRACE_DAYS`` to
    ``expires_at`` itself — one implementation of the grace boundary.
    """
    if not expires_at:
        return None
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return None
    return (expiry + timedelta(days=LICENCE_GRACE_DAYS)).isoformat()


def _renewal_claims(data: LicenseData) -> dict[str, Any]:
    """Read the signed renewal claims off a resolved licence, unmodified.

    ``LicenseData`` carries the claims from the licence-payload slice of
    FAR-2075 (PR #2244), which is the ONLY place they are parsed and validated
    (``renewal_mode`` defaults to ``"manual"`` there for a key minted before
    that slice). This route passes the typed values through so the admin view
    cannot drift from the signed payload's meaning.
    """
    return {
        "renewal_mode": data.renewal_mode,
        "term_months": data.term,
        "licence_id": data.licence_id,
        "issued_at": data.issued_at,
    }


class LicenseStatusResponse(BaseModel):
    has_license: bool
    tier: str = "community"
    features: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    org_id: str | None = None
    # -- FAR-2075 renewal surface (all backend-derived) ---------------------
    #: ``"auto"`` when the signed key says Farnalabs mints the replacement,
    #: otherwise ``"manual"`` (the signed-payload default for an older key —
    #: ``auto`` is never assumed: it promises a key already on its way).
    renewal_mode: str = DEFAULT_RENEWAL_MODE
    #: Licence term in months; ``None`` for a key minted before FAR-2075.
    term_months: int | None = None
    #: Opaque stable licence identifier; ``None`` before FAR-2075.
    licence_id: str | None = None
    #: ISO 8601 issue timestamp; ``None`` before FAR-2075.
    issued_at: str | None = None
    #: Whole days to expiry (negative once past it); ``None`` with no expiry.
    days_until_expiry: int | None = None
    #: ISO timestamp at which the delivery grace ends (capability stops).
    #: Backend-derived so the UI never adds ``LICENCE_GRACE_DAYS`` itself.
    grace_ends_at: str | None = None
    #: One of :data:`LICENCE_STATES` — the frontend renders this, never a date comparison.
    licence_state: str = LICENCE_STATE_NONE


class LicenseUploadRequest(BaseModel):
    license_key: str = Field(min_length=1)


class LicenseUploadResponse(BaseModel):
    status: str
    tier: str
    features: list[str]
    expires_at: str | None = None
    org_id: str | None = None


class LicenseIssueRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    term_months: int = Field(default=12, ge=1, le=120)
    features: list[str] | None = None
    email: str | None = None


class LicenseIssueResponse(BaseModel):
    license_key: str
    expires_at: str
    org_name: str
    tier: str
    features: list[str]
    org_id: str


def _license_failure_body(validation: LicenseValidation) -> dict[str, object]:
    """Structured, actionable body for a licence key that failed verification.

    ``detail`` stays a STRING so every existing consumer (admin UI
    ``formatApiError``, BDD steps) keeps rendering it, while ``reason`` gives
    machine-readable classification and ``hint`` names the likely cause — an
    anchor mismatch being the most common one (FAR-2091).
    """
    detail = validation.error or "Invalid license key"
    if validation.hint:
        detail = f"{detail}. {validation.hint}"
    body: dict[str, object] = {
        "detail": detail,
        "reason": validation.reason or REASON_MALFORMED,
    }
    if validation.hint:
        body["hint"] = validation.hint
    return body


def _license_status(data: LicenseData, *, has_license: bool = True) -> LicenseStatusResponse:
    """Build a ``LicenseStatusResponse`` from a signed-license payload."""
    days_until_expiry, licence_state = derive_licence_state(data.expires_at or None)
    return LicenseStatusResponse(
        has_license=has_license,
        tier=data.tier,
        features=data.features,
        expires_at=data.expires_at or None,
        org_id=data.org_id or None,
        days_until_expiry=days_until_expiry,
        grace_ends_at=licence_grace_ends_at(data.expires_at or None),
        licence_state=licence_state,
        **_renewal_claims(data),
    )


def _lapsed_licence_present(settings: Settings, org: Organisation | None) -> bool:
    """Whether a configured licence key exists but has outlived its capability.

    Past ``expires_at + LICENCE_GRACE_DAYS`` every rung of the licence ladder
    falls through (correctly — the instance must run on Community), which makes
    the admin view indistinguishable from "never licensed". The operator still
    has to be told that Team capability turned OFF, so this looks for the
    remaining evidence: a configured key that VERIFIES but fails with
    :data:`REASON_EXPIRED`. That is precisely the lapsed state; any other
    failure (malformed, anchor mismatch) is not a lapsed licence.

    The verified-but-expired classification is reused from
    :func:`parse_and_verify` — no payload or signature logic is duplicated. A
    system licence that lapsed in memory has already been cleared by
    ``get_license`` and cannot be recovered here; the org and env keys are the
    durable statements of what was licensed.
    """
    candidate_keys: list[str] = []
    org_settings = getattr(org, "settings_json", None)
    if isinstance(org_settings, dict) and org_settings.get("license_key"):
        candidate_keys.append(str(org_settings["license_key"]))
    configured_key = getattr(settings, "modulo_license_key", "") or ""
    if configured_key:
        candidate_keys.append(configured_key)
    return any(parse_and_verify(key).reason == REASON_EXPIRED for key in candidate_keys)


def _resolve_effective_license(settings: Settings, org: Organisation | None = None) -> LicenseStatusResponse:
    """Resolve the effective license, checking org-level, then in-memory, then system-level (env var).

    Delegates the rung WALK to the shared session-free
    ``core.feature_flags.resolve_effective_license`` — the same walk
    ``core.feature_flags._resolve_paid_plan`` (the plan context) and
    ``GET /api/v1/plan`` use — so this admin view cannot drift from the tier
    the app actually runs on for the same deployment state.
    """
    data = resolve_effective_license(settings, org)
    if data is None:
        # No rung grants capability. Distinguish "never licensed" from "the
        # licence lapsed": the admin needs the second one explained, and the
        # only difference is whether a verified-but-expired key still exists.
        if _lapsed_licence_present(settings, org):
            return LicenseStatusResponse(
                has_license=False,
                tier="community",
                licence_state=LICENCE_STATE_LAPSED,
            )
        return LicenseStatusResponse(has_license=False, tier="community")
    return _license_status(data)


async def _read_license_cache(settings: Settings, org_id: str) -> LicenseStatusResponse | None:
    """Best-effort Redis cache read for an org's license status."""
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        cached = await redis.get(f"{_LICENSE_CACHE_KEY_PREFIX}{org_id}")
        if cached:
            return LicenseStatusResponse(**json.loads(cached))
    except Exception:
        logger.warning("license.cache_read_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()
    return None


async def _write_license_cache(settings: Settings, org_id: str, response: LicenseStatusResponse) -> None:
    """Best-effort Redis cache write (60s TTL) for an org's license status."""
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        await redis.set(
            f"{_LICENSE_CACHE_KEY_PREFIX}{org_id}", response.model_dump_json(), ex=_LICENSE_CACHE_TTL_SECONDS
        )
    except Exception:
        logger.warning("license.cache_write_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()


async def invalidate_license_status_cache(settings: Settings, org_id: str | None = None) -> None:
    """Best-effort invalidation of the licence-status Redis cache (FAR-1719).

    With ``org_id``, delete that org's cached entry — an org-level licence
    rotate/clear (``PUT/DELETE /api/v1/admin/orgs/{org_id}/license``) changes
    only that org's resolution. With ``org_id=None``, delete every
    ``license:*`` entry — a system-licence change (``POST /api/v1/admin/license``
    upload) can change the effective resolution of EVERY org that falls back
    to the system licence, so every cached entry is potentially stale.

    Failures are swallowed and logged: the mutation that triggered
    invalidation has already committed, and the cache expires on its own
    (60s TTL), so invalidation is best-effort and must never turn the
    committed request into a 500 (commit-then-error).
    """
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        if org_id is not None:
            await redis.delete(f"{_LICENSE_CACHE_KEY_PREFIX}{org_id}")
            return
        batch: list[str] = []
        async for key in redis.scan_iter(match=f"{_LICENSE_CACHE_KEY_PREFIX}*", count=100):
            batch.append(key)
            if len(batch) >= 500:
                await redis.delete(*batch)
                batch.clear()
        if batch:
            await redis.delete(*batch)
    except Exception:
        logger.warning("license.cache_invalidate_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()


@router.get("")
@handle_db_errors("admin.license.get_license_status")
async def get_license_status(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: TenantPrincipal = require_permission(_CODE_ORG_LICENSE_MANAGE),
) -> LicenseStatusResponse:

    org_id = current_user.organisation_id
    if org_id is not None:
        cached = await _read_license_cache(settings, str(org_id))
        if cached is not None:
            return cached

    try:
        org = None
        if org_id is not None:
            async with session.begin():
                org = await get_organisation(session, org_id)

        response = _resolve_effective_license(settings, org=org)
        await _write_license_cache(settings, str(org_id), response)
        return response
    except ProgrammingError:
        logger.exception(_CODE_LICENSE_GET_FAILED)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="License information is not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "admin_license.get_license_status")
        logger.exception(_CODE_LICENSE_GET_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching license status.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_LICENSE_GET_FAILED)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve license status.",
        ) from None


@router.post(
    "",
    dependencies=[Depends(audited("license_uploaded", "license", principal_dep=get_current_tenant_user))],
    status_code=status.HTTP_200_OK,
    # The route returns either the success model or a structured 422 JSONResponse;
    # pin the documented model explicitly so the union annotation is not introspected.
    response_model=LicenseUploadResponse,
)
@handle_db_errors("admin.license.upload_license")
async def upload_license(
    req: LicenseUploadRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    _: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> LicenseUploadResponse | JSONResponse:
    try:
        validation = parse_and_verify(req.license_key)
    except LicenseError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(exc), "reason": REASON_MALFORMED},
        )

    if not validation.valid or validation.license_data is None:
        # FAR-2091: a key that cannot verify must surface WHY (anchor mismatch,
        # signature mismatch, malformed, expired) on the install surface, not
        # only in a log line. The body keeps ``detail`` as prose (existing
        # consumers render it) and adds ``reason``/``hint``.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_license_failure_body(validation),
        )

    store_license(req.license_key, validation.license_data)
    # A system-licence change can flip the effective resolution of EVERY org
    # that falls back to the system licence, so bust every cached entry. The
    # upload above already committed to in-memory state; a cache failure must
    # not turn it into a 500 (best-effort inside the helper, FAR-1719).
    await invalidate_license_status_cache(settings)

    data = validation.license_data

    return LicenseUploadResponse(
        status="ok",
        tier=data.tier,
        features=data.features,
        expires_at=data.expires_at or None,
        org_id=data.org_id or None,
    )


@router.post(
    "/issue",
    dependencies=[Depends(audited("license_issued", "license", principal_dep=get_current_tenant_user))],
    status_code=status.HTTP_201_CREATED,
    # The route returns either the issued-key model or a structured error JSONResponse;
    # pin the documented model explicitly so the union annotation is not introspected.
    response_model=LicenseIssueResponse,
)
@handle_db_errors("admin.license.issue_license")
async def issue_license(
    req: LicenseIssueRequest,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    _: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> LicenseIssueResponse | JSONResponse:
    """Manually issue (sign) a team license key for a customer.

    Uses the same signing service as the Stripe purchase fulfilment webhook.
    When ``email`` is provided, the license key is also emailed to the customer
    via a background task so this request stays fast.
    """
    license_key = generate_team_license(
        req.org_name,
        term_months=req.term_months,
        features=req.features if req.features is not None else TEAM_FEATURES,
        private_key_hex=settings.modulo_license_private_key or None,
    )

    validation = parse_and_verify(license_key)
    if not validation.valid or validation.license_data is None:
        logger.error(
            "license.issue_verification_failed error=%s reason=%s",
            validation.error,
            validation.reason,
        )
        # The deployment just signed a key and then failed to verify it: the
        # signing key (MODULO_LICENSE_PRIVATE_KEY) and the trust anchor
        # (MODULO_LICENSE_PUBLIC_KEY) are not the same keypair. Name that.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": (
                    "Generated license key failed signature verification against this deployment's "
                    "trust anchor. Check that MODULO_LICENSE_PRIVATE_KEY and MODULO_LICENSE_PUBLIC_KEY "
                    "belong to the same Ed25519 keypair (anchor mismatch)."
                ),
                "reason": validation.reason or REASON_SIGNATURE_MISMATCH,
                "hint": (
                    "The signing key and the anchor must be the two halves of one keypair. "
                    "Set MODULO_LICENSE_PUBLIC_KEY to the public key derived from "
                    "MODULO_LICENSE_PRIVATE_KEY."
                ),
            },
        )

    data = validation.license_data

    if req.email:
        background_tasks.add_task(
            email_team_license,
            settings,
            req.email,
            license_key,
            data.expires_at,
        )

    return LicenseIssueResponse(
        license_key=license_key,
        expires_at=data.expires_at,
        org_name=req.org_name,
        tier=data.tier,
        features=data.features,
        org_id=data.org_id,
    )

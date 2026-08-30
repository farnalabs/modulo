"""Auth routes: login, refresh, logout, me (v1 account management)."""

import asyncio
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError as JWTError
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.api.middleware.rate_limiter import get_auth_rate_limiter
from modulo.api.routes.remy import clear_session_approvals_for_account
from modulo.auth.dependencies import (
    OrganisationMembershipNotFound,
    get_current_user,
    resolve_role_from_membership,
)
from modulo.auth.jwt import (
    AuthenticatedPrincipal,
    TenantPrincipal,
    create_access_token,
    create_refresh_token,
    decode_refresh_token_claims,
)
from modulo.auth.passwords import authenticate_db_user
from modulo.auth.ws_token import create_ws_token
from modulo.core.rate_limiter import AuthRateLimiter
from modulo.db.crud.account import get_account_by_email, get_account_by_id, update_last_login
from modulo.db.crud.break_glass_deny import is_break_glass_denied
from modulo.db.crud.org_membership import list_memberships_for_account
from modulo.db.crud.token_family import (
    advance_sequence,
    blacklist_family,
    consume_break_glass_credential,
    create_family,
)
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.token_family import TokenFamily
from modulo.settings import Settings, get_settings

_MSG_INCORRECT_EMAIL_PASSWORD = "Incorrect email or password"  # nosec B105 — error message, not a real credential
_CODE_AUTH_REFRESH = "auth.refresh"
_CODE_AUTH_LOGOUT = "auth.logout"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    requires_bootstrap: bool = False
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LogoutResponse(BaseModel):
    detail: str = "Logged out"


class WsTokenResponse(BaseModel):
    ws_token: str
    token_type: str = "ws-opaque"
    expires_in_seconds: int = 60


class MeResponse(BaseModel):
    id: str
    email: str
    display_name: str
    org_role: str
    active: bool
    created_at: str
    is_system_admin: bool = False
    must_change_password: bool = False


class _LoginContext(NamedTuple):
    account: Account
    org_id: uuid.UUID | None
    org_role: str | None
    memberships: list[OrgMembership]
    family: TokenFamily


class _RefreshClaims(NamedTuple):
    family_id: str
    family_uuid: uuid.UUID
    account_uuid: uuid.UUID
    sequence: int
    sub: str
    org_id: str
    org_role: object | None
    account_id: str


def get_clock() -> datetime:
    """Application clock for the break-glass early-deny decision.

    The DB clock (``current_timestamp``) stays authoritative for the SQL
    predicates; this injected clock is used ONLY for the expired-branch
    decision so tests can accelerate/retard the early deny deterministically.
    Skew between the two is bounded: an early false-deny is a conservative
    DoS, and an early false-accept is re-denied downstream by the DB-clock
    SQL predicate — the direction is DoS-not-bypass.
    """
    return datetime.now(UTC)


async def _enforce_break_glass(
    account: Account,
    *,
    now: datetime,
    limiter: AuthRateLimiter | None,
    ip: str,
) -> bool:
    """Break-glass login hook — early-deny/late-consume decision.

    Called after authentication succeeds. Fail-open fast path: a normal account
    returns ``False`` immediately with zero extra DB queries (the login fast
    path is unchanged). For a break-glass account it returns ``True`` when the
    credential is live — the caller must then run the late compare-and-swap
    consumption as the FINAL DB statement before token issuance. Deny-eligible
    credentials (deactivated / NULL-expiry / expired) and hook errors are
    fail-CLOSED: ``limiter.record_failure`` + a byte-identical 401
    (detail ``Incorrect email or password``).
    """
    if account.is_break_glass is not True:
        return False
    try:
        if is_break_glass_denied(
            is_break_glass=True,
            break_glass_expires_at=account.break_glass_expires_at,
            break_glass_deactivated_at=account.break_glass_deactivated_at,
            active=account.active,
            now=now,
        ):
            if limiter is not None:
                await limiter.record_failure(ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_MSG_INCORRECT_EMAIL_PASSWORD,
            )
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception:
        _log.exception("auth.break_glass_hook_error")
        if limiter is not None:
            await limiter.record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_MSG_INCORRECT_EMAIL_PASSWORD,
        ) from None
    return True


async def _consume_break_glass_credential(
    session: AsyncSession,
    *,
    account: Account,
    limiter: AuthRateLimiter | None,
    ip: str,
) -> None:
    """Late compare-and-swap consumption (family-first, CAS-last).

    Runs as the FINAL DB statement inside the login transaction, after the
    family mint. Fail-closed on ambiguity: a CAS error or a rowcount of 0
    (already consumed / expired / deactivated / inactive) raises a byte-identical
    401 and aborts the transaction, rolling back the phantom family.
    """
    try:
        consumed = await consume_break_glass_credential(
            session,
            account_id=account.id,
            current_password_hash=account.password_hash or "",
            new_password_hash=str(uuid.uuid4()),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("auth.break_glass_cas_error")
        if limiter is not None:
            await limiter.record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_MSG_INCORRECT_EMAIL_PASSWORD,
        ) from None
    if consumed != 1:
        if limiter is not None:
            await limiter.record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_MSG_INCORRECT_EMAIL_PASSWORD,
        )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def _authenticate_credentials(
    session: AsyncSession,
    email: str,
    password: str,
    *,
    limiter: AuthRateLimiter | None,
    ip: str,
) -> Account:
    """Resolve and verify an account, recording a rate-limit failure on denial."""
    account = await get_account_by_email(session, email)
    if not account or not authenticate_db_user(password, account):
        if limiter is not None:
            await limiter.record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_MSG_INCORRECT_EMAIL_PASSWORD,
        )
    return account


def _resolve_login_org_context(
    memberships: list[OrgMembership], account: Account
) -> tuple[uuid.UUID | None, str | None]:
    """Pick the primary org + role for a login, denying accounts with none."""
    if not memberships and not account.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account has no org memberships",
        )
    if memberships:
        membership = memberships[0]
        return membership.organisation_id, membership.role
    return None, None


async def _run_login_transaction(
    session: AsyncSession,
    email: str,
    password: str,
    *,
    limiter: AuthRateLimiter | None,
    ip: str,
) -> _LoginContext:
    """Authenticate, record login, resolve org context, and mint a family."""
    async with session.begin():
        account = await _authenticate_credentials(session, email, password, limiter=limiter, ip=ip)

        must_consume = await _enforce_break_glass(
            account,
            now=get_clock(),
            limiter=limiter,
            ip=ip,
        )

        if limiter is not None:
            await limiter.record_success(ip)
        await update_last_login(session, account.id)

        memberships = await list_memberships_for_account(session, account.id)
        org_id, org_role = _resolve_login_org_context(memberships, account)

        family = await create_family(session, account.id, org_id)

        if must_consume:
            await _consume_break_glass_credential(
                session,
                account=account,
                limiter=limiter,
                ip=ip,
            )

        return _LoginContext(
            account=account,
            org_id=org_id,
            org_role=org_role,
            memberships=memberships,
            family=family,
        )


def _mint_login_response(ctx: _LoginContext, settings: Settings) -> JSONResponse:
    """Build the access+refresh token pair and auth cookies for a login."""
    access_token = create_access_token(
        ctx.account.email,
        settings.secret_key,
        organisation_id=str(ctx.org_id) if ctx.org_id else "",
        account_id=str(ctx.account.id),
        org_role=ctx.org_role or "",
        is_system_admin=ctx.account.is_system_admin,
        ttl_minutes=settings.modulo_access_token_minutes,
    )
    refresh_token = create_refresh_token(
        ctx.account.email,
        settings.secret_key,
        organisation_id=str(ctx.org_id) if ctx.org_id else "",
        account_id=str(ctx.account.id),
        org_role=ctx.org_role or "",
        is_system_admin=ctx.account.is_system_admin,
        token_family=str(ctx.family.family_id),
        token_sequence=0,
    )
    requires_bootstrap = not ctx.memberships and ctx.account.is_system_admin
    content = LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        requires_bootstrap=requires_bootstrap,
        must_change_password=bool(ctx.account.must_change_password),
    ).model_dump()
    response = JSONResponse(content=content)
    _set_auth_cookies(response, access_token, settings)
    return response


@router.post("/login")
@handle_db_errors("auth.login")
async def login(
    req: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    ip = _client_ip(request)
    limiter = get_auth_rate_limiter(settings)

    try:
        ctx = await _run_login_transaction(session, req.email, req.password, limiter=limiter, ip=ip)
    except IntegrityError:
        _log.exception("auth.login")
        _log.warning("login.integrity_error")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account already has an active session. Try again.",
        ) from None
    except ProgrammingError:
        _log.warning("login.programming_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("auth.login")
        _log.warning("login.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is temporarily unavailable. Please try again.",
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception:
        _log.exception("Unexpected error in login")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return _mint_login_response(ctx, settings)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _parse_refresh_token(req: RefreshRequest, settings: Settings) -> _RefreshClaims:
    """Decode and structurally validate a refresh token into typed claims."""
    try:
        claims = decode_refresh_token_claims(req.refresh_token, settings.secret_key)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from exc

    family_id_val = claims.get("token_family")
    sequence_val = claims.get("token_sequence")
    if not isinstance(family_id_val, str) or not isinstance(sequence_val, int):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token claims",
        )
    try:
        family_uuid = uuid.UUID(family_id_val)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token family",
        ) from exc

    account_id_claim = claims.get("account_id") or claims.get("user_id")
    if not isinstance(account_id_claim, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token claims",
        )
    try:
        account_uuid = uuid.UUID(account_id_claim)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token account",
        ) from exc

    sub_val = claims.get("sub")
    org_id_val = claims.get("org_id")
    org_role_val = claims.get("org_role")
    if not isinstance(sub_val, str) or not isinstance(org_id_val, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token payload",
        )

    return _RefreshClaims(
        family_id=family_id_val,
        family_uuid=family_uuid,
        account_uuid=account_uuid,
        sequence=sequence_val,
        sub=sub_val,
        org_id=org_id_val,
        org_role=org_role_val,
        account_id=account_id_claim,
    )


async def _advance_refresh_sequence(
    session: AsyncSession,
    claims: _RefreshClaims,
) -> tuple[str | None, int, bool]:
    """Re-check live account status + org role (ADR 017), then advance the family."""
    live_org_role: str | None = None
    new_sequence = 0
    theft_detected = False
    account_denied = False
    async with session.begin():
        # FAR-463 defense-in-depth: re-read ACCOUNT.ACTIVE on every refresh.
        # Deactivation must kill outstanding sessions exactly like membership
        # removal does. The caller-bound deactivate_break_glass blacklists only
        # families scoped to the deactivating admin's orgs, so families minted
        # under other orgs (or with NULL organisation_id) can outlive
        # deactivation - and removed-member semantics cannot catch them because
        # their memberships were left untouched. This check applies to ALL
        # principals, including system admins without memberships (they
        # authenticate by account too).
        #
        # Ordered BEFORE the membership read: equally decisive, cheaper (single
        # PK lookup), and ADR-017 membership-not-found semantics stay intact
        # for removed members whose accounts remain active.
        account = await get_account_by_id(session, claims.account_uuid)
        # getattr with a falsy default: a row shape that cannot report its
        # active flag (missing row handled via the None check; inert test
        # doubles or a partially-loaded row) is treated as INACTIVE and denied
        # - fail-closed, matching authenticate_db_user's not-account-active
        # semantics at login.
        if account is None or getattr(account, "active", False) is not True:
            _log.warning(
                "auth.refresh_account_inactive",
                extra={"account_id": claims.account_id},
            )
            # Inline correlated UPDATE (not blacklist_family): same predicate
            # (family_id AND account_id - never crosses accounts), same flags,
            # same transaction - expressed as a bulk statement so the denial
            # persists atomically. Account-bound like the logout blacklisting.
            await session.execute(
                update(TokenFamily)
                .where(
                    TokenFamily.family_id == claims.family_uuid,
                    TokenFamily.account_id == claims.account_uuid,
                )
                .values(is_blacklisted=True, blacklisted_at=datetime.now(UTC))
            )
            # Flag-and-raise-after-commit: raising inside the transaction would
            # roll back the blacklist above; committing it first also means a
            # later reactivation cannot resurrect pre-deactivation tokens.
            account_denied = True
        else:
            # ADR 017: check live membership BEFORE advancing the token-family
            # sequence - a removed member's repeated refresh attempts must not
            # keep advancing sequences needlessly.
            if claims.org_id:
                live_org_role = await resolve_role_from_membership(
                    session,
                    claims.account_id,
                    claims.org_id,
                )
            if claims.org_id and live_org_role is None:
                _log.warning(
                    "auth.refresh_membership_not_found",
                    extra={"account_id": claims.account_id, "org_id": claims.org_id},
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Account no longer has access to this organisation",
                )
            new_sequence, theft_detected = await advance_sequence(
                session, claims.family_uuid, claims.sequence, claims.account_uuid
            )
    if account_denied:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account no longer has access to this organisation",
        )
    return live_org_role, new_sequence, theft_detected


def _mint_refresh_response(
    claims: _RefreshClaims,
    minted_org_role: object,
    new_sequence: int,
    settings: Settings,
) -> JSONResponse:
    """Build the rotated access+refresh token pair and auth cookies."""
    new_access = create_access_token(
        claims.sub,
        settings.secret_key,
        organisation_id=claims.org_id,
        account_id=claims.account_id,
        org_role=str(minted_org_role),
        ttl_minutes=settings.modulo_access_token_minutes,
    )
    new_refresh = create_refresh_token(
        claims.sub,
        settings.secret_key,
        organisation_id=claims.org_id,
        account_id=claims.account_id,
        org_role=str(minted_org_role),
        token_family=claims.family_id,
        token_sequence=new_sequence,
    )
    content = RefreshResponse(access_token=new_access, refresh_token=new_refresh).model_dump()
    response = JSONResponse(content=content)
    _set_auth_cookies(response, new_access, settings)
    return response


@router.post("/refresh")
@handle_db_errors(_CODE_AUTH_REFRESH)
async def refresh(
    req: RefreshRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    claims = _parse_refresh_token(req, settings)

    try:
        live_org_role, new_sequence, theft_detected = await _advance_refresh_sequence(session, claims)
    except IntegrityError:
        _log.exception(_CODE_AUTH_REFRESH)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_AUTH_REFRESH)
        _log.warning("refresh.programming_error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_AUTH_REFRESH)
        _log.warning("refresh.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Token refresh is temporarily unavailable. Please try again.",
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception:  # nosemgrep: bare-raise-in-except
        _log.exception("Unexpected error in refresh")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if theft_detected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked due to suspected theft",
        )
    minted_org_role = live_org_role if live_org_role is not None else claims.org_role
    return _mint_refresh_response(claims, minted_org_role, new_sequence, settings)


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


async def _blacklist_refresh_family(session: AsyncSession, claims: dict[str, object]) -> None:
    """Blacklist the refresh token's family if the claims carry one."""
    family_id_val = claims.get("token_family")
    account_id_val = claims.get("account_id") or claims.get("user_id")
    if not isinstance(family_id_val, str) or not isinstance(account_id_val, str):
        return
    try:
        family_uuid = uuid.UUID(family_id_val)
        account_uuid = uuid.UUID(account_id_val)
        try:
            async with session.begin():
                blacklisted = await blacklist_family(session, family_uuid, account_uuid)
                if not blacklisted:
                    _log.warning("logout.family_not_found", extra={"family_id": family_id_val})
        except IntegrityError:
            _log.exception(_CODE_AUTH_LOGOUT)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A resource with this value already exists",
            ) from None
        except ProgrammingError:
            _log.exception(_CODE_AUTH_LOGOUT)
            _log.warning("logout.programming_error")
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=MSG_FEATURE_NOT_AVAILABLE,
            ) from None
        except SQLAlchemyError:
            _log.exception(_CODE_AUTH_LOGOUT)
            _log.warning("logout.sqlalchemy_error")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Logout is temporarily unavailable. Please try again.",
            ) from None
        except asyncio.CancelledError:
            raise
        except HTTPException as exc:
            raise exc
        except Exception:
            _log.exception("Unexpected error in logout (inner)")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=MSG_INTERNAL_SERVER_ERROR,
            ) from None
    except ValueError:
        _log.warning("logout.invalid_token_family", extra={"token_family": family_id_val})


def _clear_account_session_approvals(claims: dict[str, object]) -> None:
    """Scope the approval clear to the caller's account only (FAR-1470)."""
    account_id_val = claims.get("account_id") or claims.get("user_id")
    if isinstance(account_id_val, str):
        clear_session_approvals_for_account(account_id_val)


@router.post("/logout")
@handle_db_errors(_CODE_AUTH_LOGOUT)
async def logout(
    req: RefreshRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        claims = decode_refresh_token_claims(req.refresh_token, settings.secret_key)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from exc

    await _blacklist_refresh_family(session, claims)
    _clear_account_session_approvals(claims)

    content = LogoutResponse(detail="Logged out").model_dump()
    response = JSONResponse(content=content)
    _clear_auth_cookies(response, settings)
    return response


# ---------------------------------------------------------------------------
# WS token / me (ADR 017 live-role resolution)
# ---------------------------------------------------------------------------


async def _resolve_live_org_role(
    session: AsyncSession,
    *,
    account_id: str,
    org_id: str | None,
    username: str,
) -> str | None:
    """ADR 017: re-read the LIVE org role; deny removed/deactivated members."""
    if org_id is None:
        return None
    try:
        async with session.begin():
            live_org_role = await resolve_role_from_membership(session, account_id, org_id)
    except SQLAlchemyError:
        _log.warning("permission.live_role_read_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Role verification temporarily unavailable. Please try again.",
        ) from None
    if live_org_role is None:
        # ADR 017: missing/deactivated membership → deny. A removed user must
        # not mint a WS token or keep the claimed role.
        _log.warning(
            "permission.membership_not_found",
            extra={"account_id": account_id, "org_id": org_id, "username": username},
        )
        raise OrganisationMembershipNotFound
    return live_org_role


@router.post("/ws-token")
@handle_db_errors("auth.ws_token")
async def ws_token(
    current_user: TenantPrincipal = require_permission("run.status"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> WsTokenResponse:
    try:
        # ADR 017: embed the LIVE org role (not the claim) so a demoted admin's
        # WS token carries the reduced role.
        live_org_role = await _resolve_live_org_role(
            session,
            account_id=str(current_user.account_id),
            org_id=str(current_user.organisation_id),
            username=current_user.username,
        )

        principal_json = {
            "sub": current_user.username,
            "org_id": str(current_user.organisation_id) if current_user.organisation_id else "",
            "account_id": str(current_user.account_id),
            "org_role": live_org_role or "",
        }

        from redis.asyncio import Redis

        redis = Redis.from_url(settings.redis_url, decode_responses=False)
        try:
            token = await create_ws_token(
                redis,
                principal_json,
                ttl=settings.modulo_ws_token_ttl_seconds,
            )
            return WsTokenResponse(
                ws_token=token,
                token_type="ws-opaque",  # noqa: S106  # nosec B106 — opaque-token type label, not a credential
                expires_in_seconds=settings.modulo_ws_token_ttl_seconds,
            )
        finally:
            await redis.aclose()
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception:
        _log.exception("Unexpected error in ws_token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.get("/me")
@handle_db_errors("auth.me")
async def me(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    try:
        async with session.begin():
            account = await get_account_by_id(session, current_user.account_id)
    except ProgrammingError:
        _log.exception("auth.me")
        _log.warning("me.programming_error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("auth.me")
        _log.warning("me.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account service is temporarily unavailable. Please try again.",
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception:
        _log.exception("Unexpected error in me")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    # ADR 017: return the LIVE org role (not the claim) so the frontend stops
    # rendering admin controls for demoted/removed users.
    live_org_role = await _resolve_live_org_role(
        session,
        account_id=str(current_user.account_id),
        org_id=str(current_user.organisation_id) if current_user.organisation_id is not None else None,
        username=current_user.username,
    )

    return MeResponse(
        id=str(account.id),
        email=account.email,
        display_name=account.display_name,
        org_role=live_org_role or "",
        active=account.active,
        created_at=account.created_at.isoformat(),
        is_system_admin=current_user.is_system_admin,
        must_change_password=bool(account.must_change_password),
    )


class CsrfTokenResponse(BaseModel):
    csrf_token: str


@router.get("/csrf-token", response_model=CsrfTokenResponse)
@handle_db_errors("auth.csrf_token")
async def csrf_token(
    _current_user: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    try:
        token = secrets.token_hex(32)
        content = CsrfTokenResponse(csrf_token=token).model_dump()
        response = JSONResponse(content=content)
        _set_csrf_cookie(response, token, settings)
        return response
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception:
        _log.exception("Unexpected error in csrf_token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


def _set_auth_cookies(response: Response, access_token: str, settings: Settings) -> None:
    secure = not settings.debug
    response.set_cookie(
        key="modulo_session",
        value=access_token,
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=settings.modulo_access_token_minutes * 60,
        path="/",
    )
    csrf_token_value = secrets.token_hex(32)
    _set_csrf_cookie(response, csrf_token_value, settings)


def _set_csrf_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key="XSRF-TOKEN",
        value=token,
        httponly=False,  # NOSONAR S3330 — JS-readable CSRF token; SameSite=strict + secure mitigate.
        samesite="strict",
        secure=not settings.debug,
        max_age=settings.modulo_access_token_minutes * 60,
        path="/",
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    secure = not settings.debug
    response.set_cookie(
        key="modulo_session",
        value="",
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=0,
        path="/",
    )
    response.set_cookie(
        key="XSRF-TOKEN",
        value="",
        httponly=False,  # NOSONAR S3330 — JS-readable CSRF token; SameSite=strict + secure mitigate.
        samesite="strict",
        secure=secure,
        max_age=0,
        path="/",
    )


def _client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return "unknown"

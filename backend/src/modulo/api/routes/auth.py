"""Auth routes: login, refresh, logout, me (v1 account management)."""

import asyncio
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError as JWTError
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.api.middleware.rate_limiter import get_auth_rate_limiter
from modulo.api.routes.remy import clear_all_session_approvals
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import (
    AuthenticatedPrincipal,
    create_access_token,
    create_refresh_token,
    decode_refresh_token_claims,
)
from modulo.auth.jwt import (
    create_ws_token as create_jwt_ws_token,
)
from modulo.auth.passwords import authenticate_db_user
from modulo.auth.ws_token import create_ws_token as create_opaque_ws_token
from modulo.db.crud.account import get_account_by_email, get_account_by_id, update_last_login
from modulo.db.crud.org_membership import list_memberships_for_account
from modulo.db.crud.token_family import advance_sequence, blacklist_family, create_family
from modulo.settings import Settings, get_settings

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


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LogoutResponse(BaseModel):
    detail: str = "Logged out"


class WsTokenRequest(BaseModel):
    pass


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


@handle_db_errors("auth.login")
@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    ip = _client_ip(request)
    limiter = get_auth_rate_limiter(settings)

    try:
        async with session.begin():
            account = await get_account_by_email(session, req.email)
            if not account or not authenticate_db_user(req.password, account):
                if limiter is not None:
                    await limiter.record_failure(ip)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incorrect email or password",
                )

            if limiter is not None:
                await limiter.record_success(ip)
            await update_last_login(session, account.id)

            memberships = await list_memberships_for_account(session, account.id)
            if not memberships and not account.is_system_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account has no org memberships",
                )

            if memberships:
                membership = memberships[0]
                org_id = membership.organisation_id
                org_role = membership.role
            else:
                org_id = None
                org_role = None

            family = await create_family(session, account.id, org_id)
    except IntegrityError:
        _log.warning("login.integrity_error")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account already has an active session. Try again.",
        ) from None
    except ProgrammingError:
        _log.warning("login.programming_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.warning("login.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is temporarily unavailable. Please try again.",
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in login")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    access_token = create_access_token(
        account.email,
        settings.secret_key,
        organisation_id=str(org_id) if org_id else "",
        account_id=str(account.id),
        org_role=org_role or "",
        is_system_admin=account.is_system_admin,
    )
    refresh_token = create_refresh_token(
        account.email,
        settings.secret_key,
        organisation_id=str(org_id) if org_id else "",
        account_id=str(account.id),
        org_role=org_role or "",
        is_system_admin=account.is_system_admin,
        token_family=str(family.family_id),
        token_sequence=0,
    )
    requires_bootstrap = not memberships and account.is_system_admin
    content = LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        requires_bootstrap=requires_bootstrap,
    ).model_dump()
    response = JSONResponse(content=content)
    _set_auth_cookies(response, access_token, settings)
    return response


@handle_db_errors("auth.refresh")
@router.post("/refresh")
async def refresh(
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

    family_id_val = claims.get("token_family")
    sequence_val = claims.get("token_sequence")
    if not isinstance(family_id_val, str) or not isinstance(sequence_val, int):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token claims",
        )
    family_id_str: str = family_id_val
    sequence: int = sequence_val

    try:
        family_uuid = uuid.UUID(family_id_str)
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

    try:
        async with session.begin():
            new_sequence, theft_detected = await advance_sequence(session, family_uuid, sequence, account_uuid)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        _log.warning("refresh.programming_error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.warning("refresh.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Token refresh is temporarily unavailable. Please try again.",
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in refresh")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    if theft_detected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked due to suspected theft",
        )

    sub_val = claims.get("sub")
    org_id_val = claims.get("org_id")
    account_id_val = account_id_claim
    org_role_val = claims.get("org_role")
    if any(not isinstance(value, str) for value in (sub_val, org_id_val, account_id_val)) or (
        org_id_val is not None and not isinstance(org_id_val, str)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token payload",
        )

    new_access = create_access_token(
        str(sub_val),
        settings.secret_key,
        organisation_id=str(org_id_val),
        account_id=str(account_id_val),
        org_role=str(org_role_val),
    )
    new_refresh = create_refresh_token(
        str(sub_val),
        settings.secret_key,
        organisation_id=str(org_id_val),
        account_id=str(account_id_val),
        org_role=str(org_role_val),
        token_family=family_id_str,
        token_sequence=new_sequence,
    )
    content = RefreshResponse(access_token=new_access, refresh_token=new_refresh).model_dump()
    response = JSONResponse(content=content)
    _set_auth_cookies(response, new_access, settings)
    return response


@handle_db_errors("auth.logout")
@router.post("/logout")
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

    family_id_val = claims.get("token_family")
    account_id_val = claims.get("account_id") or claims.get("user_id")
    if isinstance(family_id_val, str) and isinstance(account_id_val, str):
        try:
            family_uuid = uuid.UUID(family_id_val)
            account_uuid = uuid.UUID(account_id_val)
            try:
                async with session.begin():
                    blacklisted = await blacklist_family(session, family_uuid, account_uuid)
                    if not blacklisted:
                        _log.warning("logout.family_not_found", extra={"family_id": family_id_val})
            except IntegrityError:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A resource with this value already exists",
                ) from None
            except ProgrammingError:
                _log.warning("logout.programming_error")
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="Feature is not available. Run database migrations to enable it.",
                ) from None
            except SQLAlchemyError:
                _log.warning("logout.sqlalchemy_error")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Logout is temporarily unavailable. Please try again.",
                ) from None
            except asyncio.CancelledError:
                raise
            except HTTPException:
                raise
            except Exception:
                _log.exception("Unexpected error in logout (inner)")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Internal server error",
                ) from None
        except ValueError:
            _log.warning("logout.invalid_token_family", extra={"token_family": family_id_val})

    clear_all_session_approvals()

    content = LogoutResponse(detail="Logged out").model_dump()
    response = JSONResponse(content=content)
    _clear_auth_cookies(response, settings)
    return response


@handle_db_errors("auth.ws_token")
@router.post("/ws-token", response_model=WsTokenResponse)
async def ws_token(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WsTokenResponse:
    try:
        principal_json = {
            "sub": current_user.username,
            "org_id": str(current_user.organisation_id) if current_user.organisation_id else "",
            "account_id": str(current_user.account_id),
            "org_role": current_user.org_role or "",
        }

        if settings.redis_url:
            redis = None
            try:
                from redis.asyncio import Redis

                redis = Redis.from_url(settings.redis_url, decode_responses=False)
                token = await create_opaque_ws_token(
                    redis,
                    principal_json,
                    ttl=settings.modulo_ws_token_ttl_seconds,
                )
                return WsTokenResponse(
                    ws_token=token,
                    token_type="ws-opaque",  # noqa: S106
                    expires_in_seconds=settings.modulo_ws_token_ttl_seconds,
                )
            except HTTPException:
                raise
            except Exception as exc:
                _log.warning("ws_token.redis_fallback", extra={"error": str(exc)})
            finally:
                if redis is not None:
                    await redis.aclose()

        token = create_jwt_ws_token(
            current_user.username,
            settings.secret_key,
            organisation_id=str(current_user.organisation_id) if current_user.organisation_id else "",
            account_id=str(current_user.account_id),
            org_role=current_user.org_role or "",
            ttl_minutes=max(1, settings.modulo_ws_token_ttl_seconds // 60),
        )
        return WsTokenResponse(
            ws_token=token,
            token_type="ws-jwt",  # noqa: S106
            expires_in_seconds=settings.modulo_ws_token_ttl_seconds,
        )
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in ws_token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


@handle_db_errors("auth.me")
@router.get("/me", response_model=MeResponse)
async def me(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    try:
        async with session.begin():
            account = await get_account_by_id(session, current_user.account_id)
    except ProgrammingError:
        _log.warning("me.programming_error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.warning("me.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account service is temporarily unavailable. Please try again.",
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in me")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return MeResponse(
        id=str(account.id),
        email=account.email,
        display_name=account.display_name,
        org_role=current_user.org_role or "",
        active=account.active,
        created_at=account.created_at.isoformat(),
        is_system_admin=current_user.is_system_admin,
    )


class CsrfTokenResponse(BaseModel):
    csrf_token: str


@handle_db_errors("auth.csrf_token")
@router.get("/csrf-token", response_model=CsrfTokenResponse)
async def csrf_token(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
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
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in csrf_token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


def _set_auth_cookies(response: Response, access_token: str, settings: Settings) -> None:
    secure = not settings.debug
    response.set_cookie(
        key="modulo_session",
        value=access_token,
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=900,
        path="/",
    )
    csrf_token_value = secrets.token_hex(32)
    _set_csrf_cookie(response, csrf_token_value, settings)


def _set_csrf_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key="XSRF-TOKEN",
        value=token,
        httponly=False,
        samesite="strict",
        secure=not settings.debug,
        max_age=900,
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
        httponly=False,
        samesite="strict",
        secure=secure,
        max_age=0,
        path="/",
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"

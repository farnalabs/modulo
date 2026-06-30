"""Auth routes: login, refresh, logout, me (v1 user management)."""

import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from jose import JWTError
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.middleware.rate_limiter import get_auth_rate_limiter
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
from modulo.db.crud.token_family import advance_sequence, blacklist_family, create_family
from modulo.db.crud.user import get_user_by_email, get_user_by_id, update_last_login
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


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    ip = _client_ip(request)
    limiter = get_auth_rate_limiter(settings)

    async with session.begin():
        user = await get_user_by_email(session, body.email)
        if not user or not authenticate_db_user(body.password, user):
            await limiter.record_failure(ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
            )

        await limiter.record_success(ip)
        await update_last_login(session, user.id)
        family = await create_family(session, user.id, user.organisation_id)

    access_token = create_access_token(
        user.email,
        settings.secret_key,
        organisation_id=str(user.organisation_id),
        user_id=str(user.id),
        org_role=user.org_role,
    )
    refresh_token = create_refresh_token(
        user.email,
        settings.secret_key,
        organisation_id=str(user.organisation_id),
        user_id=str(user.id),
        org_role=user.org_role,
        token_family=str(family.family_id),
        token_sequence=0,
    )
    content = LoginResponse(access_token=access_token, refresh_token=refresh_token).model_dump()
    response = JSONResponse(content=content)
    _set_auth_cookies(response, access_token, settings)
    return response


@router.post("/refresh")
async def refresh(
    body: RefreshRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        claims = decode_refresh_token_claims(body.refresh_token, settings.secret_key)
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

    new_sequence, theft_detected = await advance_sequence(session, family_uuid, sequence)
    if theft_detected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked due to suspected theft",
        )

    sub_val = claims.get("sub")
    org_id_val = claims.get("org_id")
    user_id_val = claims.get("user_id") or claims.get("account_id")
    org_role_val = claims.get("org_role")
    if not all(isinstance(v, str) for v in [sub_val, org_id_val, user_id_val, org_role_val]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token payload",
        )

    new_access = create_access_token(
        str(sub_val),
        settings.secret_key,
        organisation_id=str(org_id_val),
        user_id=str(user_id_val),
        org_role=str(org_role_val),
    )
    new_refresh = create_refresh_token(
        str(sub_val),
        settings.secret_key,
        organisation_id=str(org_id_val),
        user_id=str(user_id_val),
        org_role=str(org_role_val),
        token_family=family_id_str,
        token_sequence=new_sequence,
    )
    content = RefreshResponse(access_token=new_access, refresh_token=new_refresh).model_dump()
    response = JSONResponse(content=content)
    _set_auth_cookies(response, new_access, settings)
    return response


@router.post("/logout")
async def logout(
    body: RefreshRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        claims = decode_refresh_token_claims(body.refresh_token, settings.secret_key)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from exc

    family_id_val = claims.get("token_family")
    if isinstance(family_id_val, str):
        try:
            family_uuid = uuid.UUID(family_id_val)
            await blacklist_family(session, family_uuid)
        except ValueError:
            pass

    content = LogoutResponse(detail="Logged out").model_dump()
    response = JSONResponse(content=content)
    _clear_auth_cookies(response, settings)
    return response


@router.post("/ws-token", response_model=WsTokenResponse)
async def ws_token(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WsTokenResponse:
    principal_json = {
        "sub": current_user.username,
        "org_id": str(current_user.organisation_id),
        "user_id": str(current_user.user_id),
        "org_role": current_user.org_role,
    }

    if settings.redis_url:
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
        except Exception as exc:
            _log.warning("ws_token.redis_fallback", extra={"error": str(exc)})

    token = create_jwt_ws_token(
        current_user.username,
        settings.secret_key,
        organisation_id=str(current_user.organisation_id),
        user_id=str(current_user.user_id),
        org_role=current_user.org_role,
    )
    return WsTokenResponse(
        ws_token=token,
        token_type="ws-jwt",  # noqa: S106
        expires_in_seconds=settings.modulo_ws_token_ttl_seconds,
    )


@router.get("/me", response_model=MeResponse)
async def me(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    user = await get_user_by_id(session, current_user.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return MeResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        org_role=user.org_role,
        active=user.active,
        created_at=user.created_at.isoformat(),
    )


class CsrfTokenResponse(BaseModel):
    csrf_token: str


@router.get("/csrf-token", response_model=CsrfTokenResponse)
async def csrf_token(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    token = secrets.token_hex(32)
    content = CsrfTokenResponse(csrf_token=token).model_dump()
    response = JSONResponse(content=content)
    _set_csrf_cookie(response, token, settings)
    return response


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

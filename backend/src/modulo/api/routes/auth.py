"""Auth routes: login, refresh, logout, me (v1 user management)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import (
    AuthenticatedPrincipal,
    create_access_token,
    create_refresh_token,
    create_ws_token,
    decode_refresh_token_claims,
)
from modulo.auth.passwords import authenticate_db_user
from modulo.db.crud.token_family import advance_sequence, blacklist_family, create_family
from modulo.db.crud.user import get_user_by_email, get_user_by_id, update_last_login
from modulo.settings import Settings, get_settings

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
    token_type: str = "bearer"
    expires_in_minutes: int = 15


class MeResponse(BaseModel):
    id: str
    email: str
    display_name: str
    org_role: str
    active: bool
    created_at: str


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> LoginResponse:
    async with session.begin():
        user = await get_user_by_email(session, body.email)
        if not user or not authenticate_db_user(body.password, user):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
            )

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
    return LoginResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    body: RefreshRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> RefreshResponse:
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
    user_id_val = claims.get("user_id")
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
    return RefreshResponse(access_token=new_access, refresh_token=new_refresh)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    body: RefreshRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> LogoutResponse:
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

    return LogoutResponse(detail="Logged out")


@router.post("/ws-token", response_model=WsTokenResponse)
async def ws_token(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WsTokenResponse:
    token = create_ws_token(
        current_user.username,
        settings.secret_key,
        organisation_id=str(current_user.organisation_id),
        user_id=str(current_user.user_id),
        org_role=current_user.org_role,
    )
    return WsTokenResponse(ws_token=token)


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

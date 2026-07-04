"""Stage CRUD REST API."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.stage import (
    create_stage,
    delete_stage,
    get_stage,
    list_stages,
    update_stage,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

router = APIRouter(prefix="/api/v1/stages", tags=["stages"])


class StageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    position: int = 0
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org", pattern=r"^(org|team)$")


class StageUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    position: int | None = None
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = Field(None, pattern=r"^(org|team)$")


class StageResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    position: int
    owner_team_id: uuid.UUID | None
    visibility: str
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StageListResponse(BaseModel):
    items: list[StageResponse]
    total: int
    page: int
    page_size: int


@router.get("", response_model=StageListResponse)
async def list_stages_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    owner_team_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> StageListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_stages(session, page=page, page_size=page_size, owner_team_id=owner_team_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    return StageListResponse(
        items=[StageResponse.model_validate(s) for s in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=StageResponse, status_code=status.HTTP_201_CREATED)
async def create_stage_endpoint(
    req: StageCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> StageResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            stage = await create_stage(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                description=req.description,
                position=req.position,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    return StageResponse.model_validate(stage)


@router.get("/{stage_id}", response_model=StageResponse)
async def get_stage_endpoint(
    stage_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> StageResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            stage = await get_stage(session, stage_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    if stage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stage not found")
    return StageResponse.model_validate(stage)


@router.patch("/{stage_id}", response_model=StageResponse)
async def update_stage_endpoint(
    stage_id: uuid.UUID,
    req: StageUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> StageResponse:
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            stage = await update_stage(session, stage_id, updates)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    if stage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stage not found")
    return StageResponse.model_validate(stage)


@router.delete("/{stage_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stage_endpoint(
    stage_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_stage(session, stage_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stage not found")

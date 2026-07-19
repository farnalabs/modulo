"""Pipeline Folder CRUD REST API."""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.pipeline_folder import (
    create_folder,
    delete_folder,
    list_folders,
    update_folder,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/pipeline-folders", tags=["pipeline-folders"])


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None


class FolderUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None


class FolderMove(BaseModel):
    sort_order: int = Field(ge=0)


class FolderResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    sort_order: int
    account_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[FolderResponse])
@handle_db_errors("pipeline_folders.list")
async def list_folders_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[FolderResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            folders = await list_folders(session)
    except ProgrammingError:
        logger.exception("pipeline_folders.list")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None
    return [FolderResponse.model_validate(f) for f in folders]


@router.post("", response_model=FolderResponse, status_code=status.HTTP_201_CREATED)
@handle_db_errors("pipeline_folders.create")
async def create_folder_endpoint(
    req: FolderCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> FolderResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            folder = await create_folder(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                parent_id=req.parent_id,
            )
    except ProgrammingError:
        logger.exception("pipeline_folders.create")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None
    return FolderResponse.model_validate(folder)


@router.patch("/{folder_id}", response_model=FolderResponse)
@handle_db_errors("pipeline_folders.update")
async def update_folder_endpoint(
    folder_id: uuid.UUID,
    req: FolderUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> FolderResponse:
    updates = req.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            folder = await update_folder(session, folder_id, updates)
    except ProgrammingError:
        logger.exception("pipeline_folders.update(%s)", folder_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")
    return FolderResponse.model_validate(folder)


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("pipeline_folders.delete")
async def delete_folder_endpoint(
    folder_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_folder(session, folder_id)
    except ProgrammingError:
        logger.exception("pipeline_folders.delete(%s)", folder_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")


@router.patch("/{folder_id}/move", response_model=FolderResponse)
@handle_db_errors("pipeline_folders.move")
async def reorder_folder_endpoint(
    folder_id: uuid.UUID,
    req: FolderMove,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> FolderResponse:
    updates = {"sort_order": req.sort_order}
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            folder = await update_folder(session, folder_id, updates)
    except ProgrammingError:
        logger.exception("pipeline_folders.reorder(%s)", folder_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")
    return FolderResponse.model_validate(folder)

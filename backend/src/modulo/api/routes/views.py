"""Saved View CRUD REST API — persisted filters and display preferences."""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.view import (
    create_view,
    delete_view,
    get_view,
    list_views,
    update_view,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

router = APIRouter(prefix="/api/v1/views", tags=["views"])

_VALID_VIEW_TYPES = {"run_list", "pipeline_list", "audit_log"}
_VALID_SORT_ORDERS = {"asc", "desc"}


class ViewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    view_type: str = Field(..., pattern=r"^(run_list|pipeline_list|audit_log)$")
    filters: dict = Field(default_factory=dict)
    columns: list[str] | None = None
    sort_by: str | None = Field(None, max_length=100)
    sort_order: str = Field(default="desc", pattern=r"^(asc|desc)$")


class ViewUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    filters: dict | None = None
    columns: list[str] | None = None
    sort_by: str | None = Field(None, max_length=100)
    sort_order: str | None = Field(None, pattern=r"^(asc|desc)$")


class ViewResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    view_type: str
    filters: dict
    columns: list[str] | None
    sort_by: str | None
    sort_order: str
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ViewListResponse(BaseModel):
    items: list[ViewResponse]
    total: int
    page: int
    page_size: int


@router.get("", response_model=ViewListResponse)
async def list_views_endpoint(
    view_type: str | None = Query(None, pattern=r"^(run_list|pipeline_list|audit_log)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ViewListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_views(session, view_type=view_type, page=page, page_size=page_size)
    except ProgrammingError:
        logger.exception("views.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The saved_views feature is not available. Run database migrations to enable it.",
        )
    return ViewListResponse(
        items=[ViewResponse.model_validate(v) for v in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=ViewResponse, status_code=status.HTTP_201_CREATED)
async def create_view_endpoint(
    body: ViewCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ViewResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            view = await create_view(
                session,
                org_id=principal.organisation_id,
                name=body.name,
                view_type=body.view_type,
                account_id=principal.account_id,
                description=body.description,
                filters=body.filters,
                columns=body.columns,
                sort_by=body.sort_by,
                sort_order=body.sort_order,
            )
    except ProgrammingError:
        logger.exception("views.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The saved_views feature is not available. Run database migrations to enable it.",
        )
    return ViewResponse.model_validate(view)


@router.get("/{view_id}", response_model=ViewResponse)
async def get_view_endpoint(
    view_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ViewResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            view = await get_view(session, view_id)
    except ProgrammingError:
        logger.exception("views.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The saved_views feature is not available. Run database migrations to enable it.",
        )
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="View not found")
    return ViewResponse.model_validate(view)


@router.patch("/{view_id}", response_model=ViewResponse)
async def update_view_endpoint(
    view_id: uuid.UUID,
    body: ViewUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ViewResponse:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            view = await update_view(session, view_id, updates)
    except ProgrammingError:
        logger.exception("views.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The saved_views feature is not available. Run database migrations to enable it.",
        )
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="View not found")
    return ViewResponse.model_validate(view)


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_view_endpoint(
    view_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_view(session, view_id)
    except ProgrammingError:
        logger.exception("views.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The saved_views feature is not available. Run database migrations to enable it.",
        )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="View not found")

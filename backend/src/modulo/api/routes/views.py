"""Saved View CRUD REST API — persisted filters and display preferences."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_feature
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.view import (
    create_view,
    delete_view,
    get_view,
    list_views,
    update_view,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/views", tags=["views"])

_VALID_VIEW_TYPES = {"run_list", "pipeline_list", "audit_log"}
_VALID_SORT_ORDERS = {"asc", "desc"}


class ViewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    view_type: str = Field(..., pattern=r"^(run_list|pipeline_list|audit_log)$")
    filters: dict[str, Any] = Field(default_factory=dict[str, Any])
    columns: list[str] | None = None
    sort_by: str | None = Field(None, max_length=100)
    sort_order: str = Field(default="desc", pattern=r"^(asc|desc)$")


class ViewUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    filters: dict[str, Any] | None = None
    columns: list[str] | None = None
    sort_by: str | None = Field(None, max_length=100)
    sort_order: str | None = Field(None, pattern=r"^(asc|desc)$")


class ViewResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    view_type: str
    filters: dict[str, Any]
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


@router.get("", response_model=ViewListResponse, dependencies=[require_feature("view_modes")])
async def list_views_endpoint(
    view_type: str | None = Query(None, pattern=r"^(run_list|pipeline_list|audit_log)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
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
        ) from None
    except SQLAlchemyError:
        logger.exception("views.list.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("views.list.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    return ViewListResponse(
        items=[ViewResponse.model_validate(v) for v in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "",
    response_model=ViewResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_feature("view_modes")],
)
async def create_view_endpoint(
    req: ViewCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ViewResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            view = await create_view(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                view_type=req.view_type,
                account_id=principal.account_id,
                description=req.description,
                filters=req.filters,
                columns=req.columns,
                sort_by=req.sort_by,
                sort_order=req.sort_order,
            )
    except ProgrammingError:
        logger.exception("views.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The saved_views feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("views.create.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("views.create.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    return ViewResponse.model_validate(view)


@router.get("/{view_id}", response_model=ViewResponse, dependencies=[require_feature("view_modes")])
async def get_view_endpoint(
    view_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
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
        ) from None
    except SQLAlchemyError:
        logger.exception("views.get.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("views.get.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="View not found")
    return ViewResponse.model_validate(view)


@router.patch("/{view_id}", response_model=ViewResponse, dependencies=[require_feature("view_modes")])
async def update_view_endpoint(
    view_id: uuid.UUID,
    req: ViewUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ViewResponse:
    updates = req.model_dump(exclude_unset=True)
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
        ) from None
    except SQLAlchemyError:
        logger.exception("views.update.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("views.update.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="View not found")
    return ViewResponse.model_validate(view)


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[require_feature("view_modes")])
async def delete_view_endpoint(
    view_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
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
        ) from None
    except SQLAlchemyError:
        logger.exception("views.delete.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("views.delete.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="View not found")

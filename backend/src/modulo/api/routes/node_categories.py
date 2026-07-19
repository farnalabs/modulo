"""NodeCategory CRUD REST API."""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.node_category import (
    create_node_category,
    delete_node_category,
    get_node_category,
    list_node_categories,
    update_node_category,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/node-categories", tags=["node-categories"])


class NodeCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = Field(None, max_length=50)
    sort_order: int = 0


class NodeCategoryUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = Field(None, max_length=50)
    sort_order: int | None = None


class NodeCategoryResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    color: str
    icon: str | None
    sort_order: int
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NodeCategoryListResponse(BaseModel):
    items: list[NodeCategoryResponse]
    total: int
    page: int
    page_size: int


@handle_db_errors("node_categories.list_node_categories_endpoint")
@router.get("", response_model=NodeCategoryListResponse)
async def list_node_categories_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> NodeCategoryListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_node_categories(
                session, org_id=principal.organisation_id, page=page, page_size=page_size
            )
    except ProgrammingError:
        logger.warning("node_categories.list.programming_error — missing DB table?")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except IntegrityError:
        logger.warning("node_categories.list.integrity_error")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A node category with this name already exists.",
        ) from None
    except SQLAlchemyError:
        logger.warning("node_categories.list.database_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("node_categories.list.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    return NodeCategoryListResponse(
        items=[NodeCategoryResponse.model_validate(c) for c in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@handle_db_errors("node_categories.create_node_category_endpoint")
@router.post("", response_model=NodeCategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_node_category_endpoint(
    req: NodeCategoryCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> NodeCategoryResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            category = await create_node_category(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                description=req.description,
                color=req.color,
                icon=req.icon,
                sort_order=req.sort_order,
            )
    except ProgrammingError:
        logger.warning("node_categories.create.programming_error — missing DB table?")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except IntegrityError:
        logger.warning("node_categories.create.integrity_error — duplicate name")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A node category with this name already exists.",
        ) from None
    except SQLAlchemyError:
        logger.warning("node_categories.create.database_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("node_categories.create.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    return NodeCategoryResponse.model_validate(category)


@handle_db_errors("node_categories.get_node_category_endpoint")
@router.get("/{category_id}", response_model=NodeCategoryResponse)
async def get_node_category_endpoint(
    category_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> NodeCategoryResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            category = await get_node_category(session, category_id, org_id=principal.organisation_id)
    except ProgrammingError:
        logger.warning("node_categories.get.programming_error — missing DB table?")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except IntegrityError:
        logger.warning("node_categories.get.integrity_error")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A node category with this name already exists.",
        ) from None
    except SQLAlchemyError:
        logger.warning("node_categories.get.database_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("node_categories.get.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node category not found")
    return NodeCategoryResponse.model_validate(category)


@handle_db_errors("node_categories.update_node_category_endpoint")
@router.patch("/{category_id}", response_model=NodeCategoryResponse)
async def update_node_category_endpoint(
    category_id: uuid.UUID,
    req: NodeCategoryUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> NodeCategoryResponse:
    updates = req.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            category = await update_node_category(session, category_id, updates, org_id=principal.organisation_id)
    except ProgrammingError:
        logger.warning("node_categories.update.programming_error — missing DB table?")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except IntegrityError:
        logger.warning("node_categories.update.integrity_error — duplicate name")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A node category with this name already exists.",
        ) from None
    except SQLAlchemyError:
        logger.warning("node_categories.update.database_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("node_categories.update.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node category not found")
    return NodeCategoryResponse.model_validate(category)


@handle_db_errors("node_categories.delete_node_category_endpoint")
@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node_category_endpoint(
    category_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_node_category(session, category_id, org_id=principal.organisation_id)
    except ProgrammingError:
        logger.warning("node_categories.delete.programming_error — missing DB table?")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except IntegrityError:
        logger.warning("node_categories.delete.integrity_error")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete: the category is referenced by one or more nodes.",
        ) from None
    except SQLAlchemyError:
        logger.warning("node_categories.delete.database_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("node_categories.delete.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node category not found")

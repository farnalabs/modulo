"""NodeCategory CRUD REST API."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.node_category import (
    create_node_category,
    delete_node_category,
    get_node_category,
    list_node_categories,
    update_node_category,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

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


@router.get("", response_model=NodeCategoryListResponse)
async def list_node_categories_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NodeCategoryListResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        result = await list_node_categories(session, page=page, page_size=page_size)
    return NodeCategoryListResponse(
        items=[NodeCategoryResponse.model_validate(c) for c in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=NodeCategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_node_category_endpoint(
    req: NodeCategoryCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NodeCategoryResponse:
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
    return NodeCategoryResponse.model_validate(category)


@router.get("/{category_id}", response_model=NodeCategoryResponse)
async def get_node_category_endpoint(
    category_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NodeCategoryResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        category = await get_node_category(session, category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node category not found")
    return NodeCategoryResponse.model_validate(category)


@router.patch("/{category_id}", response_model=NodeCategoryResponse)
async def update_node_category_endpoint(
    category_id: uuid.UUID,
    req: NodeCategoryUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NodeCategoryResponse:
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        category = await update_node_category(session, category_id, updates)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node category not found")
    return NodeCategoryResponse.model_validate(category)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node_category_endpoint(
    category_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        deleted = await delete_node_category(session, category_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node category not found")

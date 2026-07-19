"""Parameter Schema and Parameter Set CRUD REST API."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.parameter_schema import (
    create_schema,
    delete_schema,
    get_schema,
    get_schema_references,
    list_schemas,
    update_schema,
)
from modulo.db.crud.parameter_set import (
    create_set,
    delete_set,
    get_set,
    get_set_references,
    list_sets,
    update_set,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["parameter-schemas"])


# ---------------------------------------------------------------------------
# ParameterDef
# ---------------------------------------------------------------------------


class ParameterDef(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    label: str | None = None
    description: str | None = None
    type: str = Field(default="string", pattern=r"^(string|number|boolean|select|model_backend_ref|schema_ref)$")
    required: bool = False
    default_value: Any = None
    multiline: bool = False
    options: list[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    placeholder: str | None = None
    target_injection: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------


class SchemaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    parameters: list[ParameterDef] = []


class SchemaUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    parameters: list[ParameterDef] | None = None
    version: int


class SchemaResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    version: int
    parameters: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    account_id: uuid.UUID

    model_config = {"from_attributes": True}


class SchemaListResponse(BaseModel):
    items: list[SchemaResponse]
    total: int
    page: int
    page_size: int


class SchemaReferencesResponse(BaseModel):
    agents: list[dict[str, Any]]
    sets: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Set models
# ---------------------------------------------------------------------------


class SetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    values: dict[str, Any] = {}


class SetUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    values: dict[str, Any] | None = None
    version: int


class SetResponse(BaseModel):
    id: uuid.UUID
    parameter_schema_id: uuid.UUID
    organisation_id: uuid.UUID
    version: int
    schema_version: int
    name: str
    description: str | None
    values: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    account_id: uuid.UUID

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Validation models
# ---------------------------------------------------------------------------


class ValidateRequest(BaseModel):
    values: dict[str, Any]


class ValidationErrorItem(BaseModel):
    field: str
    message: str


class ValidateResponse(BaseModel):
    valid: bool
    errors: list[ValidationErrorItem] = []


# ---------------------------------------------------------------------------
# Schema endpoints
# ---------------------------------------------------------------------------


@router.get("/parameter-schemas", response_model=SchemaListResponse)
@handle_db_errors("parameter_schemas.list")
async def list_parameter_schemas_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> SchemaListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_schemas(session, org_id=principal.organisation_id, limit=page_size)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.list")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return SchemaListResponse(
        items=[SchemaResponse.model_validate(s) for s in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("/parameter-schemas", response_model=SchemaResponse, status_code=status.HTTP_201_CREATED)
@handle_db_errors("parameter_schemas.create")
async def create_parameter_schema_endpoint(
    req: SchemaCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await create_schema(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                description=req.description,
                parameters=[p.model_dump() for p in req.parameters],
                account_id=principal.account_id,
            )
    except IntegrityError:
        logger.exception("parameter_schemas.create.conflict")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A parameter schema with this name already exists.",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.create")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.create")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.create")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return SchemaResponse.model_validate(schema)


@router.get("/parameter-schemas/{schema_id}", response_model=SchemaResponse)
@handle_db_errors("parameter_schemas.get")
async def get_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.get")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.get")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.get")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter schema not found")
    return SchemaResponse.model_validate(schema)


@router.put("/parameter-schemas/{schema_id}", response_model=SchemaResponse)
@handle_db_errors("parameter_schemas.update")
async def update_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    req: SchemaUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await update_schema(
                session,
                schema_id,
                name=req.name,
                description=req.description,
                parameters=[p.model_dump() for p in req.parameters] if req.parameters is not None else None,
                version=req.version,
            )
    except IntegrityError:
        logger.exception("parameter_schemas.update.conflict")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A parameter schema with this name already exists.",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.update")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.update")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.update")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if schema is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Schema was modified by another user. Refresh and retry.",
        )
    return SchemaResponse.model_validate(schema)


@router.delete("/parameter-schemas/{schema_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("parameter_schemas.delete")
async def delete_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_schema(session, schema_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.delete")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.delete")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.delete")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Schema cannot be deleted: it is referenced by agents or parameter sets.",
        )


@router.get("/parameter-schemas/{schema_id}/diff")
@handle_db_errors("parameter_schemas.diff")
async def diff_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    from_version: int = Query(..., description="Source version"),
    to_version: int = Query(..., description="Target version"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
    except SQLAlchemyError:
        logger.exception("parameter_schemas.diff")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.diff")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter schema not found")

    if from_version < 1 or to_version < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Versions must be >= 1",
        )

    current_params: list[dict[str, Any]] = schema.parameters if isinstance(schema.parameters, list) else []

    changes: list[dict[str, Any]] = []
    if from_version == to_version:
        return {"from_version": from_version, "to_version": to_version, "changes": changes}

    if from_version != schema.version and to_version != schema.version:
        return {
            "from_version": from_version,
            "to_version": to_version,
            "changes": changes,
            "warning": f"Only current version (v{schema.version}) is available. Historical version data is not stored.",
        }

    param_names: list[str] = [p.get("name", "") for p in current_params if isinstance(p, dict)]
    changes = [{"action": "unchanged", "name": name} for name in param_names]

    return {
        "from_version": from_version,
        "to_version": to_version,
        "changes": changes,
        "current_version": schema.version,
        "total_parameters": len(current_params),
    }


@router.get("/parameter-schemas/{schema_id}/references", response_model=SchemaReferencesResponse)
@handle_db_errors("parameter_schemas.references")
async def get_parameter_schema_references_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> SchemaReferencesResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            refs = await get_schema_references(session, schema_id)
    except ProgrammingError:
        logger.exception("parameter_schemas.references")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.references")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.references")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return SchemaReferencesResponse(
        agents=[{"id": str(a)} for a in refs["agents"]],
        sets=[{"id": str(s)} for s in refs["sets"]],
    )


@router.post("/parameter-schemas/{schema_id}/validate", response_model=ValidateResponse)
@handle_db_errors("parameter_schemas.validate")
async def validate_parameter_values_endpoint(
    schema_id: uuid.UUID,
    req: ValidateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ValidateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter schema not found")

            params = schema.parameters if isinstance(schema.parameters, list) else []
            errors: list[ValidationErrorItem] = []
            param_map: dict[str, dict[str, Any]] = {}
            for p in params:
                if isinstance(p, dict):
                    param_map[p.get("name", "")] = p

            for p_name, p_def in param_map.items():
                p_type = p_def.get("type", "string")
                p_required = p_def.get("required", False)
                value = req.values.get(p_name)

                if p_required and value is None:
                    errors.append(ValidationErrorItem(field=p_name, message="This field is required."))
                    continue
                if value is None:
                    continue

                if p_type == "string" and not isinstance(value, str):
                    errors.append(ValidationErrorItem(field=p_name, message="Expected a string value."))
                elif p_type == "number":
                    if not isinstance(value, (int, float)):
                        errors.append(ValidationErrorItem(field=p_name, message="Expected a numeric value."))
                    else:
                        p_min = p_def.get("minimum")
                        p_max = p_def.get("maximum")
                        if p_min is not None and value < p_min:
                            errors.append(ValidationErrorItem(field=p_name, message=f"Value must be >= {p_min}."))
                        if p_max is not None and value > p_max:
                            errors.append(ValidationErrorItem(field=p_name, message=f"Value must be <= {p_max}."))
                elif p_type == "boolean" and not isinstance(value, bool):
                    errors.append(ValidationErrorItem(field=p_name, message="Expected a boolean value."))
                elif p_type == "select":
                    options = p_def.get("options", [])
                    if options and str(value) not in options:
                        errors.append(
                            ValidationErrorItem(
                                field=p_name,
                                message=f"Value must be one of: {', '.join(str(o) for o in options)}.",
                            )
                        )
    except ProgrammingError:
        logger.exception("parameter_schemas.validate")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.validate")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.validate")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None

    return ValidateResponse(valid=len(errors) == 0, errors=errors)


# ---------------------------------------------------------------------------
# Set endpoints (nested under schema)
# ---------------------------------------------------------------------------


@router.get("/parameter-schemas/{schema_id}/sets", response_model=list[SetResponse])
@handle_db_errors("parameter_schemas.list_sets")
async def list_parameter_sets_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[SetResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter schema not found")
            sets = await list_sets(
                session,
                parameter_schema_id=schema_id,
                org_id=principal.organisation_id,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.list_sets")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.list_sets")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.list_sets")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return [SetResponse.model_validate(s) for s in sets]


@router.post(
    "/parameter-schemas/{schema_id}/sets",
    response_model=SetResponse,
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors("parameter_schemas.create_set")
async def create_parameter_set_endpoint(
    schema_id: uuid.UUID,
    req: SetCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> SetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter schema not found")
            ps = await create_set(
                session,
                parameter_schema_id=schema_id,
                org_id=principal.organisation_id,
                name=req.name,
                description=req.description,
                values=req.values,
                account_id=principal.account_id,
                schema_version=schema.version,
            )
    except IntegrityError:
        logger.exception("parameter_schemas.create_set.conflict")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A parameter set with this name already exists for this schema.",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.create_set")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.create_set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.create_set")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return SetResponse.model_validate(ps)


@router.get("/parameter-schemas/{schema_id}/sets/{set_id}", response_model=SetResponse)
@handle_db_errors("parameter_schemas.get_set")
async def get_parameter_set_endpoint(
    schema_id: uuid.UUID,
    set_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> SetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            ps = await get_set(session, set_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.get_set")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.get_set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.get_set")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if ps is None or ps.parameter_schema_id != schema_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter set not found")
    return SetResponse.model_validate(ps)


@router.put("/parameter-schemas/{schema_id}/sets/{set_id}", response_model=SetResponse)
@handle_db_errors("parameter_schemas.update_set")
async def update_parameter_set_endpoint(
    schema_id: uuid.UUID,
    set_id: uuid.UUID,
    req: SetUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> SetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter schema not found")
            ps = await update_set(
                session,
                set_id,
                name=req.name,
                description=req.description,
                values=req.values,
                version=req.version,
            )
    except IntegrityError:
        logger.exception("parameter_schemas.update_set.conflict")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A parameter set with this name already exists.",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.update_set")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.update_set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.update_set")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if ps is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parameter set was modified by another user. Refresh and retry.",
        )
    return SetResponse.model_validate(ps)


@router.delete(
    "/parameter-schemas/{schema_id}/sets/{set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@handle_db_errors("parameter_schemas.delete_set")
async def delete_parameter_set_endpoint(
    schema_id: uuid.UUID,
    set_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter schema not found")
            deleted = await delete_set(session, set_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.delete_set")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter schemas are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.delete_set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter schemas are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.delete_set")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter set not found")


# ---------------------------------------------------------------------------
# Global set references
# ---------------------------------------------------------------------------


@router.get("/parameter-sets/{set_id}/references")
@handle_db_errors("parameter_sets.references")
async def get_parameter_set_references_endpoint(
    set_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, list[uuid.UUID]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            refs = await get_set_references(session, set_id)
    except ProgrammingError:
        logger.exception("parameter_sets.references")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter sets are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_sets.references")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter sets are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_sets.references")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return refs

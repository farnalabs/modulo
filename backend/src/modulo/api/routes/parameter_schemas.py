"""Parameter Schema and Parameter Set CRUD REST API."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_RESOURCE_ALREADY_EXISTS, MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.parameter_schema import (
    create_schema,
    get_schema,
    get_schema_references,
    list_schemas,
    restore_schema,
    soft_delete_schema,
    update_schema,
)
from modulo.db.crud.parameter_set import (
    create_set,
    get_set,
    get_set_references,
    list_sets,
    restore_set,
    soft_delete_set,
    update_set,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_PARAMETER_SCHEMA_LIST = "parameter_schema.list"
_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE = "Parameter schemas are not available. Run database migrations to enable it."
_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE = "Parameter schemas are temporarily unavailable."
_CODE_PARAMETER_SCHEMAS_CREATE = "parameter_schemas.create"
_CODE_PARAMETER_SCHEMAS_GET = "parameter_schemas.get"
_MSG_PARAMETER_SCHEMA_NOT_FOUND = "Parameter schema not found"
_CODE_PARAMETER_SCHEMAS_UPDATE = "parameter_schemas.update"
_CODE_PARAMETER_SCHEMAS_DELETE = "parameter_schemas.delete"
_CODE_PARAMETER_SCHEMAS_RESTORE = "parameter_schemas.restore"
_CODE_PARAMETER_SCHEMAS_REFERENCES = "parameter_schemas.references"
_CODE_PARAMETER_SCHEMAS_VALIDATE = "parameter_schemas.validate"
_CODE_PARAMETER_SCHEMAS_LIST_SETS = "parameter_schemas.list_sets"
_CODE_PARAMETER_SCHEMAS_CREATE_SET = "parameter_schemas.create_set"
_CODE_PARAMETER_SCHEMAS_GET_SET = "parameter_schemas.get_set"
_MSG_PARAMETER_SET_NOT_FOUND = "Parameter set not found"
_CODE_PARAMETER_SCHEMAS_UPDATE_SET = "parameter_schemas.update_set"
_CODE_PARAMETER_SCHEMAS_DELETE_SET = "parameter_schemas.delete_set"
_CODE_PARAMETER_SCHEMAS_RESTORE_SET = "parameter_schemas.restore_set"
_CODE_PARAMETER_SETS_REFERENCES = "parameter_sets.references"


logger = logging.getLogger(__name__)

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
    parameters: list[ParameterDef] = Field(default_factory=list)


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
    values: dict[str, Any] = Field(default_factory=dict)


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
    errors: list[ValidationErrorItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema endpoints
# ---------------------------------------------------------------------------


@router.get("/parameter-schemas")
@handle_db_errors("parameter_schemas.list")
async def list_parameter_schemas_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_PARAMETER_SCHEMA_LIST),
) -> SchemaListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_schemas(session, org_id=principal.organisation_id, limit=page_size)
    except IntegrityError:
        logger.exception("parameter_schemas.list_parameter_schemas_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("parameter_schemas.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("parameter_schemas.list")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return SchemaListResponse(
        items=[SchemaResponse.model_validate(s) for s in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("/parameter-schemas", status_code=status.HTTP_201_CREATED)
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_CREATE)
async def create_parameter_schema_endpoint(
    req: SchemaCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.create"),
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
        logger.exception(_CODE_PARAMETER_SCHEMAS_CREATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_CREATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_CREATE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return SchemaResponse.model_validate(schema)


@router.get("/parameter-schemas/{schema_id}")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_GET)
async def get_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_PARAMETER_SCHEMA_LIST),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
    except IntegrityError:
        logger.exception("parameter_schemas.get_parameter_schema_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_GET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_GET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_GET)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)
    return SchemaResponse.model_validate(schema)


@router.put("/parameter-schemas/{schema_id}")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_UPDATE)
async def update_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    req: SchemaUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.update"),
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
        logger.exception(_CODE_PARAMETER_SCHEMAS_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if schema is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Schema was modified by another user. Refresh and retry.",
        )
    return SchemaResponse.model_validate(schema)


@router.delete("/parameter-schemas/{schema_id}")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_DELETE)
async def delete_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.delete"),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await soft_delete_schema(session, schema_id)
    except IntegrityError:
        logger.exception("parameter_schemas.delete_parameter_schema_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_DELETE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_DELETE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_DELETE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)
    return SchemaResponse.model_validate(schema)


@router.post("/parameter-schemas/{schema_id}/restore")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_RESTORE)
async def restore_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.update"),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await restore_schema(session, schema_id)
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_RESTORE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_RESTORE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_RESTORE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter schema not found or not deleted")
    return SchemaResponse.model_validate(schema)


@router.get("/parameter-schemas/{schema_id}/diff")
@handle_db_errors("parameter_schemas.diff")
async def diff_parameter_schema_endpoint(
    schema_id: uuid.UUID,
    from_version: int = Query(..., description="Source version"),
    to_version: int = Query(..., description="Target version"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_PARAMETER_SCHEMA_LIST),
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
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("parameter_schemas.diff")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)

    if from_version < 1 or to_version < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Versions must be >= 1",
        )

    current_params: list[dict[str, Any]] = schema.parameters if isinstance(schema.parameters, list) else []

    changes: list[dict[str, Any]] = []
    if from_version == to_version:
        return {"from_version": from_version, "to_version": to_version, "changes": changes}

    if schema.version not in (from_version, to_version):
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


@router.get("/parameter-schemas/{schema_id}/references")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_REFERENCES)
async def get_parameter_schema_references_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_PARAMETER_SCHEMA_LIST),
) -> SchemaReferencesResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            refs = await get_schema_references(session, schema_id)
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_REFERENCES)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_REFERENCES)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_REFERENCES)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return SchemaReferencesResponse(
        agents=[{"id": str(a)} for a in refs["agents"]],
        sets=[{"id": str(s)} for s in refs["sets"]],
    )


@router.post("/parameter-schemas/{schema_id}/validate")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_VALIDATE)
async def validate_parameter_values_endpoint(
    schema_id: uuid.UUID,
    req: ValidateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.validate"),
) -> ValidateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)

            params = schema.parameters if isinstance(schema.parameters, list) else []
            errors: list[ValidationErrorItem] = []
            param_map = {p.get("name", ""): p for p in params if isinstance(p, dict)}

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
        logger.exception(_CODE_PARAMETER_SCHEMAS_VALIDATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_VALIDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_VALIDATE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return ValidateResponse(valid=len(errors) == 0, errors=errors)


# ---------------------------------------------------------------------------
# Set endpoints (nested under schema)
# ---------------------------------------------------------------------------


@router.get("/parameter-schemas/{schema_id}/sets")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_LIST_SETS)
async def list_parameter_sets_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_PARAMETER_SCHEMA_LIST),
) -> list[SetResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)
            sets = await list_sets(
                session,
                parameter_schema_id=schema_id,
                org_id=principal.organisation_id,
            )
    except IntegrityError:
        logger.exception("parameter_schemas.list_parameter_sets_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_LIST_SETS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_LIST_SETS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_LIST_SETS)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return [SetResponse.model_validate(s) for s in sets]


@router.post(
    "/parameter-schemas/{schema_id}/sets",
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_CREATE_SET)
async def create_parameter_set_endpoint(
    schema_id: uuid.UUID,
    req: SetCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.set.create"),
) -> SetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)
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
        logger.exception(_CODE_PARAMETER_SCHEMAS_CREATE_SET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_CREATE_SET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_CREATE_SET)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return SetResponse.model_validate(ps)


@router.get("/parameter-schemas/{schema_id}/sets/{set_id}")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_GET_SET)
async def get_parameter_set_endpoint(
    schema_id: uuid.UUID,
    set_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_PARAMETER_SCHEMA_LIST),
) -> SetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            ps = await get_set(session, set_id)
    except IntegrityError:
        logger.exception("parameter_schemas.get_parameter_set_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_GET_SET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_GET_SET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_GET_SET)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if ps is None or ps.parameter_schema_id != schema_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SET_NOT_FOUND)
    return SetResponse.model_validate(ps)


@router.put("/parameter-schemas/{schema_id}/sets/{set_id}")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_UPDATE_SET)
async def update_parameter_set_endpoint(
    schema_id: uuid.UUID,
    set_id: uuid.UUID,
    req: SetUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.set.update"),
) -> SetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)
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
        logger.exception(_CODE_PARAMETER_SCHEMAS_UPDATE_SET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_UPDATE_SET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_UPDATE_SET)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if ps is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parameter set was modified by another user. Refresh and retry.",
        )
    return SetResponse.model_validate(ps)


@router.delete(
    "/parameter-schemas/{schema_id}/sets/{set_id}",
)
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_DELETE_SET)
async def delete_parameter_set_endpoint(
    schema_id: uuid.UUID,
    set_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.set.delete"),
) -> SetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)
            ps = await soft_delete_set(session, set_id)
    except IntegrityError:
        logger.exception("parameter_schemas.delete_parameter_set_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_DELETE_SET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_DELETE_SET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_DELETE_SET)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if ps is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SET_NOT_FOUND)
    if ps.parameter_schema_id != schema_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SET_NOT_FOUND)
    return SetResponse.model_validate(ps)


@router.post("/parameter-schemas/{schema_id}/sets/{set_id}/restore")
@handle_db_errors(_CODE_PARAMETER_SCHEMAS_RESTORE_SET)
async def restore_parameter_set_endpoint(
    schema_id: uuid.UUID,
    set_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("parameter_schema.set.update"),
) -> SetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            schema = await get_schema(session, schema_id)
            if schema is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PARAMETER_SCHEMA_NOT_FOUND)
            ps = await restore_set(session, set_id)
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_RESTORE_SET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_PARAMETER_SCHEMAS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SCHEMAS_RESTORE_SET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_PARAMETER_SCHEMAS_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SCHEMAS_RESTORE_SET)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if ps is None or ps.parameter_schema_id != schema_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parameter set not found or not deleted")
    return SetResponse.model_validate(ps)


# ---------------------------------------------------------------------------
# Global set references
# ---------------------------------------------------------------------------


@router.get("/parameter-sets/{set_id}/references")
@handle_db_errors(_CODE_PARAMETER_SETS_REFERENCES)
async def get_parameter_set_references_endpoint(
    set_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_PARAMETER_SCHEMA_LIST),
) -> dict[str, list[uuid.UUID]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            refs = await get_set_references(session, set_id)
    except ProgrammingError:
        logger.exception(_CODE_PARAMETER_SETS_REFERENCES)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Parameter sets are not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_PARAMETER_SETS_REFERENCES)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Parameter sets are temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_PARAMETER_SETS_REFERENCES)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return refs

"""Schema and SchemaVersion CRUD REST API."""

import asyncio
import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from jsonschema import Draft202012Validator, ValidationError
from jsonschema.exceptions import SchemaError as JsSchemaError
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_RESOURCE_ALREADY_EXISTS, MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import append_audit_event_isolated
from modulo.core.connector_hub import ConnectorHub
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.schema_registry import (
    SchemaGenerationError,
    SchemaGenerationService,
    SchemaInferenceError,
    SchemaInferenceService,
    apply_migration,
    create_migration,
    flag_rare_fields,
)
from modulo.core.schema_registry.inference import SUPPORTED_INFERENCE_TYPES
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.connector_instance import get_connector_instance
from modulo.db.crud.model_backend import list_model_backends
from modulo.db.crud.schema import (
    SchemaDeletionProtectedError,
    create_schema,
    create_schema_version,
    delete_schema,
    deprecate_schema,
    get_schema,
    get_schema_version,
    list_schema_versions,
    list_schemas,
    update_schema,
)
from modulo.db.crud.schema_folder import move_schema_to_folder
from modulo.db.models.schema import Schema
from modulo.db.models.schema import SchemaVersion as SchemaVersionModel
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

_CODE_SCHEMA_LIST = "schema.list"
_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE = "Schema management is not available. Run database migrations to enable it."
_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE = "Schema management is temporarily unavailable."
_CODE_SCHEMA_CREATE = "schema.create"
_MSG_SCHEMA_NOT_FOUND = "Schema not found"
_CODE_SCHEMA_UPDATE = "schema.update"
_SCHEMA_ID_PATH = "/{schema_id}"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/schemas", tags=["schemas"])


async def _assert_owns_schema(session: AsyncSession, schema_id: uuid.UUID, principal: TenantPrincipal) -> "Schema":
    """Load a schema by id and assert the caller's org owns it.

    The application session is RLS-enforced (the app role is not BYPASSRLS), but
    we assert ownership explicitly to give consistent 404s on non-Postgres
    backends that rely on the ORM tenant filter. Raises 404 (not 403) to avoid
    leaking existence.
    """
    schema = await get_schema(session, schema_id)
    if schema is None or schema.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SCHEMA_NOT_FOUND)
    return schema


# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------


class SchemaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    abstract_name: str | None = None


class SchemaUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    abstract_name: str | None = None


class SchemaResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    abstract_name: str | None
    folder_id: uuid.UUID | None = None
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime
    deprecated: bool = False
    deprecated_at: datetime | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class SchemaListResponse(BaseModel):
    items: list[SchemaResponse]
    total: int
    page: int
    page_size: int


class SchemaCountsResponse(BaseModel):
    total: int
    by_folder: dict[str, int]


# ---------------------------------------------------------------------------
# SchemaVersion models
# ---------------------------------------------------------------------------


class SchemaVersionCreate(BaseModel):
    version: str
    version_number: int
    definition_json: dict[str, Any]
    published: bool = False


class SchemaVersionResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    schema_id: uuid.UUID
    version: str
    version_number: int
    definition_json: dict[str, Any]
    published: bool
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class SchemaVersionListResponse(BaseModel):
    items: list[SchemaVersionResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Schema routes
# ---------------------------------------------------------------------------


@router.get("", responses={401: {"description": "Unauthorized"}})
@handle_db_errors("schemas.list_schemas_endpoint")
async def list_schemas_endpoint(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    folder_id: Annotated[uuid.UUID | None, Query()] = None,
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> SchemaListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await list_schemas(session, cursor=None, limit=page_size, folder_id=folder_id)
    except IntegrityError:
        logger.exception("schemas.list_schemas_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.list_schemas")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.list_schemas")
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


@router.get("/counts")
@handle_db_errors("schemas.counts_endpoint")
async def schema_counts_endpoint(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> SchemaCountsResponse:
    """Return total schema count and per-folder counts for the caller's org.

    A single GROUP BY query over the schemas table, org-scoped via RLS and an
    explicit organisation_id filter.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            rows = (
                await session.execute(
                    select(Schema.folder_id, func.count(Schema.id))
                    .where(Schema.organisation_id == principal.organisation_id)
                    .group_by(Schema.folder_id)
                )
            ).all()
    except ProgrammingError:
        logger.exception("schemas.counts")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.counts")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    total = 0
    by_folder: dict[str, int] = {}
    for folder_id, count in rows:
        total += count
        if folder_id is not None:
            by_folder[str(folder_id)] = count
    return SchemaCountsResponse(total=total, by_folder=by_folder)


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors("schemas.create_schema_endpoint")
async def create_schema_endpoint(
    req: SchemaCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_CREATE),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            schema = await create_schema(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                description=req.description,
                abstract_name=req.abstract_name,
            )
            sv = SchemaVersionModel(
                organisation_id=principal.organisation_id,
                schema_id=schema.id,
                version="latest",
                version_number=0,
                definition_json={"type": "object", "properties": {}, "additionalProperties": True},
                account_id=principal.account_id,
            )
            session.add(sv)
    except IntegrityError:
        logger.exception("schemas.create.conflict")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A schema with this name already exists.",
        ) from None
    except ProgrammingError:
        logger.exception("schemas.create")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.create_schema")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.create_schema")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return SchemaResponse.model_validate(schema)


@router.get(_SCHEMA_ID_PATH)
@handle_db_errors("schemas.get_schema_endpoint")
async def get_schema_endpoint(
    schema_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            schema = await get_schema(session, schema_id)
    except IntegrityError:
        logger.exception("schemas.get_schema_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.get")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.get_schema")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.get_schema")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if schema is None or schema.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SCHEMA_NOT_FOUND)
    return SchemaResponse.model_validate(schema)


@router.patch(_SCHEMA_ID_PATH)
@handle_db_errors("schemas.update_schema_endpoint")
async def update_schema_endpoint(
    schema_id: uuid.UUID,
    req: SchemaUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_UPDATE),
) -> SchemaResponse:
    updates = req.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _assert_owns_schema(session, schema_id, principal)
            schema = await update_schema(session, schema_id, updates)
    except IntegrityError:
        logger.exception("schemas.update_integrity")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A schema with this name already exists in your organisation.",
        ) from None
    except ProgrammingError:
        logger.exception("schemas.update")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.update_schema")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.update_schema")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SCHEMA_NOT_FOUND)
    return SchemaResponse.model_validate(schema)


@router.patch("/{schema_id}/deprecate")
@handle_db_errors("schemas.deprecate_schema_endpoint")
async def deprecate_schema_endpoint(
    schema_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_UPDATE),
) -> SchemaResponse:
    """Mark a schema as deprecated."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _assert_owns_schema(session, schema_id, principal)
            schema = await deprecate_schema(session, schema_id)
    except IntegrityError:
        logger.exception("schemas.deprecate_schema_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.deprecate")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.deprecate_schema")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.deprecate_schema")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SCHEMA_NOT_FOUND)
    return SchemaResponse.model_validate(schema)


# ---------------------------------------------------------------------------
# Folder assignment
# ---------------------------------------------------------------------------


class SchemaFolderMoveRequest(BaseModel):
    folder_id: uuid.UUID | None = None


@router.patch("/{schema_id}/folder")
@handle_db_errors("schemas.move_to_folder")
async def move_schema_to_folder_endpoint(
    schema_id: uuid.UUID,
    req: SchemaFolderMoveRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_UPDATE),
) -> SchemaResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _assert_owns_schema(session, schema_id, principal)
            schema = await move_schema_to_folder(session, schema_id, req.folder_id, principal.organisation_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        ) from None
    except ProgrammingError:
        logger.exception("schemas.move_to_folder")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SCHEMA_NOT_FOUND)
    return SchemaResponse.model_validate(schema)


@router.delete(_SCHEMA_ID_PATH, status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("schemas.delete_schema_endpoint")
async def delete_schema_endpoint(
    schema_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    force: Annotated[bool, Query()] = False,
    principal: TenantPrincipal = require_permission("schema.delete"),
) -> None:
    try:
        try:
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                await _assert_owns_schema(session, schema_id, principal)
                deleted = await delete_schema(session, schema_id, force=force)
        except SchemaDeletionProtectedError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
    except IntegrityError:
        logger.exception("schemas.delete_schema_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.delete")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.delete_schema")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.delete_schema")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SCHEMA_NOT_FOUND)


# ---------------------------------------------------------------------------
# SchemaVersion routes (nested under schema)
# ---------------------------------------------------------------------------


@router.get(
    "/{schema_id}/versions",
    dependencies=[require_feature("schema_version_history")],
)
async def list_schema_versions_endpoint(
    schema_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> SchemaVersionListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            schema = await get_schema(session, schema_id)
            if schema is None or schema.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SCHEMA_NOT_FOUND)
            result = await list_schema_versions(session, schema_id, page=page, page_size=page_size)
    except IntegrityError:
        logger.exception("schemas.list_schema_versions_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.list_versions")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.list_schema_versions")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.list_schema_versions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return SchemaVersionListResponse(
        items=[SchemaVersionResponse.model_validate(sv) for sv in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/{schema_id}/versions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_feature("schema_version_history")],
)
async def create_schema_version_endpoint(
    schema_id: uuid.UUID,
    req: SchemaVersionCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_CREATE),
) -> SchemaVersionResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _assert_owns_schema(session, schema_id, principal)
            sv = await create_schema_version(
                session,
                org_id=principal.organisation_id,
                schema_id=schema_id,
                version=req.version,
                version_number=req.version_number,
                definition_json=req.definition_json,
                account_id=principal.account_id,
                published=req.published,
            )
    except IntegrityError:
        logger.exception("schemas.create_version.conflict")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A schema version with this version already exists.",
        ) from None
    except ProgrammingError:
        logger.exception("schemas.create_version")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.create_schema_version")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.create_schema_version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return SchemaVersionResponse.model_validate(sv)


@router.get(
    "/{schema_id}/versions/{version}",
    dependencies=[require_feature("schema_version_history")],
)
async def get_schema_version_endpoint(
    schema_id: uuid.UUID,
    version: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> SchemaVersionResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            sv = await get_schema_version(session, schema_id, version)
    except IntegrityError:
        logger.exception("schemas.get_schema_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.get_version")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.get_schema_version")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.get_schema_version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if sv is None or sv.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema version not found")
    return SchemaVersionResponse.model_validate(sv)


# ---------------------------------------------------------------------------
# Schema Fields
# ---------------------------------------------------------------------------


class SchemaFieldResponse(BaseModel):
    """A single field extracted from a JSON Schema property."""

    name: str
    type: str
    description: str | None = None
    required: bool = False


class SchemaFieldListResponse(BaseModel):
    fields: list[SchemaFieldResponse]


async def _load_latest_definition(session: AsyncSession, schema_id: uuid.UUID, principal: TenantPrincipal) -> Any:
    """Load the latest version of a schema, asserting the caller owns it."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        schema = await get_schema(session, schema_id)
        if schema is None or schema.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SCHEMA_NOT_FOUND)
        sv = await _get_latest_version(session, schema_id)
        if sv is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema has no versions")
    return sv


@router.get("/{schema_id}/fields")
@handle_db_errors("schemas.list_schema_fields_endpoint")
async def list_schema_fields_endpoint(
    schema_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> SchemaFieldListResponse:
    """Return the field list for the latest version of a schema.

    Extracts ``properties`` from the JSON Schema ``definition_json``
    and returns each property as a ``SchemaFieldResponse`` with
    name, type, description, and required status.
    """
    try:
        sv = await _load_latest_definition(session, schema_id, principal)
    except IntegrityError:
        logger.exception("schemas.list_schema_fields_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.list_fields")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.list_schema_fields")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.list_schema_fields")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    definition = sv.definition_json
    properties: dict[str, Any] = definition.get("properties", {})
    required_fields: list[str] = definition.get("required", [])

    fields = [
        SchemaFieldResponse(
            name=field_name,
            type=field_schema.get("type", "string"),
            description=field_schema.get("description"),
            required=field_name in required_fields,
        )
        for field_name, field_schema in properties.items()
        if isinstance(field_schema, dict)
    ]

    return SchemaFieldListResponse(fields=fields)


# ---------------------------------------------------------------------------
# Schema Inference
# ---------------------------------------------------------------------------


class SchemaSampleQuery(BaseModel):
    resource: str = Field(min_length=1)
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=200, ge=1, le=200)


class SchemaInferRequest(BaseModel):
    connector_instance_id: uuid.UUID
    sample_query: SchemaSampleQuery


class SchemaInferResponse(BaseModel):
    definition_json: dict[str, Any]
    sample_count: int
    suggestion_name: str
    suggestion_description: str | None = None
    rare_fields: list[str] = Field(default_factory=list)


async def _sample_connector_records(
    settings: Settings,
    ci: Any,
    req: SchemaInferRequest,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Sample connector data, failing open with informative HTTP errors."""
    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    async with ConnectorHub(secrets_backend=secrets_backend) as ch:
        async with session.begin():
            await set_rls_org(session, ci.organisation_id)
            try:
                await ch.initialise([ci])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("schemas.infer.connector_init_failed")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Failed to initialise connector for sampling.",
                ) from None
        try:
            async with asyncio.timeout(30.0):
                return await ch.sample(
                    connector_id=req.connector_instance_id,
                    resource=req.sample_query.resource,
                    filters=req.sample_query.filters,
                    limit=req.sample_query.limit,
                )
        except TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Connector sampling timed out after 30s",
            ) from None
        except Exception:
            logger.exception("schemas.infer.sampling_failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to sample connector data.",
            ) from None


async def _resolve_model_backend(
    mh: Any,
    mbs: Any,
    secrets_backend: Any,
    *,
    session: AsyncSession,
    organisation_id: uuid.UUID,
    init_log: str,
    init_detail: str,
    empty_detail: str,
    get_log: str,
    get_detail: str,
) -> tuple[Any, uuid.UUID]:
    """Initialise a ``ModelBackendHub`` and return ``(backend, backend_id)``.

    Maps initialisation and selection failures to informative HTTP errors so
    schema inference and generation routes stay thin.

    The model-backend API keys live in the secrets table and are decrypted with
    the org-scoped RLS context, so ``mh.initialise`` must run inside a DB
    transaction with ``app.organisation_id`` set — otherwise ``get_secret``
    raises ``RuntimeError('no DB session')`` before the credentials fallback
    can apply (turning every model-backend init into a blanket 502).
    """
    try:
        async with session.begin():
            await set_rls_org(session, organisation_id)
            await mh.initialise(mbs.items, secrets_backend=secrets_backend)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(init_log)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=init_detail,
        ) from None
    if not mh.backend_ids:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=empty_detail,
        )
    backend_id = next(iter(mh.backend_ids))
    try:
        backend = await mh.get(backend_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(get_log)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=get_detail,
        ) from None
    return backend, backend_id


async def _infer_definition(
    settings: Settings,
    mbs: Any,
    records: list[dict[str, Any]],
    connector_type: str,
    *,
    session: AsyncSession,
    organisation_id: uuid.UUID,
) -> tuple[dict[str, Any], uuid.UUID]:
    """Run LLM schema inference and return ``(definition_json, backend_id)``."""
    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    async with ModelBackendHub() as mh:
        backend, first_backend_id = await _resolve_model_backend(
            mh,
            mbs,
            secrets_backend,
            session=session,
            organisation_id=organisation_id,
            init_log="schemas.infer.backend_init_failed",
            init_detail="Failed to initialise model backend for inference.",
            empty_detail="No model backends available for inference.",
            get_log="schemas.infer.backend_get_failed",
            get_detail="Selected model backend is unavailable.",
        )

        service = SchemaInferenceService(backend, connector_type=connector_type)
        try:
            definition_json = await service.infer(records)
        except SchemaInferenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Schema inference failed: {exc}",
            ) from exc
    return definition_json, first_backend_id


async def _resolve_infer_context(
    session: AsyncSession, principal: TenantPrincipal, req: SchemaInferRequest
) -> tuple[Any, Any]:
    """Load and validate the connector + model backends for schema inference."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)

        ci = await get_connector_instance(session, req.connector_instance_id)
        if ci is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Connector instance not found",
            )

        # Connector-types currently supported for schema inference. Single
        # source of truth lives in `schema_registry/inference.py` and is
        # derived from the `ConnectorType` enum + the connector-type-aware
        # field-extraction categories (PRD §8.16), so this list can't drift
        # from the category map or the enum.
        supported_inference_types = SUPPORTED_INFERENCE_TYPES
        if ci.connector_type_id not in supported_inference_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Connector type '{ci.connector_type_id}' does not support schema inference. "
                f"Supported types: {', '.join(sorted(supported_inference_types))}",
            )

        mbs = await list_model_backends(session, org_id=principal.organisation_id, page_size=1)
        if not mbs.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No model backends configured; cannot perform inference",
            )
        return ci, mbs


@router.post("/infer")
@handle_db_errors("schemas.infer_schema_endpoint")
async def infer_schema_endpoint(
    req: SchemaInferRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    principal: TenantPrincipal = require_permission("schema.infer"),
) -> SchemaInferResponse:
    """Sample data from a connector and infer a JSON Schema via LLM.

    The returned *definition_json* is a draft for the user to review and
    save via the standard POST /api/v1/schemas endpoint.
    """
    try:
        ci, mbs = await _resolve_infer_context(session, principal, req)
    except IntegrityError:
        logger.exception("schemas.infer_schema_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.infer.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Schema inference is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.infer_schema")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.infer.unexpected")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Schema inference failed due to an unexpected error.",
        ) from None

    records = await _sample_connector_records(settings, ci, req, session)
    definition_json, first_backend_id = await _infer_definition(
        settings,
        mbs,
        records,
        connector_type=ci.connector_type_id,
        session=session,
        organisation_id=principal.organisation_id,
    )

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="connector_instance",
        resource_id=req.connector_instance_id,
        event_type="schema_inference_completed",
        payload={
            "connector_name": ci.name,
            "connector_type": ci.connector_type_id,
            "resource": req.sample_query.resource,
            "sample_count": len(records),
            "model_backend_id": str(first_backend_id),
        },
        log_key="schemas.infer.audit_failed",
    )

    return SchemaInferResponse(
        definition_json=definition_json,
        sample_count=len(records),
        suggestion_name=f"Inferred from {ci.name}",
        suggestion_description=(
            f"Auto-inferred schema from {ci.name} ({req.sample_query.resource}, {len(records)} samples)"
        ),
        rare_fields=flag_rare_fields(records),
    )


# ---------------------------------------------------------------------------
# Schema Generation (AI-assisted from description + examples)
# ---------------------------------------------------------------------------


class SchemaGenerateRequest(BaseModel):
    description: str = Field(min_length=1)
    examples: list[dict[str, Any]] = Field(default_factory=list)


class SchemaGenerateResponse(BaseModel):
    definition_json: dict[str, Any]


async def _generate_schema(
    settings: Settings,
    mbs: Any,
    req: SchemaGenerateRequest,
    *,
    session: AsyncSession,
    organisation_id: uuid.UUID,
) -> tuple[dict[str, Any], uuid.UUID]:
    """Run LLM schema generation and return ``(definition_json, backend_id)``."""
    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    async with ModelBackendHub() as mh:
        backend, first_backend_id = await _resolve_model_backend(
            mh,
            mbs,
            secrets_backend,
            session=session,
            organisation_id=organisation_id,
            init_log="schemas.generate.backend_init_failed",
            init_detail="Failed to initialise model backend for generation.",
            empty_detail="No model backends available for generation.",
            get_log="schemas.generate.backend_get_failed",
            get_detail="Selected model backend is unavailable.",
        )

        service = SchemaGenerationService(backend)
        try:
            definition_json = await service.generate(
                description=req.description,
                examples=req.examples or None,
            )
        except SchemaGenerationError as exc:
            logger.exception("schemas.generate.failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Schema generation failed: {exc}",
            ) from exc
    return definition_json, first_backend_id


@router.post("/generate")
@handle_db_errors("schemas.generate_schema_endpoint")
async def generate_schema_endpoint(
    req: SchemaGenerateRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_CREATE),
) -> SchemaGenerateResponse:
    """Generate a JSON Schema from a natural language description and optional
    example records via an LLM.

    The returned *definition_json* is a draft for the user to review and
    save via the standard POST /api/v1/schemas endpoint.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mbs = await list_model_backends(session, org_id=principal.organisation_id, page_size=1)
            if not mbs.items:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No model backends configured; cannot generate schema",
                )
    except IntegrityError:
        logger.exception("schemas.generate_schema_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.generate")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.generate_schema")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.generate.unexpected")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Schema generation failed due to an unexpected error.",
        ) from None

    definition_json, first_backend_id = await _generate_schema(
        settings,
        mbs,
        req,
        session=session,
        organisation_id=principal.organisation_id,
    )

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="schema",
        resource_id=None,
        event_type="schema_generation_completed",
        payload={
            "description_length": len(req.description),
            "example_count": len(req.examples),
            "model_backend_id": str(first_backend_id),
        },
        log_key="schemas.generate.audit_failed",
    )

    return SchemaGenerateResponse(definition_json=definition_json)


# ---------------------------------------------------------------------------
# Schema Migration
# ---------------------------------------------------------------------------


class SchemaMigrationRequest(BaseModel):
    from_schema_id: uuid.UUID
    to_schema_id: uuid.UUID
    data: dict[str, Any]


class SchemaMigrationResponse(BaseModel):
    migrated_data: dict[str, Any]
    plan: dict[str, Any]


class SchemaMigrationPlanRequest(BaseModel):
    from_definition: dict[str, Any]
    to_definition: dict[str, Any]


async def _load_migration_versions(
    session: AsyncSession,
    principal: TenantPrincipal,
    req: SchemaMigrationRequest,
) -> tuple[Any, Any]:
    """Load the latest source and target schema versions within a transaction.

    The application session is RLS-enforced (the app role is not BYPASSRLS), so we
    must set the org context before querying. We additionally assert explicitly
    that the caller's org owns both the source and target schema before touching
    any versions (avoiding a cross-org read, and giving consistent 404s on
    non-Postgres backends that rely on the ORM tenant filter).
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await _assert_owns_schema(session, req.from_schema_id, principal)
        from_sv = await _get_latest_version(session, req.from_schema_id)
        if from_sv is None:
            raise HTTPException(status_code=404, detail="Source schema has no versions")

        await _assert_owns_schema(session, req.to_schema_id, principal)
        to_sv = await _get_latest_version(session, req.to_schema_id)
        if to_sv is None:
            raise HTTPException(status_code=404, detail="Target schema has no versions")
    return from_sv, to_sv


async def _audit_migration(
    session: AsyncSession,
    principal: TenantPrincipal,
    req: SchemaMigrationRequest,
    dry_run: bool,
    plan: Any,
) -> None:
    """Best-effort audit append; failures are logged and never break the response."""
    await append_audit_event_isolated(
        session,
        principal,
        resource_type="schema",
        resource_id=req.to_schema_id,
        event_type="schema_migration_completed",
        payload={
            "from_schema_id": str(req.from_schema_id),
            "to_schema_id": str(req.to_schema_id),
            "dry_run": dry_run,
            "field_additions": len(plan.field_additions),
            "field_removals": len(plan.field_removals),
            "type_changes": len(plan.type_changes),
            "renames": len(plan.renames),
        },
        log_key="schemas.migrate.audit_failed",
    )


def _create_migration_plan(from_definition: dict[str, Any], to_definition: dict[str, Any]) -> Any:
    """Compute a migration plan, mapping failures to a 500 HTTP response."""
    try:
        return create_migration(from_definition, to_definition)
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.migrate_create_plan")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to compute migration plan.",
        ) from None


def _apply_migration_safe(data: dict[str, Any], plan: Any) -> Any:
    """Apply a migration plan to data, mapping failures to a 500 HTTP response."""
    try:
        return apply_migration(data, plan)
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.migrate_apply")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to apply migration to data.",
        ) from None


@router.post(
    "/migrate",
    responses={
        404: {"description": "Not Found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("schemas.migrate_data_endpoint")
async def migrate_data_endpoint(
    req: SchemaMigrationRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_UPDATE),
    dry_run: Annotated[bool, Query(description="If true, preview the migration plan without applying it")] = False,
) -> SchemaMigrationResponse:
    """Migrate data from one schema version to another.

    Accepts *from_schema_id* and *to_schema_id* (Schema UUIDs),
    fetches the latest version of each, computes a migration plan,
    and applies it to *data*.

    Pass ``dry_run=true`` to preview the migration plan without
    applying any transformations.
    """
    try:
        from_sv, to_sv = await _load_migration_versions(session, principal, req)
    except IntegrityError:
        logger.exception("schemas.migrate_data_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception("schemas.migrate")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCHEMA_MANAGEMENT_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("schemas.migrate_data")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCHEMA_MANAGEMENT_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.migrate_data")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    plan = _create_migration_plan(from_sv.definition_json, to_sv.definition_json)

    plan_dict: dict[str, Any] = {
        "field_additions": plan.field_additions,
        "field_removals": plan.field_removals,
        "type_changes": {k: {"old_type": v.old_type, "new_type": v.new_type} for k, v in plan.type_changes.items()},
        "renames": plan.renames,
    }

    await _audit_migration(session, principal, req, dry_run, plan)

    if dry_run:
        plan_dict["dry_run"] = True
        return SchemaMigrationResponse(
            migrated_data=deepcopy(req.data),
            plan=plan_dict,
        )

    migrated = _apply_migration_safe(req.data, plan)

    return SchemaMigrationResponse(
        migrated_data=migrated,
        plan=plan_dict,
    )


@router.post("/migrate/plan")
@handle_db_errors("schemas.migration_plan_endpoint")
async def migration_plan_endpoint(
    req: SchemaMigrationPlanRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> dict[str, Any]:
    """Preview a migration plan between two schemas without applying it.

    Computes a structural diff between two inline definitions. Requires an
    authenticated principal (``schema.list``) and records a
    ``schema_migration_planned`` audit event so plan previews are traceable;
    audit failures are logged and never break the response.
    """
    try:
        plan = create_migration(req.from_definition, req.to_definition)
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("schemas.migration_plan")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to compute migration plan.",
        ) from None

    plan_dict: dict[str, Any] = {
        "field_additions": plan.field_additions,
        "field_removals": plan.field_removals,
        "type_changes": {k: {"old_type": v.old_type, "new_type": v.new_type} for k, v in plan.type_changes.items()},
        "renames": plan.renames,
    }

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="schema",
        resource_id=None,
        event_type="schema_migration_planned",
        payload={
            "field_additions": len(plan.field_additions),
            "field_removals": len(plan.field_removals),
            "type_changes": len(plan.type_changes),
            "renames": len(plan.renames),
        },
        log_key="schemas.migrate_plan.audit_failed",
    )

    return plan_dict


async def _get_latest_version(session: AsyncSession, schema_id: uuid.UUID) -> Any:
    """Fetch the latest SchemaVersion for a given schema_id."""
    versions = await list_schema_versions(session, schema_id, page=1, page_size=1)
    return versions.items[0] if versions.items else None


# ---------------------------------------------------------------------------
# Schema Validation
# ---------------------------------------------------------------------------


class SchemaValidateRequest(BaseModel):
    definition: dict[str, Any]


class SchemaValidationError(BaseModel):
    line: int | None = None
    column: int | None = None
    path: str
    message: str
    schema_path: str | None = None


class SchemaValidateResponse(BaseModel):
    valid: bool
    errors: list[SchemaValidationError]


def _step_json_path(target: Any, part: str) -> Any:
    """Walk one path segment, returning ``None`` when the location is absent."""
    if isinstance(target, dict):
        return target.get(part, {})
    if isinstance(target, list):
        try:
            return target[int(part)]
        except (ValueError, IndexError):
            return None
    return None


def _json_path_exists(target: Any, parts: list[str]) -> bool:
    """Return whether ``parts`` resolves to an existing location in ``target``."""
    for part in parts:
        target = _step_json_path(target, part)
        if target is None:
            return False
    return True


def _find_json_location(raw: str, error_path: str) -> tuple[int | None, int | None]:
    """Best-effort line/column lookup for a validation error path in raw JSON text."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, None

    parts = error_path.strip("/").split("/") if error_path else []
    if not parts or not _json_path_exists(parsed, parts):
        return None, None

    # Seek the key in raw text
    key_to_find = parts[-1]
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        if f'"{key_to_find}"' in line:
            return i + 1, line.index(f'"{key_to_find}"') + 1
    return None, None


@router.post("/validate")
@handle_db_errors("schemas.validate_schema_endpoint")
async def validate_schema_endpoint(
    req: SchemaValidateRequest,
    _: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> SchemaValidateResponse:
    """Validate a JSON Schema against JSON Schema Draft 2020-12.

    Returns structural validation errors with best-effort line/column info.
    """
    raw = json.dumps(req.definition, indent=2)
    errors: list[SchemaValidationError] = []

    try:
        Draft202012Validator.check_schema(req.definition)
    except (ValidationError, JsSchemaError) as exc:
        path_copy = list(exc.path)
        path_parts = str(path_copy[0]) if path_copy else ""
        line, col = _find_json_location(raw, path_parts)
        errors.append(
            SchemaValidationError(
                line=line,
                column=col,
                path=".".join(str(p) for p in path_copy) if path_copy else "(root)",
                message=exc.message,
                schema_path=".".join(str(p) for p in exc.schema_path) if exc.schema_path else None,
            )
        )
        return SchemaValidateResponse(valid=False, errors=errors)

    return SchemaValidateResponse(valid=True, errors=[])


# ---------------------------------------------------------------------------
# Schema Import (from raw JSON Schema file content)
# ---------------------------------------------------------------------------


class SchemaImportRequest(BaseModel):
    content: str = Field(min_length=1, description="Raw JSON Schema text to import")


class SchemaImportField(BaseModel):
    name: str
    type: str
    description: str | None = None
    required: bool = False


class SchemaImportResponse(BaseModel):
    name: str | None = None
    description: str | None = None
    fields: list[SchemaImportField]


@router.post("/import")
@handle_db_errors("schemas.import_schema_endpoint")
async def import_schema_endpoint(
    req: SchemaImportRequest,
    _: TenantPrincipal = require_permission(_CODE_SCHEMA_LIST),
) -> SchemaImportResponse:
    """Parse raw JSON Schema content and extract fields for the schema builder.

    Returns the schema name (from ``title``), description (from ``description``),
    and each property as a ``SchemaImportField``.
    """
    try:
        schema = json.loads(req.content)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON: {exc}",
        ) from exc

    if not isinstance(schema, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JSON Schema must be a JSON object",
        )

    try:
        Draft202012Validator.check_schema(schema)
    except (ValidationError, JsSchemaError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid JSON Schema: {exc.message}",
        ) from exc

    name = schema.get("title")
    description = schema.get("description")
    properties = schema.get("properties", {})
    required_fields: list[str] = schema.get("required", [])

    fields = [
        SchemaImportField(
            name=field_name,
            type=field_schema.get("type", "string"),
            description=field_schema.get("description"),
            required=field_name in required_fields,
        )
        for field_name, field_schema in properties.items()
        if isinstance(field_schema, dict)
    ]

    return SchemaImportResponse(
        name=name,
        description=description,
        fields=fields,
    )


# TEST_MARKER - remove me

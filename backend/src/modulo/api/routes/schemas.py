"""Schema and SchemaVersion CRUD REST API."""

import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.connector_hub import ConnectorHub
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.schema_registry import (
    SchemaGenerationError,
    SchemaGenerationService,
    SchemaInferenceError,
    SchemaInferenceService,
    apply_migration,
    create_migration,
)
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
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/schemas", tags=["schemas"])


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
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime
    deprecated: bool = False
    deprecated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SchemaListResponse(BaseModel):
    items: list[SchemaResponse]
    total: int
    page: int
    page_size: int


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

    model_config = {"from_attributes": True}


class SchemaVersionListResponse(BaseModel):
    items: list[SchemaVersionResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Schema routes
# ---------------------------------------------------------------------------


@router.get("", response_model=SchemaListResponse)
async def list_schemas_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SchemaListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await list_schemas(session, page=page, page_size=page_size)
    except ProgrammingError:
        logger.exception("schemas.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Schema management is not available. Run database migrations to enable it.",
        ) from None
    return SchemaListResponse(
        items=[SchemaResponse.model_validate(s) for s in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=SchemaResponse, status_code=status.HTTP_201_CREATED)
async def create_schema_endpoint(
    body: SchemaCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SchemaResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        schema = await create_schema(
            session,
            org_id=principal.organisation_id,
            name=body.name,
            account_id=principal.account_id,
            description=body.description,
            abstract_name=body.abstract_name,
        )
    return SchemaResponse.model_validate(schema)


@router.get("/{schema_id}", response_model=SchemaResponse)
async def get_schema_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SchemaResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        schema = await get_schema(session, schema_id)
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")
    return SchemaResponse.model_validate(schema)


@router.patch("/{schema_id}", response_model=SchemaResponse)
async def update_schema_endpoint(
    schema_id: uuid.UUID,
    body: SchemaUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SchemaResponse:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        schema = await update_schema(session, schema_id, updates)
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")
    return SchemaResponse.model_validate(schema)


@router.patch("/{schema_id}/deprecate", response_model=SchemaResponse)
async def deprecate_schema_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SchemaResponse:
    """Mark a schema as deprecated."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        schema = await deprecate_schema(session, schema_id)
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")
    return SchemaResponse.model_validate(schema)


@router.delete("/{schema_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schema_endpoint(
    schema_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await delete_schema(session, schema_id)
    except SchemaDeletionProtectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")


# ---------------------------------------------------------------------------
# SchemaVersion routes (nested under schema)
# ---------------------------------------------------------------------------


@router.get("/{schema_id}/versions", response_model=SchemaVersionListResponse)
async def list_schema_versions_endpoint(
    schema_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SchemaVersionListResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        schema = await get_schema(session, schema_id)
        if schema is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")
        result = await list_schema_versions(session, schema_id, page=page, page_size=page_size)
    return SchemaVersionListResponse(
        items=[SchemaVersionResponse.model_validate(sv) for sv in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/{schema_id}/versions",
    response_model=SchemaVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_schema_version_endpoint(
    schema_id: uuid.UUID,
    body: SchemaVersionCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SchemaVersionResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        schema = await get_schema(session, schema_id)
        if schema is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")
        sv = await create_schema_version(
            session,
            org_id=principal.organisation_id,
            schema_id=schema_id,
            version=body.version,
            version_number=body.version_number,
            definition_json=body.definition_json,
            account_id=principal.account_id,
            published=body.published,
        )
    return SchemaVersionResponse.model_validate(sv)


@router.get("/{schema_id}/versions/{version}", response_model=SchemaVersionResponse)
async def get_schema_version_endpoint(
    schema_id: uuid.UUID,
    version: str,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SchemaVersionResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        sv = await get_schema_version(session, schema_id, version)
    if sv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema version not found")
    return SchemaVersionResponse.model_validate(sv)


# ---------------------------------------------------------------------------
# Schema Inference
# ---------------------------------------------------------------------------


class SchemaSampleQuery(BaseModel):
    resource: str = Field(min_length=1)
    filters: dict[str, Any] = {}
    limit: int = Field(default=10, ge=1, le=100)


class SchemaInferRequest(BaseModel):
    connector_instance_id: uuid.UUID
    sample_query: SchemaSampleQuery


class SchemaInferResponse(BaseModel):
    definition_json: dict[str, Any]
    sample_count: int
    suggestion_name: str
    suggestion_description: str | None = None


@router.post("/infer", response_model=SchemaInferResponse)
async def infer_schema_endpoint(
    body: SchemaInferRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> SchemaInferResponse:
    """Sample data from a connector and infer a JSON Schema via LLM.

    The returned *definition_json* is a draft for the user to review and
    save via the standard POST /api/v1/schemas endpoint.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)

            ci = await get_connector_instance(session, body.connector_instance_id)
            if ci is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Connector instance not found",
                )

            mbs = await list_model_backends(session, page_size=1)
            if not mbs.items:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No model backends configured; cannot perform inference",
                )
    except ProgrammingError:
        logger.exception("schemas.infer.table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Schema inference is not available. Run database migrations to enable it.",
        ) from None

    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key)

    async with ConnectorHub(secrets_backend=secrets_backend) as ch:
        await ch.initialise([ci])
        try:
            records = await ch.sample(
                connector_id=body.connector_instance_id,
                resource=body.sample_query.resource,
                filters=body.sample_query.filters,
                limit=body.sample_query.limit,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to sample connector: {exc}",
            ) from exc

    async with ModelBackendHub() as mh:
        await mh.initialise(mbs.items, secrets_backend=secrets_backend)
        first_backend_id = next(iter(mh.backend_ids))
        backend = await mh.get(first_backend_id)

        service = SchemaInferenceService(backend)
        try:
            definition_json = await service.infer(records)
        except SchemaInferenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Schema inference failed: {exc}",
            ) from exc

    suggestion_name = f"Inferred from {ci.name}"
    suggestion_description = (
        f"Auto-inferred schema from {ci.name} ({body.sample_query.resource}, {len(records)} samples)"
    )

    return SchemaInferResponse(
        definition_json=definition_json,
        sample_count=len(records),
        suggestion_name=suggestion_name,
        suggestion_description=suggestion_description,
    )


# ---------------------------------------------------------------------------
# Schema Generation (AI-assisted from description + examples)
# ---------------------------------------------------------------------------


class SchemaGenerateRequest(BaseModel):
    description: str = Field(min_length=1)
    examples: list[dict[str, Any]] = []


class SchemaGenerateResponse(BaseModel):
    definition_json: dict[str, Any]


@router.post("/generate", response_model=SchemaGenerateResponse)
async def generate_schema_endpoint(
    body: SchemaGenerateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> SchemaGenerateResponse:
    """Generate a JSON Schema from a natural language description and optional
    example records via an LLM.

    The returned *definition_json* is a draft for the user to review and
    save via the standard POST /api/v1/schemas endpoint.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        mbs = await list_model_backends(session, page_size=1)
        if not mbs.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No model backends configured; cannot generate schema",
            )

    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key)

    async with ModelBackendHub() as mh:
        await mh.initialise(mbs.items, secrets_backend=secrets_backend)
        first_backend_id = next(iter(mh.backend_ids))
        backend = await mh.get(first_backend_id)

        service = SchemaGenerationService(backend)
        try:
            definition_json = await service.generate(
                description=body.description,
                examples=body.examples or None,
            )
        except SchemaGenerationError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Schema generation failed: {exc}",
            ) from exc

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


@router.post("/migrate", response_model=SchemaMigrationResponse)
async def migrate_data_endpoint(
    body: SchemaMigrationRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    dry_run: bool = Query(False, description="If true, preview the migration plan without applying it"),
) -> SchemaMigrationResponse:
    """Migrate data from one schema version to another.

    Accepts *from_schema_id* and *to_schema_id* (Schema UUIDs),
    fetches the latest version of each, computes a migration plan,
    and applies it to *data*.

    Pass ``dry_run=true`` to preview the migration plan without
    applying any transformations.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        from_schema = await get_schema(session, body.from_schema_id)
        if from_schema is None:
            raise HTTPException(status_code=404, detail="Source schema not found")
        from_sv = await _get_latest_version(session, body.from_schema_id)
        if from_sv is None:
            raise HTTPException(status_code=404, detail="Source schema has no versions")

        to_schema = await get_schema(session, body.to_schema_id)
        if to_schema is None:
            raise HTTPException(status_code=404, detail="Target schema not found")
        to_sv = await _get_latest_version(session, body.to_schema_id)
        if to_sv is None:
            raise HTTPException(status_code=404, detail="Target schema has no versions")

    plan = create_migration(from_sv.definition_json, to_sv.definition_json)
    plan_dict = {
        "field_additions": plan.field_additions,
        "field_removals": plan.field_removals,
        "type_changes": {k: {"old_type": v.old_type, "new_type": v.new_type} for k, v in plan.type_changes.items()},
        "renames": plan.renames,
    }

    if dry_run:
        plan_dict["dry_run"] = True
        return SchemaMigrationResponse(
            migrated_data=deepcopy(body.data),
            plan=plan_dict,
        )

    migrated = apply_migration(body.data, plan)

    return SchemaMigrationResponse(
        migrated_data=migrated,
        plan=plan_dict,
    )


@router.post("/migrate/plan", response_model=dict[str, Any])
async def migration_plan_endpoint(
    body: SchemaMigrationPlanRequest,
) -> dict[str, Any]:
    """Preview a migration plan between two schemas without applying it."""
    plan = create_migration(body.from_definition, body.to_definition)
    return {
        "field_additions": plan.field_additions,
        "field_removals": plan.field_removals,
        "type_changes": {k: {"old_type": v.old_type, "new_type": v.new_type} for k, v in plan.type_changes.items()},
        "renames": plan.renames,
    }


async def _get_latest_version(session: AsyncSession, schema_id: uuid.UUID) -> Any:
    """Fetch the latest SchemaVersion for a given schema_id."""
    versions = await list_schema_versions(session, schema_id, page=1, page_size=1)
    return versions.items[0] if versions.items else None


# ---------------------------------------------------------------------------
# Schema Validation
# ---------------------------------------------------------------------------


class SchemaValidateRequest(BaseModel):
    definition: dict[str, Any] = Field(alias="definition")


class SchemaValidationError(BaseModel):
    line: int | None = None
    column: int | None = None
    path: str
    message: str
    schema_path: str | None = None


class SchemaValidateResponse(BaseModel):
    valid: bool
    errors: list[SchemaValidationError]


def _find_json_location(raw: str, instance: dict[str, Any], error_path: str) -> tuple[int | None, int | None]:
    """Best-effort line/column lookup for a validation error path in raw JSON text."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, None

    parts = error_path.strip("/").split("/") if error_path else []
    target = parsed
    for part in parts:
        if isinstance(target, dict):
            target = target.get(part, {})
        elif isinstance(target, list):
            try:
                target = target[int(part)]
            except (ValueError, IndexError):
                return None, None
        else:
            return None, None

    # Seek the key in raw text
    if not parts:
        return None, None

    key_to_find = parts[-1]
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        if f'"{key_to_find}"' in line:
            return i + 1, line.index(f'"{key_to_find}"') + 1
    return None, None


@router.post("/validate", response_model=SchemaValidateResponse)
async def validate_schema_endpoint(
    body: SchemaValidateRequest,
) -> SchemaValidateResponse:
    """Validate a JSON Schema against JSON Schema Draft 2020-12.

    Returns structural validation errors with best-effort line/column info.
    """
    raw = json.dumps(body.definition, indent=2)
    errors: list[SchemaValidationError] = []

    try:
        Draft202012Validator.check_schema(body.definition)
    except ValidationError as exc:
        path_parts = str(exc.path.popleft()) if exc.path else ""
        line, col = _find_json_location(raw, body.definition, path_parts)
        errors.append(
            SchemaValidationError(
                line=line,
                column=col,
                path=".".join(str(p) for p in exc.path) if exc.path else "(root)",
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


@router.post("/import", response_model=SchemaImportResponse)
async def import_schema_endpoint(
    body: SchemaImportRequest,
) -> SchemaImportResponse:
    """Parse raw JSON Schema content and extract fields for the schema builder.

    Returns the schema name (from ``title``), description (from ``description``),
    and each property as a ``SchemaImportField``.
    """
    try:
        schema = json.loads(body.content)
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
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid JSON Schema: {exc.message}",
        ) from exc

    name = schema.get("title")
    description = schema.get("description")
    properties = schema.get("properties", {})
    required_fields: list[str] = schema.get("required", [])

    fields: list[SchemaImportField] = []
    for field_name, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            continue
        field_type = field_schema.get("type", "string")
        field_desc = field_schema.get("description")
        fields.append(
            SchemaImportField(
                name=field_name,
                type=field_type,
                description=field_desc,
                required=field_name in required_fields,
            )
        )

    return SchemaImportResponse(
        name=name,
        description=description,
        fields=fields,
    )

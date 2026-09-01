"""ModelBackend CRUD REST API.

Credentials (API keys) are encrypted at rest with Fernet. The ciphertext is
never exposed in any response — only a boolean `has_credentials` field.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_in_dev_operator, require_permission
from modulo.api.models.team_visibility import TeamVisibilityMixin
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.secret_storage import decode_stored_secret_scoped
from modulo.core.audit_logger import append_audit_event_isolated
from modulo.core.model_backend_hub import _build_backend
from modulo.core.plugin_registry import get_plugin_registry
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.model_backend import (
    create_model_backend,
    delete_model_backend,
    get_model_backend,
    list_backends_referencing_fallback,
    list_model_backends,
    list_pipeline_references_for_backend,
    update_model_backend,
)
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.model_backends.base import HEALTH_CHECK_TIMEOUT
from modulo.settings import Settings, get_settings
from modulo.util import sanitise_log_value as _sanitise_log_value

_CODE_MODEL_BACKENDS_LIST_MODEL = "model_backends.list_model_backends_endpoint"
_MSG_MODEL_BACKENDS_NOT_AVAILABLE = "Model backends are not available. Run database migrations to enable this feature."
_CODE_MODEL_BACKENDS_CREATE_MODEL = "model_backends.create_model_backend_endpoint"
_CODE_MODEL_BACKENDS_GET_MODEL = "model_backends.get_model_backend_endpoint"
_MSG_MODEL_BACKEND_NOT_FOUND = "Model backend not found"
_CODE_MODEL_BACKENDS_UPDATE_MODEL = "model_backends.update_model_backend_endpoint"
_CODE_MODEL_BACKENDS_DELETE_MODEL = "model_backends.delete_model_backend_endpoint"
_CODE_MODEL_BACKENDS_RECHECK_MODEL = "model_backends.recheck_model_backend_health_endpoint"
_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED = "model_backends.audit_append_failed"
_PERM_MODEL_BACKEND_LIST = "model_backend.list"
_CODE_MODEL_BACKENDS_PIPELINE_REFS = "model_backends.pipeline_references_endpoint"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/model-backends", tags=["model-backends"])

HealthCheckStatus = Literal["ok", "unhealthy", "not_applicable"]


def _encrypt(api_key: str, fernet_key: str) -> bytes:
    return Fernet(fernet_key.encode()).encrypt(api_key.encode())


async def _run_health_check_on_save(
    provider: str,
    model_id: str,
    api_key: str | None,
    default_params: dict[str, Any],
) -> tuple[HealthCheckStatus, str | None]:
    """Best-effort test-inference health check run on save (PRD §8.1 ``health_check``).

    Builds the configured backend and runs its health check — a ``GET /models``
    ping for OpenAI-compatible providers, an inference call for the rest — so
    auth failures and quota errors are surfaced before pipelines reference the
    backend. The result is recorded on the entity's ``last_health_check_at`` /
    ``last_health_check_error`` columns, which the graph validator surfaces as
    ``MODEL_BACKEND_UNHEALTHY`` at save/run time.

    Returns ``(status, detail)`` where *detail* is ``None`` on success:

    * ``"ok"`` — the provider responded; the credentials are valid.
    * ``"unhealthy"`` — the provider responded with a failure (auth, quota,
      network, timeout); *detail* carries the provider error.
    * ``"not_applicable"`` — the provider cannot be built from the API-supplied
      credentials alone (e.g. Bedrock needs aws keys, vertexai needs ``project``,
      watsonx needs ``project_id``, azure_openai needs ``azure_endpoint``). This
      is a configuration limitation, NOT a health failure — the caller must not
      record an error, or the graph validator would hard-block every run.

    Never raises and never blocks the create/update: the check is best-effort
    (a transient provider outage must not prevent configuring a backend).
    """
    try:
        creds: dict[str, Any] = {"api_key": api_key} if api_key else {}
        backend = _build_backend(provider, model_id, creds, default_params)
    except HTTPException:
        raise
    except Exception:
        # Provider cannot be constructed from API-supplied credentials — not a
        # health failure. Never persisted as last_health_check_error (the graph
        # validator would surface it as MODEL_BACKEND_UNHEALTHY on every run).
        return "not_applicable", None
    try:
        result = await asyncio.wait_for(backend.health_check(), timeout=HEALTH_CHECK_TIMEOUT)
        if result.ok:
            return "ok", None
        return "unhealthy", result.detail
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        return "unhealthy", str(exc)[:500]


async def _persist_health_check_result(
    session: AsyncSession,
    backend_id: uuid.UUID,
    status_: HealthCheckStatus,
    detail: str | None,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    org_role: str,
) -> None:
    """Persist a health-check result on the entity in a short transaction.

    Called AFTER the request's write transaction has committed, so the provider
    network call that produced the result never held the DB connection or a row
    lock (the check runs with ``HEALTH_CHECK_TIMEOUT`` seconds of budget).
    ``not_applicable`` records no error (and clears any stale one) — a provider
    the API cannot construct must never block runs as ``MODEL_BACKEND_UNHEALTHY``.
    """
    checked_at = datetime.now(UTC)
    async with session.begin():
        await set_rls_org(session, org_id)
        await set_rls_user_context(session, user_id, org_role)
        row = await session.get(ModelBackend, backend_id)
        if row is None:
            return
        row.last_health_check_at = checked_at
        row.last_health_check_error = None if status_ != "unhealthy" else detail


async def _run_health_check_on_save_and_persist(
    session: AsyncSession,
    backend: ModelBackend,
    provider: str,
    model_id: str,
    api_key: str | None,
    default_params: dict[str, Any],
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    org_role: str,
) -> tuple[HealthCheckStatus, str | None]:
    """Run the save-time health check outside the write transaction and persist it.

    Best-effort end to end: the entity write has already committed before this
    runs, so a check or persistence failure must not fail the request — the
    result is logged, not propagated.
    """
    status_, detail = await _run_health_check_on_save(provider, model_id, api_key, default_params)
    try:
        await _persist_health_check_result(
            session,
            backend.id,
            status_,
            detail,
            org_id=org_id,
            user_id=user_id,
            org_role=org_role,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to persist health check result for model backend %s", backend.id)
    return status_, detail


def _audit_safe_backend_fields(updates: dict[str, Any]) -> dict[str, Any]:
    """Return the non-credential update fields, UUID values stringified, for audit payloads."""
    safe: dict[str, Any] = {}
    for key, value in updates.items():
        if key == "credentials_ciphertext":
            continue
        if isinstance(value, uuid.UUID):
            safe[key] = str(value)
        else:
            safe[key] = value
    return safe


async def _list_snapshots_referencing_backend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    backend_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Return ``{snapshot_id, pipeline_id}`` for every org snapshot whose
    ``model_backend_pins_json`` references ``backend_id`` and that is tied to a
    non-terminal run.

    PRD §8.1 deletion protection — a backend referenced by a PipelineSnapshot
    associated with a non-terminal run cannot be hard-deleted (the run may
    still need the pinned backend, e.g. after a HITL pause/resume). Snapshots
    referenced only by terminal runs impose no such constraint. Pins are a JSON
    column with no relational FK, so the org's snapshots are scanned in Python
    for a ``model_backend_id`` match, mirroring ``list_backends_referencing_fallback``.
    """
    target = str(backend_id)
    stmt = (
        select(PipelineSnapshot)
        .join(Run, Run.snapshot_id == PipelineSnapshot.id)
        .where(
            PipelineSnapshot.organisation_id == org_id,
            Run.status.not_in(TERMINAL_STATUSES),
        )
    )
    matches: list[dict[str, Any]] = []
    rows = (await session.execute(stmt)).scalars()
    for snap in rows:
        pins = snap.model_backend_pins_json or []
        if any(str(pin.get("model_backend_id")) == target for pin in pins):
            matches.append({"snapshot_id": snap.id, "pipeline_id": snap.pipeline_id})
    return matches


class ModelBackendCreate(TeamVisibilityMixin):
    name: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    provider: str = Field(..., min_length=1, max_length=128)
    model_id: str = Field(..., min_length=1, max_length=128)
    api_key: str = Field(..., min_length=1)
    default_params: ClassVar[dict[str, Any]] = {}
    visibility: str = Field(default="org")
    owner_team_id: uuid.UUID | None = None
    fallback_backend_ids: list[uuid.UUID] | None = None
    tier: Literal["native", "preview", "in_dev"] = Field(default="native")


class ModelBackendUpdate(TeamVisibilityMixin):
    name: str | None = Field(None, min_length=1, max_length=255)
    display_name: str | None = Field(None, min_length=1, max_length=255)
    model_id: str | None = Field(None, min_length=1, max_length=128)
    api_key: str | None = Field(None, min_length=1)
    default_params: dict[str, Any] | None = None
    visibility: str | None = None
    owner_team_id: uuid.UUID | None = None
    fallback_backend_ids: list[uuid.UUID] | None = None
    tier: Literal["native", "preview", "in_dev"] | None = None


class ModelBackendResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    display_name: str
    provider: str
    model_id: str
    has_credentials: bool
    default_params: dict[str, Any]
    visibility: str
    owner_team_id: uuid.UUID | None = None
    tier: str
    fallback_backend_ids: list[uuid.UUID] | None = None
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ModelBackendListResponse(BaseModel):
    items: list[ModelBackendResponse]
    total: int
    page: int
    page_size: int


class ModelBackendHealthCheckResponse(BaseModel):
    status: Literal["healthy", "unhealthy", "not_applicable"]
    detail: str | None = None
    checked_at: datetime | None = None


class PipelineReference(BaseModel):
    pipeline_id: uuid.UUID
    pipeline_name: str
    agent_name: str | None = None
    agent_id: uuid.UUID | None = None
    reference_type: Literal["direct_node", "agent"]


class PipelineReferenceListResponse(BaseModel):
    items: list[PipelineReference]
    total: int
    page: int
    page_size: int


def _to_response(mb: Any) -> ModelBackendResponse:
    raw_fallback_ids = getattr(mb, "fallback_backend_ids", None)
    fallback_ids: list[uuid.UUID] | None = None
    if raw_fallback_ids is not None:
        fallback_ids = [uuid.UUID(fid) if isinstance(fid, str) else fid for fid in raw_fallback_ids]
    return ModelBackendResponse(
        id=mb.id,
        organisation_id=mb.organisation_id,
        name=mb.name,
        display_name=mb.display_name,
        provider=mb.provider,
        model_id=mb.model_id,
        has_credentials=bool(mb.credentials_ciphertext),
        default_params=mb.default_params,
        visibility=mb.visibility,
        owner_team_id=mb.owner_team_id,
        tier=mb.tier,
        fallback_backend_ids=fallback_ids,
        created_by=mb.account_id,
        created_at=mb.created_at,
        updated_at=mb.updated_at,
    )


@router.get("", responses={401: {"description": "Unauthorized"}})
@handle_db_errors(_CODE_MODEL_BACKENDS_LIST_MODEL)
async def list_model_backends_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_in_dev: bool = Query(default=False, description="Include in_dev tier items (default excludes them)"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_MODEL_BACKEND_LIST),
) -> ModelBackendListResponse:
    if include_in_dev:
        require_in_dev_operator(principal, "model_backend.list.in_dev")
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_model_backends(
                session,
                org_id=principal.organisation_id,
                page=page,
                page_size=page_size,
                excluded_tiers=[] if include_in_dev else None,
            )
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_LIST_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_LIST_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_MODEL_BACKENDS_LIST_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing model backends.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error listing model backends")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing model backends.",
        ) from None
    return ModelBackendListResponse(
        items=[_to_response(mb) for mb in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


_VALID_PROVIDERS = {
    "ai21",
    "anthropic",
    "azure_openai",
    "bedrock",
    "cohere",
    "custom",
    "deepseek",
    "fireworks",
    "gemini",
    "grok",
    "groq",
    "jan",
    "llamacpp",
    "lm_studio",
    "localai",
    "mistral",
    "ollama",
    "opencode",
    "openai",
    "openrouter",
    "perplexity",
    "qwen",
    "replicate",
    "tgi",
    "togetherai",
    "vertexai",
    "vllm",
    "watsonx",
}


async def _validate_fallback_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    fallback_ids: list[uuid.UUID],
    *,
    backend_id: uuid.UUID | None = None,
) -> None:
    """Reject fallback backend IDs that reference no existing org backend (422).

    The ``fallback_backend_ids`` JSON column has no relational FK (PRD 8.1), so
    the API is the enforcement point: a fallback chain pointing at a backend
    that does not exist (or belongs to another org) is a configuration error,
    not a runtime-failover concern. The hub already skips unregistered IDs
    gracefully, but a silent skip hides a misconfiguration — fail fast instead.

    On update, a self-reference (the backend listing itself as its own
    fallback) is also rejected: it is a meaningless failover chain and would
    permanently block deletion (the delete-protection scan reports the backend
    as referencing itself).
    """
    if not fallback_ids:
        return
    unique_ids = list(dict.fromkeys(fallback_ids))
    if backend_id is not None and backend_id in unique_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {
                    "type": "value_error",
                    "loc": ["body", "fallback_backend_ids"],
                    "msg": "A model backend cannot reference itself as a fallback",
                }
            ],
        )
    found = set(
        (
            await session.execute(
                select(ModelBackend.id).where(
                    ModelBackend.organisation_id == org_id,
                    ModelBackend.id.in_(unique_ids),
                )
            )
        ).scalars()
    )
    missing = [fid for fid in unique_ids if fid not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {
                    "type": "value_error",
                    "loc": ["body", "fallback_backend_ids"],
                    "msg": "Unknown model backend id(s) referenced as fallbacks: "
                    + ", ".join(str(fid) for fid in missing),
                }
            ],
        )


def _validate_provider(provider: str) -> None:
    """Raise 422 if provider is not a known built-in or plugin backend."""
    if provider in _VALID_PROVIDERS:
        return
    try:
        registry = get_plugin_registry()
        if registry.has_model_backend(provider):
            return
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("model_backends._validate_provider")
        logger.warning("Plugin registry check failed for provider %r: %s", _sanitise_log_value(provider), exc)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[
            {
                "type": "value_error",
                "loc": ["body", "provider"],
                "msg": f"Unknown model backend provider: {provider!r}",
            }
        ],
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors(_CODE_MODEL_BACKENDS_CREATE_MODEL)
async def create_model_backend_endpoint(
    req: ModelBackendCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("model_backend.create"),
    settings: Settings = Depends(get_settings),
) -> ModelBackendResponse:
    _validate_provider(req.provider)
    ciphertext = _encrypt(req.api_key, settings.fernet_key)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            if req.fallback_backend_ids:
                await _validate_fallback_ids(session, principal.organisation_id, req.fallback_backend_ids)

            existing = (
                await session.execute(
                    select(ModelBackend)
                    .where(
                        ModelBackend.organisation_id == principal.organisation_id,
                        ModelBackend.name == req.name,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A model backend with name {req.name!r} already exists in this organisation",
                )

            fallback_ids: list[str] | None = None
            if req.fallback_backend_ids:
                fallback_ids = [str(fid) for fid in req.fallback_backend_ids]
            mb = await create_model_backend(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                display_name=req.display_name,
                provider=req.provider,
                model_id=req.model_id,
                credentials_ciphertext=ciphertext,
                account_id=principal.account_id,
                default_params=req.default_params,
                visibility=req.visibility,
                owner_team_id=req.owner_team_id,
                fallback_backend_ids=fallback_ids,
                tier=req.tier,
            )

            secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
            secret_value = json.dumps({"api_key": req.api_key})
            await secrets_backend.set_secret(str(mb.id), secret_value)
            response = _to_response(mb)
        # The entity write has COMMITTED above. The PRD 8.1 health check runs
        # OUTSIDE the write transaction so the provider network call (up to
        # HEALTH_CHECK_TIMEOUT=10s) never holds the DB connection or a row
        # lock; the result is persisted in a short second transaction.
        await _run_health_check_on_save_and_persist(
            session,
            mb,
            req.provider,
            req.model_id,
            req.api_key,
            dict(req.default_params or {}),
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            org_role=principal.org_role,
        )

        # PRD §8.12 audit trail: backend registration was previously invisible.
        # Written in a fresh transaction (the create above already committed)
        # and failure-isolated so a broken append never fails a completed create.
        await append_audit_event_isolated(
            session,
            principal,
            resource_type="model_backend",
            event_type="model_backend.created",
            resource_id=mb.id,
            payload={
                "name": mb.name,
                "provider": mb.provider,
                "model_id": mb.model_id,
                "tier": mb.tier,
                "fallback_backend_ids": [str(fid) for fid in (mb.fallback_backend_ids or [])],
                "has_credentials": bool(mb.credentials_ciphertext),
            },
            log_key=_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED,
        )
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_CREATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_CREATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_MODEL_BACKENDS_CREATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while creating model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error creating model backend")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating model backend.",
        ) from None
    return response


@router.get("/{backend_id}")
@handle_db_errors(_CODE_MODEL_BACKENDS_GET_MODEL)
async def get_model_backend_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_MODEL_BACKEND_LIST),
) -> ModelBackendResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await get_model_backend(session, backend_id)
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_GET_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_GET_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_MODEL_BACKENDS_GET_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error fetching model backend")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching model backend.",
        ) from None
    if mb is None or mb.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MODEL_BACKEND_NOT_FOUND)
    return _to_response(mb)


@router.get("/{backend_id}/pipeline-references")
@handle_db_errors(_CODE_MODEL_BACKENDS_PIPELINE_REFS)
async def list_pipeline_references_endpoint(
    backend_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_MODEL_BACKEND_LIST),
) -> PipelineReferenceListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await get_model_backend(session, backend_id)
            if mb is None or mb.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MODEL_BACKEND_NOT_FOUND)
            result = await list_pipeline_references_for_backend(
                session,
                org_id=principal.organisation_id,
                backend_id=backend_id,
                page=page,
                page_size=page_size,
            )
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_PIPELINE_REFS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_PIPELINE_REFS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_MODEL_BACKENDS_PIPELINE_REFS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing pipeline references.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error listing pipeline references")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing pipeline references.",
        ) from None
    return PipelineReferenceListResponse(
        items=[PipelineReference(**ref) for ref in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.patch("/{backend_id}", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors(_CODE_MODEL_BACKENDS_UPDATE_MODEL)
async def update_model_backend_endpoint(
    backend_id: uuid.UUID,
    req: ModelBackendUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("model_backend.update"),
    settings: Settings = Depends(get_settings),
) -> ModelBackendResponse:
    updates: dict[str, Any] = req.model_dump(exclude_unset=True)
    if "api_key" in updates and updates["api_key"] is not None:
        ct = _encrypt(updates.pop("api_key"), settings.fernet_key)
        updates["credentials_ciphertext"] = ct  # nosemgrep: credential-not-in-state
    elif "api_key" in updates:
        updates.pop("api_key")
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            fallback_ids = updates.get("fallback_backend_ids")
            if fallback_ids is not None:
                await _validate_fallback_ids(session, principal.organisation_id, fallback_ids, backend_id=backend_id)
                # JSON column cannot serialize raw uuid.UUID objects; stringify
                # before the write, mirroring the create path (line ~315).
                updates["fallback_backend_ids"] = [str(fid) for fid in fallback_ids]
            existing = await get_model_backend(session, backend_id)
            if existing is None or existing.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            mb = await update_model_backend(session, backend_id, updates)
            if mb is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MODEL_BACKEND_NOT_FOUND)
            await session.refresh(mb)
            if req.api_key is not None:
                secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
                secret_value = json.dumps({"api_key": req.api_key})
                await secrets_backend.set_secret(str(mb.id), secret_value)
            response = _to_response(mb)
        # The entity write has COMMITTED above. A credential change re-runs the
        # PRD 8.1 health check OUTSIDE the write transaction (post-rotation
        # validation) so the provider network call never holds the DB connection
        # or row lock; the result is persisted in a short second transaction.
        if req.api_key is not None:
            await _run_health_check_on_save_and_persist(
                session,
                mb,
                mb.provider,
                mb.model_id,
                req.api_key,
                dict(mb.default_params or {}),
                org_id=principal.organisation_id,
                user_id=principal.account_id,
                org_role=principal.org_role,
            )

        # PRD §8.12 audit trail: backend edits and credential rotation were
        # previously invisible. Written in fresh transactions (the update above
        # already committed) and failure-isolated so a broken append never fails
        # a completed update. ``model_backend_credentials_updated`` fires under
        # its exact PRD name when an API key is supplied; the generic edit event
        # carries only the non-credential fields that actually changed.
        changed_fields = _audit_safe_backend_fields(updates)
        if changed_fields:
            await append_audit_event_isolated(
                session,
                principal,
                resource_type="model_backend",
                event_type="model_backend.updated",
                resource_id=mb.id,
                payload={"backend_id": str(mb.id), "changed_fields": changed_fields},
                log_key=_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED,
            )
        if req.api_key is not None:
            await append_audit_event_isolated(
                session,
                principal,
                resource_type="model_backend",
                event_type="model_backend_credentials_updated",
                resource_id=mb.id,
                payload={
                    "backend_id": str(mb.id),
                    "name": mb.name,
                    "provider": mb.provider,
                    "model_id": mb.model_id,
                },
                log_key=_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED,
            )
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_UPDATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_UPDATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_MODEL_BACKENDS_UPDATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error updating model backend")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating model backend.",
        ) from None
    return response


@router.post("/{backend_id}/health-check")
@handle_db_errors(_CODE_MODEL_BACKENDS_RECHECK_MODEL)
async def recheck_model_backend_health_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("model_backend.update"),
    settings: Settings = Depends(get_settings),
) -> ModelBackendHealthCheckResponse:
    """Re-run the health check on demand and persist the result (PRD §8.1).

    Operators use this to re-validate a backend after a transient save-time
    outage — the only alternative before this route was PATCHing a new API key,
    which made a sticky ``last_health_check_error`` un-clearable without rotation.
    The stored credential is decrypted and re-pinged against the provider; the
    result is persisted in a short transaction after the read transaction
    commits, so the network call never holds a DB connection or row lock.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await get_model_backend(session, backend_id)
            if mb is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")
            try:
                api_key = await decode_stored_secret_scoped(
                    session, mb.credentials_ciphertext, settings.fernet_key, org_id=principal.organisation_id
                )
            except Exception:
                logger.warning("Failed to decrypt credentials for model backend %s; health check skipped", backend_id)
                api_key = None
        status_, detail = await _run_health_check_on_save_and_persist(
            session,
            mb,
            mb.provider,
            mb.model_id,
            api_key,
            dict(mb.default_params or {}),
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            org_role=principal.org_role,
        )
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_RECHECK_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_RECHECK_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_MODEL_BACKENDS_RECHECK_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while re-checking model backend health.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error re-checking model backend health")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while re-checking model backend health.",
        ) from None
    label = {"ok": "healthy", "unhealthy": "unhealthy", "not_applicable": "not_applicable"}[status_]
    return ModelBackendHealthCheckResponse(status=label, detail=detail, checked_at=datetime.now(UTC))


@router.delete("/{backend_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors(_CODE_MODEL_BACKENDS_DELETE_MODEL)
async def delete_model_backend_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("model_backend.delete"),
) -> None:
    audit_payload: dict[str, Any] | None = None
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            referencing = await list_backends_referencing_fallback(
                session, org_id=principal.organisation_id, backend_id=backend_id
            )
            if referencing:
                names = ", ".join(sorted(mb.name for mb in referencing))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(f"Cannot delete model backend: it is referenced as a fallback by backend(s): {names}"),
                )
            # PRD §8.1 deletion protection: a backend pinned by a PipelineSnapshot
            # associated with a non-terminal run cannot be hard-deleted — the
            # in-flight run may still resolve the pinned backend (e.g. after a
            # HITL pause/resume). Terminal-run snapshots impose no constraint.
            non_terminal = await _list_snapshots_referencing_backend(
                session, org_id=principal.organisation_id, backend_id=backend_id
            )
            if non_terminal:
                snapshot_ids = ", ".join(sorted(str(r["snapshot_id"]) for r in non_terminal))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot delete model backend: it is pinned by a PipelineSnapshot associated with a "
                        f"non-terminal run (snapshot(s): {snapshot_ids}). Wait for the runs to finish before "
                        "hard-deleting the backend."
                    ),
                )
            # Capture the entity details BEFORE the delete so the audit event can
            # survive the row (a post-delete read would return nothing).
            existing = await get_model_backend(session, backend_id)
            if existing is None or existing.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if existing is not None:
                audit_payload = {
                    "name": existing.name,
                    "provider": existing.provider,
                    "model_id": existing.model_id,
                    "tier": existing.tier,
                }
            deleted = await delete_model_backend(session, backend_id)
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_DELETE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_DELETE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_MODEL_BACKENDS_DELETE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while deleting model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error deleting model backend")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting model backend.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MODEL_BACKEND_NOT_FOUND)

    # PRD §8.12 audit trail: backend deletion was previously invisible. Written
    # in a fresh transaction (the delete above already committed) and
    # failure-isolated so a broken append never fails a completed delete.
    if audit_payload is not None:
        await append_audit_event_isolated(
            session,
            principal,
            resource_type="model_backend",
            event_type="model_backend.deleted",
            resource_id=backend_id,
            payload=audit_payload,
            log_key=_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED,
        )

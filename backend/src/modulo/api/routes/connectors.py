"""ConnectorInstance CRUD REST API.

Credentials are encrypted at rest with Fernet. The ciphertext is never exposed
in any response — only a boolean `has_credentials` field indicates presence.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from httpx import HTTPStatusError, RequestError
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_in_dev_operator, require_permission
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK, mask_config_json
from modulo.api.models.team_visibility import TeamVisibilityMixin
from modulo.auth.jwt import TenantPrincipal
from modulo.connectors.base import ConnectorType
from modulo.connectors.github import REQUIRED_FINE_GRAINED_PERMISSIONS as GITHUB_REQUIRED_FINE_GRAINED_PERMISSIONS
from modulo.connectors.github import REQUIRED_SCOPES as GITHUB_REQUIRED_SCOPES
from modulo.connectors.github import GitHubConnector, is_fine_grained_pat
from modulo.core.connector_hub import ConnectorDecryptError, ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.connector_instance import (
    create_connector_instance,
    delete_connector_instance,
    get_connector_instance,
    list_connector_instances,
    update_connector_instance,
)
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

_CODE_CONNECTORS_LIST_CONNECTORS_ENDPOINT = "connectors.list_connectors_endpoint"
_MSG_CONNECTORS_NOT_AVAILABLE_RUN = "Connectors are not available. Run database migrations to enable this feature."
_CODE_CONNECTORS_CREATE_CONNECTOR_ENDPOINT = "connectors.create_connector_endpoint"
_CODE_CONNECTORS_GET_CONNECTOR_ENDPOINT = "connectors.get_connector_endpoint"
_MSG_CONNECTOR_NOT_FOUND = "Connector not found"
_CODE_CONNECTORS_UPDATE_CONNECTOR_ENDPOINT = "connectors.update_connector_endpoint"
_CODE_CONNECTORS_DELETE_CONNECTOR_ENDPOINT = "connectors.delete_connector_endpoint"
_PERM_CONNECTOR_LIST = "connector.list"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])


def _github_missing_scope_detail(token: str, missing: set[str]) -> str:
    """Human-readable required-scope rejection detail, token-type aware.

    Classic PATs are checked against classic OAuth scopes (``repo``,
    ``read:org``); fine-grained PATs (``github_pat_`` prefix) are checked against
    the PRD §7.11 fine-grained permissions. Reporting the classic set for a
    fine-grained token would be wrong — GitHub never issues those scopes to it.
    """
    if is_fine_grained_pat(token):
        return (
            f"GitHub token is missing required fine-grained permissions: "
            f"{', '.join(sorted(missing))}. "
            f"Required: {', '.join(sorted(GITHUB_REQUIRED_FINE_GRAINED_PERMISSIONS))}"
        )
    return (
        f"GitHub token is missing required OAuth scopes: "
        f"{', '.join(sorted(missing))}. "
        f"Required: {', '.join(sorted(GITHUB_REQUIRED_SCOPES))}"
    )


def _encrypt(credentials: str, fernet_key: str) -> bytes:
    return Fernet(fernet_key.encode()).encrypt(credentials.encode())


class ConnectorCreate(TeamVisibilityMixin):
    name: str = Field(..., min_length=1, max_length=255)
    connector_type_id: str = Field(..., min_length=1, max_length=128)
    credentials: str = Field(..., min_length=1)
    config_json: dict[str, Any] = Field(default_factory=dict)
    allowed_operations: list[str] = Field(default_factory=list)
    visibility: str = Field(default="org")
    owner_team_id: uuid.UUID | None = None
    tier: Literal["native", "preview", "in_dev"] = Field(default="native")


class ConnectorUpdate(TeamVisibilityMixin):
    name: str | None = Field(None, min_length=1, max_length=255)
    credentials: str | None = Field(None, min_length=1)
    config_json: dict[str, Any] | None = None
    allowed_operations: list[str] | None = None
    visibility: str | None = None
    owner_team_id: uuid.UUID | None = None
    tier: Literal["native", "preview", "in_dev"] | None = None


class ConnectorResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    connector_type_id: str
    has_credentials: bool
    config_json: dict[str, Any]
    allowed_operations: list[str]
    status: str
    visibility: str
    owner_team_id: uuid.UUID | None = None
    tier: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ConnectorListResponse(BaseModel):
    items: list[ConnectorResponse]
    total: int
    page: int
    page_size: int
    next_cursor: str | None = None
    has_more: bool = False


class ConnectorTypeItem(BaseModel):
    id: str
    display_name: str


class ConnectorTypeListResponse(BaseModel):
    items: list[ConnectorTypeItem]


@router.get("/types")
async def list_connector_types() -> ConnectorTypeListResponse:
    items = [ConnectorTypeItem(id=t.value, display_name=t.value.replace("_", " ").title()) for t in ConnectorType]
    return ConnectorTypeListResponse(items=items)


def _to_response(ci: Any) -> ConnectorResponse:
    return ConnectorResponse(
        id=ci.id,
        organisation_id=ci.organisation_id,
        name=ci.name,
        connector_type_id=ci.connector_type_id,
        has_credentials=bool(ci.credentials_ciphertext),
        config_json=mask_config_json(ci.config_json),
        allowed_operations=ci.allowed_operations,
        status=ci.status,
        visibility=ci.visibility,
        owner_team_id=ci.owner_team_id,
        tier=ci.tier,
        created_at=ci.created_at,
        updated_at=ci.updated_at,
    )


@router.get("", responses={401: {"description": "Unauthorized"}})
@handle_db_errors(_CODE_CONNECTORS_LIST_CONNECTORS_ENDPOINT)
async def list_connectors_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    include_in_dev: bool = Query(default=False, description="Include in_dev tier items (default excludes them)"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_CONNECTOR_LIST),
) -> ConnectorListResponse:
    if include_in_dev:
        require_in_dev_operator(principal, "connector.list.in_dev")
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_connector_instances(
                session,
                page=page,
                page_size=page_size,
                cursor=cursor,
                excluded_tiers=[] if include_in_dev else None,
            )
    except IntegrityError:
        logger.exception(_CODE_CONNECTORS_LIST_CONNECTORS_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_CONNECTORS_LIST_CONNECTORS_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_CONNECTORS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_CONNECTORS_LIST_CONNECTORS_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing connectors.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error listing connectors")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing connectors.",
        ) from None
    return ConnectorListResponse(
        items=[_to_response(ci) for ci in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors(_CODE_CONNECTORS_CREATE_CONNECTOR_ENDPOINT)
async def create_connector_endpoint(
    req: ConnectorCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("connector.create"),
    settings: Settings = Depends(get_settings),
) -> ConnectorResponse:
    if req.connector_type_id == "github":
        temp = GitHubConnector(token=req.credentials)
        try:
            missing = await temp.verify_scopes()
        except (HTTPStatusError, RequestError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Cannot verify GitHub token — API call failed",
            ) from None
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=_github_missing_scope_detail(req.credentials, missing),
            )

    ciphertext = _encrypt(req.credentials, settings.fernet_key)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            ci = await create_connector_instance(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                connector_type_id=req.connector_type_id,
                account_id=principal.account_id,
                credentials_ciphertext=ciphertext,
                config_json=req.config_json,
                allowed_operations=req.allowed_operations,
                visibility=req.visibility,
                owner_team_id=req.owner_team_id,
                tier=req.tier,
            )
    except IntegrityError:
        logger.exception(_CODE_CONNECTORS_CREATE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Connector cannot be created — a constraint violation occurred "
                "(e.g. duplicate name or invalid reference)."
            ),
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_CONNECTORS_CREATE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_CONNECTORS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_CONNECTORS_CREATE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while creating connector.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error creating connector")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating connector.",
        ) from None
    return _to_response(ci)


@router.get("/{connector_id}")
@handle_db_errors(_CODE_CONNECTORS_GET_CONNECTOR_ENDPOINT)
async def get_connector_endpoint(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_CONNECTOR_LIST),
) -> ConnectorResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            ci = await get_connector_instance(session, connector_id)
    except IntegrityError:
        logger.exception(_CODE_CONNECTORS_GET_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_CONNECTORS_GET_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_CONNECTORS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_CONNECTORS_GET_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching connector.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error fetching connector")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching connector.",
        ) from None
    if ci is None or ci.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CONNECTOR_NOT_FOUND)
    return _to_response(ci)


class ConnectorHealthResponse(BaseModel):
    """Live connector health check result."""

    ok: bool
    detail: str = ""


@router.get("/{connector_id}/health")
@handle_db_errors("connectors.connector_health_endpoint")
async def connector_health_endpoint(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_CONNECTOR_LIST),
    settings: Settings = Depends(get_settings),
) -> ConnectorHealthResponse:
    """Run a live health check against a connector instance.

    Builds the connector from the stored config/credentials and runs its
    ``health_check``. A missing connector (or one outside the caller's org) is
    a 404. Build/decrypt failures are 502; a failing health check is reported
    in-band as ``ok: false`` with the connector's detail.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        ci = await get_connector_instance(session, connector_id)
    if ci is None or ci.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
    try:
        secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key)
        async with ConnectorHub(secrets_backend=secrets_backend) as hub:
            await hub.initialise([ci])
            connector = hub.get(connector_id)
            result = await connector.health_check()
    except ConnectorDecryptError:
        logger.exception("connectors.connector_health_endpoint")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to decrypt connector credentials.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error checking connector health")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to check connector health.",
        ) from None
    return ConnectorHealthResponse(ok=result.ok, detail=result.detail)


@router.patch("/{connector_id}", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors(_CODE_CONNECTORS_UPDATE_CONNECTOR_ENDPOINT)
async def update_connector_endpoint(
    connector_id: uuid.UUID,
    req: ConnectorUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("connector.update"),
    settings: Settings = Depends(get_settings),
) -> ConnectorResponse:
    updates: dict[str, Any] = req.model_dump(exclude_unset=True)
    credentials_updated = "credentials" in updates
    if credentials_updated:
        new_credentials = updates.pop("credentials")
        ct = _encrypt(new_credentials, settings.fernet_key)
        updates["credentials_ciphertext"] = ct  # nosemgrep: credential-not-in-state
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            existing = await get_connector_instance(session, connector_id)
            if existing is None or existing.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            if existing is not None and "config_json" in updates and updates["config_json"] is not None:
                current_cfg = existing.config_json or {}
                merged_cfg = dict(current_cfg)
                for k, v in updates["config_json"].items():
                    if isinstance(v, str) and v == SENSITIVE_VALUE_MASK:
                        # A masked placeholder must never clobber the stored secret
                        # (read-modify-write round-trip guard). Keep the existing value.
                        continue
                    if v is None:
                        merged_cfg.pop(k, None)
                    else:
                        merged_cfg[k] = v
                updates["config_json"] = merged_cfg
            if existing is not None and existing.connector_type_id == "github" and credentials_updated:
                temp = GitHubConnector(token=new_credentials)
                try:
                    missing = await temp.verify_scopes()
                except (HTTPStatusError, RequestError, ValueError):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="Cannot verify GitHub token — API call failed",
                    ) from None
                if missing:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=_github_missing_scope_detail(new_credentials, missing),
                    )
            ci = await update_connector_instance(session, connector_id, updates)
    except IntegrityError:
        logger.exception(_CODE_CONNECTORS_UPDATE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Connector cannot be updated — a constraint violation occurred "
                "(e.g. duplicate name or invalid reference)."
            ),
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_CONNECTORS_UPDATE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_CONNECTORS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_CONNECTORS_UPDATE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating connector.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error updating connector")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating connector.",
        ) from None
    if ci is None or ci.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CONNECTOR_NOT_FOUND)
    return _to_response(ci)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors(_CODE_CONNECTORS_DELETE_CONNECTOR_ENDPOINT)
async def delete_connector_endpoint(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("connector.delete"),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            existing = await get_connector_instance(session, connector_id)
            if existing is None or existing.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            deleted = await delete_connector_instance(session, connector_id)
    except IntegrityError:
        logger.exception(_CODE_CONNECTORS_DELETE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_CONNECTORS_DELETE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_CONNECTORS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_CONNECTORS_DELETE_CONNECTOR_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while deleting connector.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error deleting connector")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting connector.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CONNECTOR_NOT_FOUND)

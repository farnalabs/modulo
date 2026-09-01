import json
import logging
import uuid
from datetime import datetime
from typing import Any

import httpx
from defusedxml import ElementTree
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    deny_break_glass_mint,
    get_db_session,
    get_system_db_session,
    require_feature,
    require_permission,
)
from modulo.api.middleware.sensitive_mask import SensitiveValue
from modulo.auth.jwt import TenantPrincipal
from modulo.core.ssrf import pinned_async_client, validate_outbound_url_async
from modulo.db.crud.sso_provider import (
    create_provider,
    delete_provider,
    get_provider,
    list_providers,
    set_group_mappings,
    toggle_provider,
    update_provider,
)
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_CODE_SSO_MANAGE = "sso.manage"
_MSG_DATABASE_ERROR_PLEASE_TRY = "Database error. Please try again."
_MSG_SSO_PROVIDER_NOT_FOUND = "SSO provider not found"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/sso", tags=["admin-sso"])


class SsoProviderCreate(BaseModel):
    provider_type: str = Field(pattern=r"^(oidc|saml)$")
    name: str = Field(min_length=1, max_length=255)
    provider_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    discovery_url: str | None = None
    metadata_url: str | None = None
    metadata_xml: str | None = None
    entity_id: str | None = None
    scopes: list[str] | None = None
    enabled: bool = True
    auto_provision: bool = True
    default_role: str = Field(default="runner", pattern=r"^(operator|runner)$")


class SsoProviderUpdate(BaseModel):
    name: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    discovery_url: str | None = None
    metadata_url: str | None = None
    metadata_xml: str | None = None
    entity_id: str | None = None
    scopes: list[str] | None = None
    enabled: bool | None = None
    auto_provision: bool | None = None
    default_role: str | None = Field(default=None, pattern=r"^(operator|runner)$")


class SsoProviderResponse(BaseModel):
    id: uuid.UUID
    provider_type: str
    name: str
    provider_id: str | None = None
    client_id: str | None = None
    client_secret: SensitiveValue | None = None
    discovery_url: str | None = None
    metadata_url: str | None = None
    metadata_xml: str | None = None
    entity_id: str | None = None
    scopes: list[str] | None = None
    enabled: bool
    auto_provision: bool
    default_role: str
    created_at: datetime

    model_config = {"from_attributes": True}
    updated_at: datetime

    @field_validator("scopes", mode="before")
    @classmethod
    def _normalize_scopes(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return None
        if isinstance(value, list):
            if not value:
                return []
            if all(isinstance(scope, str) for scope in value):
                return value
        return None

    @field_validator("client_secret", mode="before")
    @classmethod
    def _normalize_client_secret(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str | bytes):
            return "configured" if value else ""
        raise ValueError("client_secret has an unsupported storage type")


class SsoProviderTestResult(BaseModel):
    success: bool
    message: str
    provider_info: dict[str, Any] | None = None


@router.get("/providers")
@handle_db_errors("admin.sso.get_providers")
async def get_providers(
    _: object = require_feature("sso"),
    current_user: TenantPrincipal = require_permission(_CODE_SSO_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> list[SsoProviderResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            providers = await list_providers(session, org_id=current_user.organisation_id)
        return [SsoProviderResponse.model_validate(p) for p in providers]
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SSO providers DB error on list: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in get_providers")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.post(
    "/providers",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("admin.sso.create_provider_endpoint")
async def create_provider_endpoint(
    req: SsoProviderCreate,
    _: object = require_feature("sso"),
    current_user: TenantPrincipal = require_permission(_CODE_SSO_MANAGE),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    system_session: AsyncSession = Depends(get_system_db_session),
) -> SsoProviderResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            provider = await create_provider(
                session,
                provider_type=req.provider_type,
                name=req.name,
                provider_id=req.provider_id,
                client_id=req.client_id,
                client_secret=req.client_secret,
                discovery_url=req.discovery_url,
                metadata_url=req.metadata_url,
                metadata_xml=req.metadata_xml,
                entity_id=req.entity_id,
                scopes=req.scopes,
                enabled=req.enabled,
                auto_provision=req.auto_provision,
                default_role=req.default_role,
                fernet_key=settings.fernet_key,
                org_id=current_user.organisation_id,
                actor_user_id=current_user.account_id,
                system_session=system_session,
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError as exc:
        _log.warning("SSO provider create conflict: %s", exc, exc_info=True)
        detail = str(exc).lower()
        if "provider_id" in detail or "uq_sso_providers_provider_id" in detail:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An SSO provider with this provider ID already exists.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An SSO provider with this name already exists."
        ) from exc
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on create: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SSO providers DB error on create: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in create_provider_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return SsoProviderResponse.model_validate(provider)


@router.put(
    "/providers/{provider_id}",
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("admin.sso.update_provider_endpoint")
async def update_provider_endpoint(
    provider_id: uuid.UUID,
    req: SsoProviderUpdate,
    _: object = require_feature("sso"),
    current_user: TenantPrincipal = require_permission(_CODE_SSO_MANAGE),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> SsoProviderResponse:
    updates = req.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            provider = await update_provider(
                session,
                provider_id,
                org_id=current_user.organisation_id,
                actor_user_id=current_user.account_id,
                fernet_key=settings.fernet_key,
                **updates,
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError as exc:
        _log.warning("SSO provider duplicate name on update: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An SSO provider with this name already exists.",
        ) from exc
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on update: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SSO providers DB error on update: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in update_provider_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_SSO_PROVIDER_NOT_FOUND,
        )
    return SsoProviderResponse.model_validate(provider)


@router.delete(
    "/providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("admin.sso.delete_provider_endpoint")
async def delete_provider_endpoint(
    provider_id: uuid.UUID,
    _: object = require_feature("sso"),
    current_user: TenantPrincipal = require_permission(_CODE_SSO_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            deleted = await delete_provider(
                session,
                provider_id,
                org_id=current_user.organisation_id,
                actor_user_id=current_user.account_id,
            )
    except IntegrityError:
        _log.exception("admin_sso.delete_provider_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on delete: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SSO providers DB error on delete: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in delete_provider_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_SSO_PROVIDER_NOT_FOUND,
        )


@router.post("/providers/{provider_id}/test")
@handle_db_errors("admin.sso.test_provider_connection")
async def test_provider_connection(
    provider_id: uuid.UUID,
    _: object = require_feature("sso"),
    current_user: TenantPrincipal = require_permission(_CODE_SSO_MANAGE),
    session: AsyncSession = Depends(get_db_session),
    _settings: Settings = Depends(get_settings),
) -> SsoProviderTestResult:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            provider = await get_provider(session, provider_id, org_id=current_user.organisation_id)
    except IntegrityError:
        _log.exception("admin_sso.test_provider_connection")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on test connection: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SSO providers DB error on test connection: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in test_provider_connection")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_SSO_PROVIDER_NOT_FOUND,
        )

    try:
        if provider.provider_type == "oidc":
            return await _test_oidc_connection(provider)
        return await _test_saml_connection(provider)
    except HTTPException:
        raise
    except Exception as exc:
        _log.warning("SSO test connection failed: %s", exc, exc_info=True)
        return SsoProviderTestResult(
            success=False,
            message=str(exc),
        )


async def _test_oidc_connection(provider: Any) -> SsoProviderTestResult:
    if not provider.discovery_url:
        return SsoProviderTestResult(
            success=False,
            message="Discovery URL is required for OIDC providers",
        )

    try:
        await validate_outbound_url_async(provider.discovery_url)
    except ValueError as exc:
        return SsoProviderTestResult(success=False, message=f"Rejected: {exc}")

    try:
        async with await pinned_async_client(provider.discovery_url) as client:
            resp = await client.get(provider.discovery_url, timeout=httpx.Timeout(10.0, connect=5.0))
            resp.raise_for_status()
            disc = resp.json()
    except (httpx.HTTPError, ValueError):
        _log.warning("admin_sso._test_oidc_connection", exc_info=True)
        return SsoProviderTestResult(
            success=False,
            message="Failed to fetch discovery document",
        )

    if not disc.get("authorization_endpoint"):
        return SsoProviderTestResult(
            success=False,
            message="Discovery document missing authorization_endpoint",
        )

    provider_info = {
        "issuer": disc.get("issuer", ""),
        "authorization_endpoint": disc.get("authorization_endpoint"),
        "token_endpoint": disc.get("token_endpoint"),
        "userinfo_endpoint": disc.get("userinfo_endpoint"),
        "jwks_uri": disc.get("jwks_uri"),
        "scopes_supported": disc.get("scopes_supported", []),
    }
    if provider.client_id:
        provider_info["client_id_validated"] = True

    return SsoProviderTestResult(
        success=True,
        message="Successfully connected to OIDC provider. Endpoints discovered.",
        provider_info=provider_info,
    )


async def _test_saml_connection(provider: Any) -> SsoProviderTestResult:
    metadata_xml = provider.metadata_xml
    if not metadata_xml and provider.metadata_url:
        try:
            await validate_outbound_url_async(provider.metadata_url)
        except ValueError as exc:
            return SsoProviderTestResult(success=False, message=f"Rejected: {exc}")
        try:
            async with await pinned_async_client(provider.metadata_url) as client:
                resp = await client.get(provider.metadata_url, timeout=httpx.Timeout(10.0, connect=5.0))
                resp.raise_for_status()
                metadata_xml = resp.text
        except Exception:
            _log.warning("admin_sso._test_saml_connection", exc_info=True)
            return SsoProviderTestResult(
                success=False,
                message="Failed to fetch metadata",
            )

    if not metadata_xml:
        return SsoProviderTestResult(
            success=False,
            message="Metadata URL or Metadata XML is required for SAML providers",
        )

    try:
        root = ElementTree.fromstring(metadata_xml)
    except Exception as exc:
        _log.warning("admin_sso._test_saml_connection", exc_info=True)
        return SsoProviderTestResult(
            success=False,
            message=f"Failed to parse metadata XML: {exc}",
        )
    md_ns = "urn:oasis:names:tc:SAML:2.0:metadata"
    entity_id = root.get("entityID", "")

    sso_descriptor = root.find(f"{{{md_ns}}}IDPSSODescriptor")
    if sso_descriptor is None:
        return SsoProviderTestResult(
            success=False,
            message="No IDPSSODescriptor found in metadata XML",
        )

    sso_service = sso_descriptor.find(
        f"{{{md_ns}}}SingleSignOnService[@Binding='urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect']"
    )
    if sso_service is None:
        sso_service = sso_descriptor.find(f"{{{md_ns}}}SingleSignOnService")
    sso_url = ""
    cert_info = []
    if sso_service is not None:
        sso_url = sso_service.get("Location", "")

    for key_desc in sso_descriptor.findall(f"{{{md_ns}}}KeyDescriptor"):
        key_info = key_desc.find(f"{{{md_ns}}}KeyInfo")
        if key_info is not None:
            x509 = key_info.find(f"{{{md_ns}}}X509Data")
            if x509 is not None:
                cert = x509.find(f"{{{md_ns}}}X509Certificate")
                if cert is not None and cert.text:
                    raw = cert.text.replace(" ", "")
                    cert_info.append(
                        {
                            "use": key_desc.get("use", "signing"),
                            "certificate": f"{raw[:40]}...{raw[-20:]}",
                        }
                    )

    provider_info = {
        "entity_id": entity_id,
        "sso_url": sso_url,
        "certificates": cert_info,
    }

    return SsoProviderTestResult(
        success=True,
        message="Successfully parsed SAML metadata.",
        provider_info=provider_info,
    )


@router.put("/providers/{provider_id}/toggle", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("admin.sso.toggle_provider_endpoint")
async def toggle_provider_endpoint(
    provider_id: uuid.UUID,
    _: object = require_feature("sso"),
    current_user: TenantPrincipal = require_permission(_CODE_SSO_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> SsoProviderResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            provider = await toggle_provider(
                session,
                provider_id,
                org_id=current_user.organisation_id,
                actor_user_id=current_user.account_id,
            )
    except IntegrityError:
        _log.exception("admin_sso.toggle_provider_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on toggle: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SSO providers DB error on toggle: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in toggle_provider_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_SSO_PROVIDER_NOT_FOUND,
        )
    return SsoProviderResponse.model_validate(provider)


class GroupMappingItem(BaseModel):
    idp_group: str
    team_id: str
    team_role: str = "viewer"


class GroupMappingsRequest(BaseModel):
    mappings: list[GroupMappingItem]


class GroupMappingsResponse(BaseModel):
    mappings: list[GroupMappingItem]


@router.put("/providers/{provider_id}/group-mappings")
@handle_db_errors("admin.sso.set_group_mappings_endpoint")
async def set_group_mappings_endpoint(
    provider_id: uuid.UUID,
    req: GroupMappingsRequest,
    _: object = require_feature("sso"),
    current_user: TenantPrincipal = require_permission(_CODE_SSO_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> GroupMappingsResponse:
    mappings_dict = [m.model_dump() for m in req.mappings]
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            provider = await set_group_mappings(
                session,
                provider_id,
                mappings_dict,
                org_id=current_user.organisation_id,
            )
    except IntegrityError:
        _log.exception("admin_sso.set_group_mappings_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on set_group_mappings: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SSO providers DB error on set_group_mappings: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in set_group_mappings_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_SSO_PROVIDER_NOT_FOUND,
        )
    return GroupMappingsResponse(mappings=[GroupMappingItem(**m) for m in provider.group_mappings])


@router.get("/providers/{provider_id}/group-mappings")
@handle_db_errors("admin.sso.get_group_mappings_endpoint")
async def get_group_mappings_endpoint(
    provider_id: uuid.UUID,
    _: object = require_feature("sso"),
    current_user: TenantPrincipal = require_permission(_CODE_SSO_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> GroupMappingsResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            provider = await get_provider(session, provider_id, org_id=current_user.organisation_id)
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on get_group_mappings: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SSO providers DB error on get_group_mappings: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in get_group_mappings_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_SSO_PROVIDER_NOT_FOUND,
        )
    return GroupMappingsResponse(mappings=[GroupMappingItem(**m) for m in provider.group_mappings])

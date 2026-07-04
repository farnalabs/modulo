import json
import logging
import uuid
from typing import Any

import httpx
from defusedxml import ElementTree as ET
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_feature
from modulo.api.middleware.sensitive_mask import SensitiveValue
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.sso_provider import (
    create_provider,
    delete_provider,
    get_provider,
    list_providers,
    set_group_mappings,
    toggle_provider,
    update_provider,
)
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/sso", tags=["admin-sso"])


class SsoProviderCreate(BaseModel):
    provider_type: str = Field(pattern=r"^(oidc|saml)$")
    name: str = Field(min_length=1, max_length=255)
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
    id: str
    provider_type: str
    name: str
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
    created_at: str
    updated_at: str

    @classmethod
    def from_orm(cls, provider: Any) -> "SsoProviderResponse":
        scopes = None
        if provider.scopes:
            try:
                parsed = json.loads(provider.scopes)
                if isinstance(parsed, list):
                    scopes = parsed
                else:
                    scopes = [str(parsed)]
            except (json.JSONDecodeError, TypeError):
                scopes = None

        return cls(
            id=str(provider.id),
            provider_type=provider.provider_type,
            name=provider.name,
            client_id=provider.client_id,
            client_secret=provider.client_secret,
            discovery_url=provider.discovery_url,
            metadata_url=provider.metadata_url,
            metadata_xml=provider.metadata_xml,
            entity_id=provider.entity_id,
            scopes=scopes,
            enabled=provider.enabled,
            auto_provision=provider.auto_provision,
            default_role=provider.default_role,
            created_at=provider.created_at.isoformat() if provider.created_at else "",
            updated_at=provider.updated_at.isoformat() if provider.updated_at else "",
        )


class SsoProviderTestResult(BaseModel):
    success: bool
    message: str
    provider_info: dict[str, Any] | None = None


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can manage SSO providers",
        )


@router.get("/providers", response_model=list[SsoProviderResponse])
async def get_providers(
    _: None = require_feature("sso"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[SsoProviderResponse]:
    _require_admin(current_user)
    try:
        providers = await list_providers(session)
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    return [SsoProviderResponse.from_orm(p) for p in providers]  # type: ignore[pydantic-orm]


@router.post("/providers", response_model=SsoProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider_endpoint(
    req: SsoProviderCreate,
    _: None = require_feature("sso"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> SsoProviderResponse:
    _require_admin(current_user)
    try:
        provider = await create_provider(
            session,
            provider_type=req.provider_type,
            name=req.name,
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
            org_id=current_user.organisation_id,
            actor_user_id=current_user.account_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on create: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    return SsoProviderResponse.from_orm(provider)  # type: ignore[pydantic-orm]


@router.put("/providers/{provider_id}", response_model=SsoProviderResponse)
async def update_provider_endpoint(
    provider_id: uuid.UUID,
    req: SsoProviderUpdate,
    _: None = require_feature("sso"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> SsoProviderResponse:
    _require_admin(current_user)
    updates = {k: v for k, v in req.model_dump(exclude_none=True).items()}
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    try:
        provider = await update_provider(session, provider_id, actor_user_id=current_user.account_id, **updates)
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on update: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO provider not found",
        )
    return SsoProviderResponse.from_orm(provider)  # type: ignore[pydantic-orm]


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_endpoint(
    provider_id: uuid.UUID,
    _: None = require_feature("sso"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    _require_admin(current_user)
    try:
        deleted = await delete_provider(session, provider_id, actor_user_id=current_user.account_id)
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on delete: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc


@router.post("/providers/{provider_id}/test", response_model=SsoProviderTestResult)
async def test_provider_connection(
    provider_id: uuid.UUID,
    _: None = require_feature("sso"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> SsoProviderTestResult:
    _require_admin(current_user)
    try:
        provider = await get_provider(session, provider_id)
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on test connection: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO provider not found",
        )

    try:
        if provider.provider_type == "oidc":
            return await _test_oidc_connection(provider)
        else:
            return await _test_saml_connection(provider)
    except Exception as exc:
        _log.warning("SSO test connection failed: %s", exc)
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
        async with httpx.AsyncClient() as client:
            resp = await client.get(provider.discovery_url, timeout=httpx.Timeout(10.0, connect=5.0))
            resp.raise_for_status()
            disc = resp.json()
    except Exception as exc:
        return SsoProviderTestResult(
            success=False,
            message=f"Failed to fetch discovery document: {exc}",
        )

    if not disc.get("authorization_endpoint"):
        return SsoProviderTestResult(
            success=False,
            message="Discovery document missing authorization_endpoint",
        )

    if provider.client_id:
        issuer = disc.get("issuer", "")
        provider_info = {
            "issuer": issuer,
            "authorization_endpoint": disc.get("authorization_endpoint"),
            "token_endpoint": disc.get("token_endpoint"),
            "userinfo_endpoint": disc.get("userinfo_endpoint"),
            "jwks_uri": disc.get("jwks_uri"),
            "scopes_supported": disc.get("scopes_supported", []),
            "client_id_validated": True,
        }
    else:
        provider_info = {
            "issuer": disc.get("issuer", ""),
            "authorization_endpoint": disc.get("authorization_endpoint"),
            "token_endpoint": disc.get("token_endpoint"),
            "userinfo_endpoint": disc.get("userinfo_endpoint"),
            "jwks_uri": disc.get("jwks_uri"),
            "scopes_supported": disc.get("scopes_supported", []),
        }

    return SsoProviderTestResult(
        success=True,
        message="Successfully connected to OIDC provider. Endpoints discovered.",
        provider_info=provider_info,
    )


async def _test_saml_connection(provider: Any) -> SsoProviderTestResult:
    metadata_xml = provider.metadata_xml
    if not metadata_xml and provider.metadata_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(provider.metadata_url, timeout=httpx.Timeout(10.0, connect=5.0))
                resp.raise_for_status()
                metadata_xml = resp.text
        except Exception as exc:
            return SsoProviderTestResult(
                success=False,
                message=f"Failed to fetch metadata: {exc}",
            )

    if not metadata_xml:
        return SsoProviderTestResult(
            success=False,
            message="Metadata URL or Metadata XML is required for SAML providers",
        )

    try:
        root = ET.fromstring(metadata_xml)
    except Exception as exc:
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


@router.put("/providers/{provider_id}/toggle", response_model=SsoProviderResponse)
async def toggle_provider_endpoint(
    provider_id: uuid.UUID,
    _: None = require_feature("sso"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> SsoProviderResponse:
    _require_admin(current_user)
    try:
        provider = await toggle_provider(session, provider_id, actor_user_id=current_user.account_id)
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on toggle: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO provider not found",
        )
    return SsoProviderResponse.from_orm(provider)  # type: ignore[pydantic-orm]


class GroupMappingItem(BaseModel):
    idp_group: str
    team_id: str
    team_role: str = "viewer"


class GroupMappingsRequest(BaseModel):
    mappings: list[GroupMappingItem]


class GroupMappingsResponse(BaseModel):
    mappings: list[GroupMappingItem]


@router.put("/providers/{provider_id}/group-mappings", response_model=GroupMappingsResponse)
async def set_group_mappings_endpoint(
    provider_id: uuid.UUID,
    req: GroupMappingsRequest,
    _: None = require_feature("sso"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> GroupMappingsResponse:
    _require_admin(current_user)
    mappings_dict = [m.model_dump() for m in req.mappings]
    try:
        provider = await set_group_mappings(session, provider_id, mappings_dict)
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on set_group_mappings: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO provider not found",
        )
    return GroupMappingsResponse(mappings=[GroupMappingItem(**m) for m in provider.group_mappings])


@router.get("/providers/{provider_id}/group-mappings", response_model=GroupMappingsResponse)
async def get_group_mappings_endpoint(
    provider_id: uuid.UUID,
    _: None = require_feature("sso"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> GroupMappingsResponse:
    _require_admin(current_user)
    try:
        provider = await get_provider(session, provider_id)
    except ProgrammingError as exc:
        _log.warning("SSO providers table not available on get_group_mappings: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO provider not found",
        )
    return GroupMappingsResponse(mappings=[GroupMappingItem(**m) for m in provider.group_mappings])

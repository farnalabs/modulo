"""Registry API — browse, publish, pull, and trust-verify registry primitives."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.registry import (
    get_publisher_status,
    get_registry_primitive,
    list_registry_primitives_ranked,
    list_verified_publishers,
    publish_primitive,
    register_publisher,
    revoke_publisher,
    verify_bundle_integrity,
    verify_primitive_signature,
)
from modulo.core.registry.crypto import (
    generate_keypair as crypto_generate_keypair,
)
from modulo.core.registry.crypto import (
    sign_primitive as crypto_sign_primitive,
)
from modulo.core.registry.crypto import (
    verify_signature as crypto_verify_signature,
)
from modulo.db.crud.library_primitive import create_library_primitive
from modulo.db.crud.publisher import get_publisher_by_key as db_get_publisher_by_key
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/registry", tags=["registry"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegistryEntryResponse(BaseModel):
    author: str
    name: str
    slug: str
    version: str
    primitive_type: str
    description: str
    tags: list[str]
    content_json: dict[str, Any]
    checksum_sha256: str
    ed25519_signature_hex: str
    signing_key_fingerprint: str
    publisher_status: str = "community"
    published_at: datetime
    download_count: int

    model_config = {"from_attributes": True, "arbitrary_types_allowed": True}


class RegistryListResponse(BaseModel):
    items: list[RegistryEntryResponse]
    total: int


class RegistryRankedItemResponse(BaseModel):
    entry: RegistryEntryResponse
    publisher_status: str
    publisher_name: str
    popularity_score: float


class RegistryRankedListResponse(BaseModel):
    items: list[RegistryRankedItemResponse]
    total: int


class PublishRequest(BaseModel):
    author: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    primitive_type: str = Field(pattern=r"^(schema|workflow|agent|integration)$")
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    content_json: dict[str, Any]
    signing_key_hex: str = Field(min_length=64)


class PullResponse(BaseModel):
    entry: RegistryEntryResponse
    verified: bool
    integrity_ok: bool


class RegisterPublisherRequest(BaseModel):
    fingerprint_hex: str = Field(min_length=16, max_length=64)
    author: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    website: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/primitives", response_model=RegistryRankedListResponse)
async def list_registry_primitives_endpoint(
    author: str | None = Query(None),
    primitive_type: str | None = Query(None),
    search: str | None = Query(None),
    sort_by: str = Query("popularity", pattern=r"^(popularity|recent|downloads|rating)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> RegistryRankedListResponse:
    """List primitives with publisher trust badges and popularity ranking."""
    enriched = list_registry_primitives_ranked(
        author=author,
        primitive_type=primitive_type,
        search=search,
        sort_by=sort_by,
    )
    total = len(enriched)
    start = (page - 1) * page_size
    sliced = enriched[start : start + page_size]
    items = [
        RegistryRankedItemResponse(
            entry=RegistryEntryResponse(
                author=item["entry"].author,
                name=item["entry"].name,
                slug=item["entry"].slug,
                version=item["entry"].version,
                primitive_type=item["entry"].primitive_type,
                description=item["entry"].description,
                tags=item["entry"].tags,
                content_json=item["entry"].content_json,
                checksum_sha256=item["entry"].checksum_sha256,
                ed25519_signature_hex=item["entry"].ed25519_signature_hex,
                signing_key_fingerprint=item["entry"].signing_key_fingerprint,
                publisher_status=item["publisher_status"],
                published_at=item["entry"].published_at,
                download_count=item["entry"].download_count,
            ),
            publisher_status=item["publisher_status"],
            publisher_name=item["publisher_name"],
            popularity_score=item["popularity_score"],
        )
        for item in sliced
    ]
    return RegistryRankedListResponse(items=items, total=total)


@router.get("/primitives/{slug:path}", response_model=PullResponse)
async def get_registry_primitive_endpoint(
    slug: str,
    verify: bool = Query(True),
) -> PullResponse:
    """Get a single primitive by its ``author/name`` slug, with signature verification."""
    entry = get_registry_primitive(slug)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive '{slug}' not found",
        )

    verified = verify_primitive_signature(entry) if verify else False
    publisher_status = get_publisher_status(entry.signing_key_fingerprint)
    bundle = {
        "author": entry.author,
        "name": entry.name,
        "version": entry.version,
        "primitive_type": entry.primitive_type,
        "description": entry.description,
        "tags": entry.tags,
        "content_json": entry.content_json,
    }
    integrity_ok = verify_bundle_integrity(bundle, entry.checksum_sha256)

    return PullResponse(
        entry=RegistryEntryResponse(
            author=entry.author,
            name=entry.name,
            slug=entry.slug,
            version=entry.version,
            primitive_type=entry.primitive_type,
            description=entry.description,
            tags=entry.tags,
            content_json=entry.content_json,
            checksum_sha256=entry.checksum_sha256,
            ed25519_signature_hex=entry.ed25519_signature_hex,
            signing_key_fingerprint=entry.signing_key_fingerprint,
            publisher_status=publisher_status,
            published_at=entry.published_at,
            download_count=entry.download_count,
        ),
        verified=verified,
        integrity_ok=integrity_ok,
    )


@router.post(
    "/primitives",
    response_model=RegistryEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_primitive_endpoint(
    body: PublishRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RegistryEntryResponse:
    """Publish a new primitive to the registry (in-memory for alpha)."""
    entry = await publish_primitive(
        author=body.author,
        name=body.name,
        primitive_type=body.primitive_type,
        description=body.description,
        tags=body.tags,
        content_json=body.content_json,
        signing_key_hex=body.signing_key_hex,
    )
    return RegistryEntryResponse.model_validate(entry)


@router.post(
    "/primitives/{slug:path}/download",
    response_model=PullResponse,
)
async def download_registry_primitive_endpoint(
    slug: str,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PullResponse:
    """Download a primitive from the registry into the org's local library.

    Increments the download count, verifies the signature and bundle integrity,
    and creates a local LibraryPrimitive record.
    """
    entry = get_registry_primitive(slug)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive '{slug}' not found",
        )

    bundle = {
        "author": entry.author,
        "name": entry.name,
        "version": entry.version,
        "primitive_type": entry.primitive_type,
        "description": entry.description,
        "tags": entry.tags,
        "content_json": entry.content_json,
    }
    verified = verify_primitive_signature(entry)
    integrity_ok = verify_bundle_integrity(bundle, entry.checksum_sha256)

    entry.download_count += 1

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await create_library_primitive(
            session,
            org_id=principal.organisation_id,
            source="registry",
            primitive_type=entry.primitive_type,
            name=f"{entry.author}/{entry.name}",
            slug=entry.slug.replace("/", "-"),
            description=entry.description,
            author=entry.author,
            version=entry.version,
            tags=entry.tags,
            content_json=entry.content_json,
            source_url=f"/api/v1/registry/primitives/{entry.slug}",
            forked_from=None,
            checksum=entry.checksum_sha256,
            ed25519_signature=entry.ed25519_signature_hex,
            verified=verified,
            download_count=None,
            average_rating=None,
            review_count=None,
            owner_team_id=None,
            visibility="org",
            created_by=principal.user_id,
        )

    return PullResponse(
        entry=RegistryEntryResponse.model_validate(entry),
        verified=verified,
        integrity_ok=integrity_ok,
    )


# ---------------------------------------------------------------------------
# Registry protocol v2 — publish / pull / verify
# ---------------------------------------------------------------------------


class PublishRequestV2(BaseModel):
    author: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    primitive_type: str = Field(pattern=r"^(schema|workflow|agent|integration|test_fixture|pipeline_template)$")
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    content_json: dict[str, Any]


class PublishResponseV2(BaseModel):
    slug: str
    version: str
    checksum_sha256: str
    ed25519_signature_hex: str
    signing_key_fingerprint: str
    verified: bool


class PullResponseV2(BaseModel):
    author: str
    name: str
    slug: str
    version: str
    primitive_type: str
    description: str
    tags: list[str]
    content_json: dict[str, Any]
    checksum_sha256: str
    ed25519_signature_hex: str
    signing_key_fingerprint: str
    publisher_status: str
    verified: bool


class VerifyResponseV2(BaseModel):
    slug: str
    verified: bool
    signing_key_fingerprint: str
    publisher_status: str
    trust_tier: str | None = None
    publisher_name: str | None = None


@router.post("/publish", response_model=PublishResponseV2, status_code=status.HTTP_201_CREATED)
async def publish_primitive_v2(
    body: PublishRequestV2,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PublishResponseV2:
    """Publish a primitive to the registry (v2 protocol).

    Generates a temp keypair, signs the primitive data with Ed25519,
    computes a SHA-256 checksum, and stores the entry in the registry.
    """
    keypair = crypto_generate_keypair()

    payload = {
        "author": body.author,
        "name": body.name,
        "version": "1.0",
        "primitive_type": body.primitive_type,
        "description": body.description,
        "tags": body.tags,
        "content_json": body.content_json,
    }
    checksum = hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    signature = crypto_sign_primitive(payload, keypair["private_key"])

    entry = await publish_primitive(
        author=body.author,
        name=body.name,
        primitive_type=body.primitive_type,
        description=body.description,
        tags=body.tags,
        content_json=body.content_json,
        signing_key_hex=keypair["private_key"],
    )

    return PublishResponseV2(
        slug=entry.slug,
        version=entry.version,
        checksum_sha256=checksum,
        ed25519_signature_hex=signature,
        signing_key_fingerprint=keypair["fingerprint"],
        verified=True,
    )


@router.get("/pull/{slug:path}", response_model=PullResponseV2)
async def pull_registry_primitive_v2(
    slug: str,
) -> PullResponseV2:
    """Pull a published primitive from the registry (v2 protocol).

    Returns the full primitive data plus signature and checksum.
    Verifies the signature before returning.
    """
    entry = get_registry_primitive(slug)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive '{slug}' not found",
        )

    verified = verify_primitive_signature(entry)
    publisher_status = get_publisher_status(entry.signing_key_fingerprint)

    return PullResponseV2(
        author=entry.author,
        name=entry.name,
        slug=entry.slug,
        version=entry.version,
        primitive_type=entry.primitive_type,
        description=entry.description,
        tags=entry.tags,
        content_json=entry.content_json,
        checksum_sha256=entry.checksum_sha256,
        ed25519_signature_hex=entry.ed25519_signature_hex,
        signing_key_fingerprint=entry.signing_key_fingerprint,
        publisher_status=publisher_status,
        verified=verified,
    )


@router.get("/verify/{slug:path}", response_model=VerifyResponseV2)
async def verify_registry_primitive_v2(
    slug: str,
    public_key_hex: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> VerifyResponseV2:
    """Verify a published primitive's signature (v2 protocol).

    Optionally accepts a ``public_key_hex`` query parameter to verify
    against a specific public key.  Otherwise uses the built-in registry
    key or the publisher's registered key.

    Returns the publisher's trust tier (green/amber/null) and name
    when a matching publisher is found.
    """
    entry = get_registry_primitive(slug)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive '{slug}' not found",
        )

    publisher_status = get_publisher_status(entry.signing_key_fingerprint)
    trust_tier: str | None = None
    publisher_name: str | None = None
    verified = False

    if public_key_hex:
        payload = {
            "author": entry.author,
            "name": entry.name,
            "version": entry.version,
            "primitive_type": entry.primitive_type,
            "description": entry.description,
            "tags": entry.tags,
            "content_json": entry.content_json,
        }
        verified = crypto_verify_signature(payload, entry.ed25519_signature_hex, public_key_hex)

        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            db_pub = await db_get_publisher_by_key(session, principal.organisation_id, public_key_hex)
            if db_pub is not None:
                trust_tier = db_pub.trust_tier
                publisher_name = db_pub.name
    else:
        verified = verify_primitive_signature(entry)

    return VerifyResponseV2(
        slug=entry.slug,
        verified=verified,
        signing_key_fingerprint=entry.signing_key_fingerprint,
        publisher_status=publisher_status,
        trust_tier=trust_tier,
        publisher_name=publisher_name,
    )


# ---------------------------------------------------------------------------
# Publisher management
# ---------------------------------------------------------------------------


@router.post("/publishers", status_code=status.HTTP_201_CREATED)
async def register_publisher_endpoint(
    body: RegisterPublisherRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    """Register a verified publisher (admin operation)."""
    pub = register_publisher(
        fingerprint_hex=body.fingerprint_hex,
        author=body.author,
        name=body.name,
        website=body.website,
    )
    return {"status": "registered", "fingerprint": pub.fingerprint, "author": pub.author}


@router.post("/publishers/{fingerprint_hex}/revoke")
async def revoke_publisher_endpoint(
    fingerprint_hex: str,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    """Revoke a publisher's trust status."""
    ok = revoke_publisher(fingerprint_hex)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Publisher not found")
    return {"status": "revoked", "fingerprint": fingerprint_hex}


@router.get("/publishers")
async def list_publishers_endpoint() -> list[dict[str, str]]:
    """List all verified publishers."""
    publishers = list_verified_publishers()
    return [
        {"author": p.author, "name": p.name, "fingerprint": p.fingerprint, "website": p.website} for p in publishers
    ]

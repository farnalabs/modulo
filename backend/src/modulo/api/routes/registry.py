"""Registry API — browse, publish, pull, and trust-verify registry primitives."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives import serialization as pem_serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_UNEXPECTED_ERROR_NO_PERIOD
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
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
    verify_signature as crypto_verify_signature,
)
from modulo.db.crud.library_primitive import create_library_primitive
from modulo.db.crud.publisher import get_publisher_by_key as db_get_publisher_by_key
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.registry.crypto import (
    verify as crypto_pem_verify,
)
from modulo.registry.crypto import (
    verify_trust_anchor,
)
from modulo.util import sanitise_log_value as _sanitise_log_value

_log = logging.getLogger(__name__)

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


@router.get("/primitives")
@handle_db_errors("registry.list_registry_primitives_endpoint")
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


@router.get("/primitives/{slug:path}")
@handle_db_errors("registry.get_registry_primitive_endpoint")
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
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors("registry.publish_primitive_endpoint")
async def publish_primitive_endpoint(
    req: PublishRequest,
    principal: TenantPrincipal = require_permission("registry.publish"),
) -> RegistryEntryResponse:
    """Publish a new primitive to the registry (in-memory for alpha)."""
    try:
        entry = publish_primitive(
            author=req.author,
            name=req.name,
            primitive_type=req.primitive_type,
            description=req.description,
            tags=req.tags,
            content_json=req.content_json,
            signing_key_hex=req.signing_key_hex,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("registry.publish_primitive.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e
    return RegistryEntryResponse.model_validate(entry)


@router.post(
    "/primitives/{slug:path}/download",
)
@handle_db_errors("registry.download_registry_primitive_endpoint")
async def download_registry_primitive_endpoint(
    slug: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("registry.pull"),
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

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
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
                account_id=principal.account_id,
            )
    except ProgrammingError:
        _log.exception("registry.download_registry_primitive_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.warning(
            "DB error in download_registry_primitive_endpoint for slug=%s",
            _sanitise_log_value(slug),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("registry.download_primitive.unexpected_error", extra={"slug": slug})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

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
    signature: str = Field(min_length=1, description="Base64 Ed25519 signature of the payload")
    public_key_pem: str = Field(min_length=1, description="PEM-encoded Ed25519 public key")


class PublishResponseV2(BaseModel):
    slug: str
    version: str
    checksum_sha256: str
    ed25519_signature_hex: str
    signing_key_fingerprint: str
    public_key_pem: str = ""
    trust_anchor_verified: bool = False
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
    trust_anchor_verified: bool = False


@router.post("/publish", status_code=status.HTTP_201_CREATED)
@handle_db_errors("registry.publish_primitive_v2")
async def publish_primitive_v2(
    req: PublishRequestV2,
    principal: TenantPrincipal = require_permission("registry.publish"),
) -> PublishResponseV2:
    """Publish a primitive to the registry (v2 protocol).

    Accepts a signed payload — the client signs the primitive data with
    their Ed25519 private key and sends the signature + public key.
    The server verifies the signature before accepting.
    """
    try:
        payload_bytes = json.dumps(
            {
                "author": req.author,
                "name": req.name,
                "primitive_type": req.primitive_type,
                "description": req.description,
                "tags": req.tags,
                "content_json": req.content_json,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

        if not crypto_pem_verify(req.public_key_pem, payload_bytes, req.signature):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Signature verification failed — payload does not match the provided public key",
            )

        trust_anchor_ok = verify_trust_anchor(req.public_key_pem, req.signature)

        pub_key = pem_serialization.load_pem_public_key(req.public_key_pem.encode())
        if not isinstance(pub_key, Ed25519PublicKey):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Ed25519 public key PEM",
            )
        public_raw = pub_key.public_bytes(
            encoding=pem_serialization.Encoding.Raw,
            format=pem_serialization.PublicFormat.Raw,
        )
        fingerprint = hashlib.sha256(public_raw).hexdigest()[:16]

        sig_hex = base64.b64decode(req.signature).hex()

        temp_keypair = crypto_generate_keypair()
        entry = publish_primitive(
            author=req.author,
            name=req.name,
            primitive_type=req.primitive_type,
            description=req.description,
            tags=req.tags,
            content_json=req.content_json,
            signing_key_hex=temp_keypair["private_key"],
        )

        entry.ed25519_signature_hex = sig_hex
        entry.signing_key_fingerprint = fingerprint

        checksum = hashlib.sha256(payload_bytes).hexdigest()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("registry.publish_primitive_v2.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return PublishResponseV2(
        slug=entry.slug,
        version=entry.version,
        checksum_sha256=checksum,
        ed25519_signature_hex=sig_hex,
        signing_key_fingerprint=fingerprint,
        public_key_pem=req.public_key_pem,
        trust_anchor_verified=trust_anchor_ok,
        verified=True,
    )


@router.get("/pull/{slug:path}")
@handle_db_errors("registry.pull_registry_primitive_v2")
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


@router.get("/verify/{slug:path}")
@handle_db_errors("registry.verify_registry_primitive_v2")
async def verify_registry_primitive_v2(
    slug: str,
    public_key_hex: str | None = None,
    public_key_pem: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("registry.pull"),
) -> VerifyResponseV2:
    """Verify a published primitive's signature (v2 protocol).

    Accepts an optional ``public_key_hex`` parameter for hex-encoded key
    verification, or ``public_key_pem`` for PEM-encoded key verification
    with trust anchor support.  When neither is provided, uses the
    built-in registry key.

    Returns trust anchor verification status, the publisher's trust tier
    (green/amber/null) and name when a matching publisher is found.
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
    trust_anchor_verified = False

    if public_key_pem:
        payload_bytes = json.dumps(
            {
                "author": entry.author,
                "name": entry.name,
                "version": entry.version,
                "primitive_type": entry.primitive_type,
                "description": entry.description,
                "tags": entry.tags,
                "content_json": entry.content_json,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

        sig_b64 = base64.b64encode(bytes.fromhex(entry.ed25519_signature_hex)).decode()

        verified = crypto_pem_verify(public_key_pem, payload_bytes, sig_b64)
        trust_anchor_verified = verify_trust_anchor(public_key_pem, sig_b64)
    elif public_key_hex:
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

        try:
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                db_pub = await db_get_publisher_by_key(session, principal.organisation_id, public_key_hex)
                if db_pub is not None:
                    trust_tier = db_pub.trust_tier
                    publisher_name = db_pub.name
        except ProgrammingError:
            _log.exception("registry.verify_registry_primitive_v2")
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Feature is not available. Run database migrations to enable it.",
            ) from None
        except SQLAlchemyError:
            _log.exception("registry.verify_registry_primitive_v2")
            _log.warning(
                "DB error in verify_registry_primitive_v2: public_key_hex path, slug=%s, fp=%s",
                _sanitise_log_value(slug),
                _sanitise_log_value(entry.signing_key_fingerprint),
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database operation failed. Please try again.",
            ) from None
        except HTTPException:
            raise
        except Exception as e:
            _log.exception(
                "registry.verify_primitive.unexpected_error",
                extra={"slug": slug, "fp": entry.signing_key_fingerprint},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
            ) from e
    else:
        verified = verify_primitive_signature(entry)

    return VerifyResponseV2(
        slug=entry.slug,
        verified=verified,
        signing_key_fingerprint=entry.signing_key_fingerprint,
        publisher_status=publisher_status,
        trust_tier=trust_tier,
        publisher_name=publisher_name,
        trust_anchor_verified=trust_anchor_verified,
    )


# ---------------------------------------------------------------------------
# Publisher management
# ---------------------------------------------------------------------------


@router.post("/publishers", status_code=status.HTTP_201_CREATED)
@handle_db_errors("registry.register_publisher_endpoint")
async def register_publisher_endpoint(
    req: RegisterPublisherRequest,
    principal: TenantPrincipal = require_permission("registry.publisher.manage"),
) -> dict[str, str]:
    """Register a verified publisher (admin operation)."""
    pub = register_publisher(
        fingerprint_hex=req.fingerprint_hex,
        author=req.author,
        name=req.name,
        website=req.website,
    )
    return {"status": "registered", "fingerprint": pub.fingerprint, "author": pub.author}


@router.post("/publishers/{fingerprint_hex}/revoke")
@handle_db_errors("registry.revoke_publisher_endpoint")
async def revoke_publisher_endpoint(
    fingerprint_hex: str,
    principal: TenantPrincipal = require_permission("registry.publisher.manage"),
) -> dict[str, str]:
    """Revoke a publisher's trust status."""
    ok = revoke_publisher(fingerprint_hex)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Publisher not found")
    return {"status": "revoked", "fingerprint": fingerprint_hex}


@router.get("/publishers")
@handle_db_errors("registry.list_publishers_endpoint")
async def list_publishers_endpoint() -> list[dict[str, str]]:
    """List all verified publishers."""
    publishers = list_verified_publishers()
    return [
        {"author": p.author, "name": p.name, "fingerprint": p.fingerprint, "website": p.website} for p in publishers
    ]

"""Registry service — Ed25519 signing, SHA-256 integrity, publish/pull protocol.

Primitives are identified by ``author/name`` namespaced slugs.
The in-memory built-in registry can be replaced with a hosted Modulo-operated
registry in production.

State of the art:
  - Ed25519-signed manifests via the ``cryptography`` library
  - SHA-256 bundle integrity hash stored in ``checksum``
  - Version pinning on import
  - Abstract schema namespacing (author/name)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from modulo.core.registry.crypto import _canonical_json

logger = logging.getLogger(__name__)

__all__ = [
    "PUBLISHER_TRUST_COMMUNITY",
    "PUBLISHER_TRUST_REVOKED",
    "PUBLISHER_TRUST_VERIFIED",
    "Publisher",
    "RegistryEntry",
    "compute_bundle_hash",
    "compute_popularity_score",
    "fingerprint",
    "generate_signing_key",
    "get_publisher",
    "get_publisher_status",
    "get_registry_primitive",
    "list_registry_primitives",
    "list_registry_primitives_ranked",
    "list_verified_publishers",
    "publish_primitive",
    "register_publisher",
    "resolve_namespaced_slug",
    "revoke_publisher",
    "sign_manifest",
    "verify_bundle_integrity",
    "verify_manifest",
    "verify_primitive_signature",
]

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RegistryEntry:
    """A published primitive in the registry."""

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
    published_at: datetime
    download_count: int = 0


# ---------------------------------------------------------------------------
# Signing utilities
# ---------------------------------------------------------------------------


def _sha256_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry_payload(
    author: str,
    name: str,
    version: str,
    primitive_type: str,
    description: str,
    tags: list[str],
    content_json: dict[str, Any],
) -> dict[str, Any]:
    """Build the canonical payload dict for signing/hashing a registry entry."""
    return {
        "author": author,
        "name": name,
        "version": version,
        "primitive_type": primitive_type,
        "description": description,
        "tags": tags,
        "content_json": content_json,
    }


def generate_signing_key() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a new Ed25519 key pair for registry publishing."""
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def fingerprint(public_key: Ed25519PublicKey) -> str:
    """Return a stable hex fingerprint for a public key."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _sha256_digest(raw)[:16]


def sign_manifest(
    payload: dict[str, Any],
    private_key: Ed25519PrivateKey,
) -> str:
    """Sign a canonical JSON payload, return hex signature."""
    try:
        canonical = _canonical_json(payload)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    sig = private_key.sign(canonical)
    return sig.hex()


def verify_manifest(
    payload: dict[str, Any],
    signature_hex: str,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify an Ed25519 signature against canonical JSON of payload."""
    try:
        canonical = _canonical_json(payload)
    except ValueError:
        logger.error("verify_manifest: non-serializable payload")
        return False
    try:
        public_key.verify(bytes.fromhex(signature_hex), canonical)
    except InvalidSignature:
        logger.warning("verify_manifest: invalid signature")
        return False
    except ValueError:
        logger.warning("verify_manifest: bad signature hex")
        return False
    else:
        return True


# ---------------------------------------------------------------------------
# Bundle integrity
# ---------------------------------------------------------------------------


def compute_bundle_hash(bundle: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of a canonical bundle JSON."""
    try:
        canonical = _canonical_json(bundle)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    return _sha256_digest(canonical)


def verify_bundle_integrity(bundle: dict[str, Any], expected_sha256: str) -> bool:
    """Verify that a bundle matches its expected SHA-256 checksum."""
    return compute_bundle_hash(bundle) == expected_sha256


# ---------------------------------------------------------------------------
# In-memory built-in registry (replaced with hosted registry in production)
# ---------------------------------------------------------------------------

_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def _build_entry(
    author: str,
    name: str,
    primitive_type: str,
    description: str,
    tags: list[str],
    content_json: dict[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    version: str = "1.0",
) -> RegistryEntry:
    public = private_key.public_key()
    payload = _entry_payload(author, name, version, primitive_type, description, tags, content_json)
    checksum = compute_bundle_hash(payload)
    sig = sign_manifest(payload, private_key)
    slug = f"{author}/{name}"

    return RegistryEntry(
        author=author,
        name=name,
        slug=slug,
        version=version,
        primitive_type=primitive_type,
        description=description,
        tags=list(tags),
        content_json=content_json,
        checksum_sha256=checksum,
        ed25519_signature_hex=sig,
        signing_key_fingerprint=fingerprint(public),
        published_at=_EPOCH,
    )


# Development signing key for the built-in registry primitives.
_registry_private = Ed25519PrivateKey.generate()
_registry_public = _registry_private.public_key()
_registry_fingerprint = fingerprint(_registry_public)

_BUILTIN_REGISTRY: dict[str, RegistryEntry] = {
    e.slug: e
    for e in [
        _build_entry(
            author="modulo",
            name="prd-input-schema",
            primitive_type="schema",
            description="Input schema for a product requirements document.",
            tags=["schema", "product", "prd"],
            content_json={
                "fields": [
                    {"name": "title", "type": "string", "required": True},
                    {"name": "problem_statement", "type": "string", "required": True},
                    {"name": "goals", "type": "array", "items": "string", "required": False},
                ],
            },
            private_key=_registry_private,
        ),
        _build_entry(
            author="modulo",
            name="requirements-output-schema",
            primitive_type="schema",
            description="Structured requirements extracted from a PRD.",
            tags=["schema", "requirements", "prd"],
            content_json={
                "fields": [
                    {"name": "functional", "type": "array", "items": "string", "required": True},
                    {
                        "name": "non_functional",
                        "type": "array",
                        "items": "string",
                        "required": False,
                    },
                ],
            },
            private_key=_registry_private,
        ),
        _build_entry(
            author="modulo",
            name="prd-to-requirements",
            primitive_type="workflow",
            description="End-to-end pipeline: ingest a PRD and produce structured requirements.",
            tags=["workflow", "prd", "requirements"],
            content_json={
                "nodes": [
                    {"id": "ingest", "agent": "prd-ingestion"},
                    {"id": "write", "agent": "requirements-writer"},
                ],
                "edges": [{"source": "ingest", "target": "write"}],
                "entry": "ingest",
            },
            private_key=_registry_private,
        ),
        # -------------------------------------------------------------------
        # Modulo dogfood pipeline primitives
        # -------------------------------------------------------------------
        _build_entry(
            author="modulo",
            name="github-issue-input-schema",
            primitive_type="schema",
            description="Schema for a GitHub issue to be processed by the Modulo dogfood pipeline.",
            tags=["schema", "github", "issue", "dogfood"],
            content_json={
                "fields": [
                    {"name": "issue_number", "type": "integer", "required": True},
                    {"name": "title", "type": "string", "required": True},
                    {"name": "body", "type": "string", "required": True},
                    {"name": "labels", "type": "array", "items": "string", "required": False},
                    {"name": "repo", "type": "string", "required": True},
                ],
            },
            private_key=_registry_private,
        ),
        _build_entry(
            author="modulo",
            name="structured-requirements-schema",
            primitive_type="schema",
            description="Structured requirements extracted from a GitHub issue for code generation.",
            tags=["schema", "requirements", "spec", "dogfood"],
            content_json={
                "fields": [
                    {"name": "agent_task", "type": "string", "required": True},
                    {"name": "feature_area", "type": "string", "required": True},
                    {"name": "spec_summary", "type": "string", "required": True},
                    {
                        "name": "files_to_change",
                        "type": "array",
                        "items": "string",
                        "required": False,
                    },
                    {"name": "implementation_notes", "type": "string", "required": False},
                ],
            },
            private_key=_registry_private,
        ),
        _build_entry(
            author="modulo",
            name="code-diff-output-schema",
            primitive_type="schema",
            description="Generated code changes as a list of file diffs.",
            tags=["schema", "code", "diff", "dogfood"],
            content_json={
                "fields": [
                    {
                        "name": "files",
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                        "required": True,
                    },
                ],
            },
            private_key=_registry_private,
        ),
        _build_entry(
            author="modulo",
            name="test-result-output-schema",
            primitive_type="schema",
            description="Result of running tests against generated code.",
            tags=["schema", "test", "result", "dogfood"],
            content_json={
                "fields": [
                    {"name": "passed", "type": "boolean", "required": True},
                    {"name": "failed", "type": "boolean", "required": True},
                    {"name": "output", "type": "string", "required": True},
                    {"name": "duration_ms", "type": "integer", "required": False},
                ],
            },
            private_key=_registry_private,
        ),
        _build_entry(
            author="modulo",
            name="pr-output-schema",
            primitive_type="schema",
            description="Result of creating a pull request.",
            tags=["schema", "pr", "github", "dogfood"],
            content_json={
                "fields": [
                    {"name": "pr_url", "type": "string", "required": True},
                    {"name": "pr_number", "type": "integer", "required": True},
                    {"name": "success", "type": "boolean", "required": True},
                ],
            },
            private_key=_registry_private,
        ),
        _build_entry(
            author="modulo",
            name="modulo-dogfood-pipeline",
            primitive_type="workflow",
            description=(
                "End-to-end pipeline that builds Modulo from a GitHub issue: reads spec, "
                "generates code, applies changes, runs tests, and creates a PR with HITL review."
            ),
            tags=["workflow", "dogfood", "modulo", "pipeline"],
            content_json={
                "nodes": [
                    {"id": "issue-reader", "agent": "issue-reader"},
                    {"id": "code-generator", "agent": "code-generator"},
                    {"id": "code-applier", "agent": "code-applier"},
                    {"id": "test-runner", "agent": "test-runner"},
                    {"id": "pr-creator", "agent": "pr-creator"},
                ],
                "edges": [
                    {"source": "issue-reader", "target": "code-generator"},
                    {"source": "code-generator", "target": "code-applier"},
                    {"source": "code-applier", "target": "test-runner"},
                    {
                        "source": "test-runner",
                        "target": "pr-creator",
                        "hitl_gate_config": {
                            "human_only": False,
                            "gate_id": "review_before_pr",
                            "overdue_threshold_minutes": 60,
                        },
                    },
                ],
                "entry": "issue-reader",
            },
            private_key=_registry_private,
        ),
    ]
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_registry_primitives(
    *,
    author: str | None = None,
    primitive_type: str | None = None,
    search: str | None = None,
) -> list[RegistryEntry]:
    """List all published primitives in the registry, with optional filters."""
    results = list(_BUILTIN_REGISTRY.values())
    if author:
        results = [e for e in results if e.author == author]
    if primitive_type:
        results = [e for e in results if e.primitive_type == primitive_type]
    if search:
        term = search.lower()
        results = [e for e in results if term in e.name.lower() or term in e.description.lower()]
    return results


def get_registry_primitive(slug: str) -> RegistryEntry | None:
    """Return a single primitive by its ``author/name`` slug."""
    return _BUILTIN_REGISTRY.get(slug)


def _validate_registry_inputs(
    author: str, name: str, primitive_type: str, tags: list[str], content_json: dict[str, Any],
) -> None:
    """Validate registry metadata inputs and content serializability."""
    if not author or not author.strip():
        raise ValueError("author must be a non-empty string")
    if not name or not name.strip():
        raise ValueError("name must be a non-empty string")
    if not primitive_type or not primitive_type.strip():
        raise ValueError("primitive_type must be a non-empty string")
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise ValueError("tags must be a list of strings")
    if not isinstance(content_json, dict):
        raise ValueError("content_json must be a dict")
    try:
        _canonical_json(content_json)
    except ValueError as exc:
        raise ValueError(f"content_json is not JSON-serializable: {exc}") from None


def publish_primitive(
    author: str,
    name: str,
    primitive_type: str,
    description: str,
    tags: list[str],
    content_json: dict[str, Any],
    signing_key_hex: str,
    *,
    version: str = "1.0",
) -> RegistryEntry:
    """Publish a new primitive to the registry (in-memory for alpha).

    In production this would POST to the hosted Modulo registry API.
    The signing key must correspond to the author's registered key.
    """
    _validate_registry_inputs(author, name, primitive_type, tags, content_json)

    try:
        private_bytes = bytes.fromhex(signing_key_hex)
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    except ValueError:
        raise ValueError("invalid signing key hex") from None

    slug = f"{author}/{name}"
    if slug in _BUILTIN_REGISTRY:
        logger.info("publish_primitive: overwriting existing entry %r", slug)

    entry = _build_entry(
        author=author,
        name=name,
        primitive_type=primitive_type,
        description=description,
        tags=tags,
        content_json=content_json,
        private_key=private_key,
        version=version,
    )
    _BUILTIN_REGISTRY[entry.slug] = entry
    return entry


def resolve_namespaced_slug(slug: str) -> tuple[str, str]:
    """Split ``author/name`` into (author, name).

    If no slash is present, default to the ``modulo`` author.
    Returns ``("modulo", "")`` for empty slugs.
    """
    if not slug:
        logger.warning("resolve_namespaced_slug: empty slug")
        return "modulo", ""
    if "/" not in slug:
        return "modulo", slug
    parts = slug.split("/", 1)
    if not parts[0]:
        logger.warning("resolve_namespaced_slug: empty author in slug %r, defaulting to modulo", slug)
        return "modulo", parts[1]
    if not parts[1]:
        logger.warning("resolve_namespaced_slug: empty name in slug %r", slug)
        return parts[0], ""
    return parts[0], parts[1]


def verify_primitive_signature(
    entry: RegistryEntry,
    public_key: Ed25519PublicKey | None = None,
) -> bool:
    """Verify the Ed25519 signature on a registry entry.

    If a public_key is provided, uses it directly.
    Otherwise falls back to the built-in registry development key.
    """
    if public_key is not None:
        pubkey = public_key
    elif entry.signing_key_fingerprint == _registry_fingerprint:
        pubkey = _registry_public
    else:
        logger.warning(
            "No public_key provided and fingerprint %s does not match registry key",
            entry.signing_key_fingerprint,
        )
        return False

    payload = _entry_payload(
        entry.author, entry.name, entry.version,
        entry.primitive_type, entry.description, entry.tags, entry.content_json,
    )
    return verify_manifest(payload, entry.ed25519_signature_hex, pubkey)


# ---------------------------------------------------------------------------
# Publisher trust model
# ---------------------------------------------------------------------------

PUBLISHER_TRUST_VERIFIED = "verified"
PUBLISHER_TRUST_COMMUNITY = "community"
PUBLISHER_TRUST_REVOKED = "revoked"


@dataclass
class Publisher:
    """A registered publisher with Ed25519 signing key."""

    author: str
    name: str
    fingerprint: str
    status: str = PUBLISHER_TRUST_VERIFIED  # verified | community | revoked
    website: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


_publishers: dict[str, Publisher] = {
    _registry_fingerprint: Publisher(
        author="modulo",
        name="Modulo Team",
        fingerprint=_registry_fingerprint,
        status=PUBLISHER_TRUST_VERIFIED,
        website="https://modulo.ai",
    ),
}


def register_publisher(
    fingerprint_hex: str,
    author: str,
    name: str,
    website: str = "",
) -> Publisher:
    """Register a verified publisher."""
    pub = Publisher(
        author=author,
        name=name,
        fingerprint=fingerprint_hex,
        status=PUBLISHER_TRUST_VERIFIED,
        website=website,
    )
    _publishers[fingerprint_hex] = pub
    return pub


def revoke_publisher(fingerprint_hex: str) -> bool:
    """Revoke a publisher's trust status."""
    pub = _publishers.get(fingerprint_hex)
    if pub is None:
        logger.warning("revoke_publisher: fingerprint %s not found", fingerprint_hex)
        return False
    if pub.status == PUBLISHER_TRUST_REVOKED:
        logger.warning("revoke_publisher: fingerprint %s already revoked", fingerprint_hex)
    pub.status = PUBLISHER_TRUST_REVOKED
    return True


def get_publisher_status(fingerprint_hex: str) -> str:
    """Return the trust status for a signing key fingerprint.

    Returns ``verified``, ``community``, or ``revoked``.
    """
    pub = _publishers.get(fingerprint_hex)
    if pub is None:
        return PUBLISHER_TRUST_COMMUNITY
    return pub.status


def get_publisher(fingerprint_hex: str) -> Publisher | None:
    return _publishers.get(fingerprint_hex)


def list_verified_publishers() -> list[Publisher]:
    return [p for p in _publishers.values() if p.status == PUBLISHER_TRUST_VERIFIED]


# ---------------------------------------------------------------------------
# Search ranking
# ---------------------------------------------------------------------------


_DOWNLOAD_WEIGHT = 0.4
_RATING_WEIGHT = 0.4
_RECENCY_WEIGHT = 0.2
_REVIEW_BONUS_MAX = 0.05
_RECENCY_DAYS = 90.0
_DOWNLOAD_SCALE = 1000.0
_MAX_DOWNLOAD_SCORE = 10.0
_MAX_RATING = 5.0


def compute_popularity_score(
    download_count: int,
    average_rating: float | None,
    review_count: int,
    published_at: datetime,
) -> float:
    """Compute a simple popularity score for ranking.

    Factors: downloads (40%), rating (40%), recency (20%).
    Plus a review-count bonus (up to +0.05).
    """
    now = datetime.now(UTC)
    days_since_publish = max((now - published_at).days, 1)

    # Downloads: log-scaled to avoid runaway values
    download_score = min(download_count / _DOWNLOAD_SCALE, _MAX_DOWNLOAD_SCORE) / _MAX_DOWNLOAD_SCORE * _DOWNLOAD_WEIGHT

    # Rating: 0-5 scale mapped to 0.0-0.4
    rating_val = max(average_rating if average_rating is not None else 0.0, 0.0)
    rating_score = min(rating_val, _MAX_RATING) / _MAX_RATING * _RATING_WEIGHT

    # Recency: decay over RECENCY_DAYS days
    recency_score = max(1.0 - (days_since_publish / _RECENCY_DAYS), 0.0) * _RECENCY_WEIGHT

    # Review count bonus: small bump for having reviews
    review_bonus = min(review_count / 10.0, 1.0) * _REVIEW_BONUS_MAX

    return download_score + rating_score + recency_score + review_bonus


def list_registry_primitives_ranked(
    *,
    author: str | None = None,
    primitive_type: str | None = None,
    search: str | None = None,
    sort_by: str = "popularity",  # popularity | recent | downloads | rating
) -> list[dict[str, Any]]:
    """List primitives with computed scores and publisher trust status.

    Each result dict includes the entry data plus:
      - publisher_status: verified | community | revoked
      - popularity_score: float
    """
    results = list_registry_primitives(
        author=author,
        primitive_type=primitive_type,
        search=search,
    )

    enriched: list[dict[str, Any]] = []
    for e in results:
        status = get_publisher_status(e.signing_key_fingerprint)
        publisher = get_publisher(e.signing_key_fingerprint)
        score = compute_popularity_score(
            download_count=e.download_count,
            average_rating=None,
            review_count=0,
            published_at=e.published_at,
        )
        enriched.append(
            {
                "entry": e,
                "publisher_status": status,
                "publisher_name": publisher.name if publisher else e.author,
                "popularity_score": round(score, 4),
            },
        )

    if sort_by == "recent":
        enriched.sort(key=lambda x: x["entry"].published_at, reverse=True)
    elif sort_by == "downloads":
        enriched.sort(key=lambda x: x["entry"].download_count, reverse=True)
    else:
        if sort_by != "popularity":
            logger.warning("Unknown sort_by=%r, falling back to popularity", sort_by)
        enriched.sort(key=lambda x: x["popularity_score"], reverse=True)

    return enriched

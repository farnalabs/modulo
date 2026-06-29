"""Step definitions for community registry features — browse, publish, pull, verify, publishers."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/library/community_registry.feature")

_SLUG = "modulo/prd-input-schema"

_MOCK_ENTRY = {
    "author": "modulo",
    "name": "prd-input-schema",
    "slug": _SLUG,
    "version": "1.0",
    "primitive_type": "schema",
    "description": "Input schema for a product requirements document.",
    "tags": ["schema", "product", "prd"],
    "content_json": {
        "fields": [
            {"name": "title", "type": "string", "required": True},
        ]
    },
    "checksum_sha256": "abc123",
    "ed25519_signature_hex": "ab" * 32,
    "signing_key_fingerprint": "abcdef1234567890",
    "publisher_status": "verified",
    "published_at": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
    "download_count": 5,
}

_MOCK_RANKED_ITEMS = [
    {
        "entry": _MOCK_ENTRY,
        "publisher_status": "verified",
        "publisher_name": "Modulo Team",
        "popularity_score": 0.75,
    },
    {
        "entry": {
            **_MOCK_ENTRY,
            "slug": "modulo/requirements-output-schema",
            "name": "requirements-output-schema",
            "description": "Structured requirements.",
        },
        "publisher_status": "verified",
        "publisher_name": "Modulo Team",
        "popularity_score": 0.50,
    },
]


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {
        "response": None,
        "publishers": {},
    }


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('a user in org "{org}" downloads "{slug}"'))
def _user_in_org_downloads(ctx: dict[str, Any], org: str, slug: str) -> None:
    ctx["download_org"] = org
    ctx["download_slug"] = slug


@given(parsers.parse('the registry has a publisher "{author}" with key "{key}"'))
def _registry_has_publisher(author: str, key: str) -> None:
    from modulo.core.registry import register_publisher
    register_publisher(fingerprint_hex=key, author=author, name=author.capitalize())


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse("the user requests GET {path}"))
def _request_get(client, path: str, ctx: dict[str, Any]) -> None:
    ctx["response"] = client.get(path)


@when(parsers.parse("the user sends POST {path}"))
def _request_post_body(client, path: str, ctx: dict[str, Any]) -> None:
    ctx["response"] = client.post(path)


@when(parsers.parse('the user publishes a v1 primitive as "{slug}"'))
def _publish_v1_primitive(client, slug: str, ctx: dict[str, Any]) -> None:
    author, name = slug.split("/", 1)
    payload = {
        "author": author,
        "name": name,
        "primitive_type": "workflow",
        "description": "A test workflow",
        "tags": ["test"],
        "content_json": {"nodes": [], "edges": [], "entry": "start"},
        "signing_key_hex": "a" * 64,
    }
    ctx["response"] = client.post("/api/v1/registry/primitives", json=payload)


@when(parsers.parse('the user publishes a signed v2 primitive as "{slug}"'))
def _publish_signed_v2_primitive(client, slug: str, ctx: dict[str, Any]) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    public = private.public_key()

    from cryptography.hazmat.primitives import serialization
    public_key_pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    author, name = slug.split("/", 1)
    payload_fields = {
        "author": author,
        "name": name,
        "primitive_type": "workflow",
        "description": "Signed workflow",
        "tags": ["signed"],
        "content_json": {"nodes": [], "entry": "start"},
    }

    import json
    payload_bytes = json.dumps(
        {
            "author": payload_fields["author"],
            "name": payload_fields["name"],
            "primitive_type": payload_fields["primitive_type"],
            "description": payload_fields.get("description", ""),
            "tags": payload_fields.get("tags", []),
            "content_json": payload_fields.get("content_json", {}),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    sig = private.sign(payload_bytes)
    import base64
    signature_b64 = base64.b64encode(sig).decode()

    request_body = {
        **payload_fields,
        "signature": signature_b64,
        "public_key_pem": public_key_pem,
    }

    ctx["response"] = client.post("/api/v1/registry/publish", json=request_body)


@when(parsers.parse('the user downloads registry primitive "{slug}"'))
def _download_registry_primitive(client, slug: str, ctx: dict[str, Any]) -> None:
    ctx["response"] = client.post(f"/api/v1/registry/primitives/{slug}/download")


@when(parsers.parse('the user registers a publisher "{author}" with key "{key}"'))
def _register_publisher(client, author: str, key: str, ctx: dict[str, Any]) -> None:
    payload = {
        "fingerprint_hex": key,
        "author": author,
        "name": author.capitalize(),
        "website": f"https://{author}.example.com",
    }
    ctx["response"] = client.post("/api/v1/registry/publishers", json=payload)


@when(parsers.parse("the {org} org requests GET {path}"))
def _org_requests_get(client, alt_org_client, org: str, path: str, ctx: dict[str, Any]) -> None:
    c = alt_org_client if org != "acme" else client
    ctx["response"] = c.get(path)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the response status is 200")
def _response_status_200(ctx: dict[str, Any]) -> None:
    assert ctx["response"].status_code == 200, (
        f"Expected 200, got {ctx['response'].status_code}: {ctx['response'].text}"
    )


@then("the response status is 201")
def _response_status_201(ctx: dict[str, Any]) -> None:
    assert ctx["response"].status_code == 201, (
        f"Expected 201, got {ctx['response'].status_code}: {ctx['response'].text}"
    )


@then("the response status is 404")
def _response_status_404(ctx: dict[str, Any]) -> None:
    assert ctx["response"].status_code == 404, (
        f"Expected 404, got {ctx['response'].status_code}: {ctx['response'].text}"
    )


@then(parsers.parse('the response body has key "{key}" with an array'))
def _response_has_key_array(ctx: dict[str, Any], key: str) -> None:
    data = ctx["response"].json()
    assert key in data, f"Missing key '{key}'"
    assert isinstance(data[key], list), f"Key '{key}' should be a list"


@then(parsers.parse('the response body has key "{key}"'))
def _response_has_key(ctx: dict[str, Any], key: str) -> None:
    data = ctx["response"].json()
    assert key in data, f"Missing key '{key}' in response"


@then("each item has entry, publisher_status, publisher_name, and popularity_score")
def _each_item_has_fields(ctx: dict[str, Any]) -> None:
    items = ctx["response"].json()["items"]
    for item in items:
        assert "entry" in item, "Missing entry"
        assert "publisher_status" in item, "Missing publisher_status"
        assert "publisher_name" in item, "Missing publisher_name"
        assert "popularity_score" in item, "Missing popularity_score"


@then(parsers.parse('each returned entry matches the search term "{term}"'))
def _each_entry_matches_search(ctx: dict[str, Any], term: str) -> None:
    items = ctx["response"].json()["items"]
    term_lower = term.lower()
    for item in items:
        entry = item["entry"] if "entry" in item else item
        match = term_lower in (entry.get("name", "") or "").lower() or term_lower in (
            entry.get("description", "") or ""
        ).lower()
        assert match, f"No match for '{term_lower}' in {entry.get('name')} / {entry.get('description')}"


@then(parsers.parse('the entry has slug "{slug}"'))
def _entry_has_slug(ctx: dict[str, Any], slug: str) -> None:
    data = ctx["response"].json()
    entry = data.get("entry", data)
    assert entry.get("slug") == slug, f"Expected slug '{slug}', got '{entry.get('slug')}'"


@then("the slug matches the author and name")
def _slug_matches_author_name(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    slug = data.get("slug", "")
    assert "/" in slug, f"Slug '{slug}' does not contain author/name separator"


@then(parsers.parse('the response body has key "{key}" with value "{value}"'))
def _response_has_key_value(ctx: dict[str, Any], key: str, value: str) -> None:
    data = ctx["response"].json()
    assert str(data.get(key)) == value, f"Expected '{key}' = '{value}', got '{data.get(key)}'"


@then(parsers.parse('the response body has status "{status}"'))
def _response_has_status(ctx: dict[str, Any], status: str) -> None:
    data = ctx["response"].json()
    assert data.get("status") == status, f"Expected status '{status}', got '{data.get('status')}'"


@then(parsers.parse('the response contains no primitives from org "{org}"'))
def _response_no_primitives_from_org(ctx: dict[str, Any], org: str) -> None:
    data = ctx["response"].json()
    items = data.get("items", [])
    for item in items:
        slug = item.get("slug", "")
        assert not slug.startswith(org), f"Found primitive from org '{org}': {slug}"

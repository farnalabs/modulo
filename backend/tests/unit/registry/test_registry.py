"""Unit tests for the registry service — signing, integrity, CRUD."""

import copy

import pytest

from modulo.core.registry import (
    _BUILTIN_REGISTRY,
    _canonical_json,
    _sha256_digest,
    compute_bundle_hash,
    fingerprint,
    generate_signing_key,
    get_registry_primitive,
    list_registry_primitives,
    publish_primitive,
    resolve_namespaced_slug,
    sign_manifest,
    verify_bundle_integrity,
    verify_manifest,
    verify_primitive_signature,
)


class TestCrypto:
    def test_generate_key_and_fingerprint(self):
        _private, public = generate_signing_key()
        fp = fingerprint(public)
        assert len(fp) == 16
        assert isinstance(fp, str)

    def test_sign_and_verify_roundtrip(self):
        private, public = generate_signing_key()
        payload = {"hello": "world", "nested": [1, 2, 3]}
        sig = sign_manifest(payload, private)
        assert verify_manifest(payload, sig, public) is True

    def test_verify_rejects_tampered_payload(self):
        private, public = generate_signing_key()
        sig = sign_manifest({"a": 1}, private)
        assert verify_manifest({"a": 2}, sig, public) is False

    def test_verify_rejects_wrong_key(self):
        private, _ = generate_signing_key()
        _, wrong_public = generate_signing_key()
        sig = sign_manifest({"a": 1}, private)
        assert verify_manifest({"a": 1}, sig, wrong_public) is False

    def test_canonical_json_stable(self):
        a = _canonical_json({"b": 2, "a": 1})
        b = _canonical_json({"a": 1, "b": 2})
        assert a == b
        assert b'"a":1,"b":2' in a

    def test_sha256_digest(self):
        h = _sha256_digest(b"hello")
        assert len(h) == 64
        assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


class TestBundleIntegrity:
    def test_compute_bundle_hash(self):
        bundle = {"pipeline": {"name": "test"}}
        h = compute_bundle_hash(bundle)
        assert len(h) == 64

    def test_verify_bundle_integrity_match(self):
        bundle = {"key": "value"}
        h = compute_bundle_hash(bundle)
        assert verify_bundle_integrity(bundle, h) is True

    def test_verify_bundle_integrity_mismatch(self):
        bundle = {"key": "value"}
        h = compute_bundle_hash({"key": "different"})
        assert verify_bundle_integrity(bundle, h) is False


class TestRegistryCRUD:
    def test_list_all_primitives(self):
        results = list_registry_primitives()
        assert len(results) == 9  # 3 original + 6 dogfood

    def test_list_filter_by_type(self):
        results = list_registry_primitives(primitive_type="schema")
        assert all(r.primitive_type == "schema" for r in results)

    def test_list_filter_by_author(self):
        results = list_registry_primitives(author="modulo")
        assert len(results) > 0
        assert all(r.author == "modulo" for r in results)

    def test_list_search(self):
        results = list_registry_primitives(search="prd")
        assert len(results) > 0

    def test_get_by_slug_found(self):
        entry = get_registry_primitive("modulo/prd-input-schema")
        assert entry is not None
        assert entry.primitive_type == "schema"

    def test_get_by_slug_not_found(self):
        entry = get_registry_primitive("nonexistent/foo")
        assert entry is None

    def test_resolve_namespaced_slug_with_author(self):
        author, name = resolve_namespaced_slug("modulo/prd-input-schema")
        assert author == "modulo"
        assert name == "prd-input-schema"

    def test_resolve_namespaced_slug_default_author(self):
        author, name = resolve_namespaced_slug("my-schema")
        assert author == "modulo"
        assert name == "my-schema"

    def test_resolve_namespaced_slug_custom_author(self):
        author, name = resolve_namespaced_slug("community/awesome-schema")
        assert author == "community"
        assert name == "awesome-schema"

    def test_builtin_primitives_have_valid_signatures(self):
        for slug, entry in _BUILTIN_REGISTRY.items():
            assert verify_primitive_signature(entry), f"Signature check failed for {slug}"
            assert len(entry.ed25519_signature_hex) > 0

    def test_builtin_primitives_have_checksums(self):
        for entry in _BUILTIN_REGISTRY.values():
            assert len(entry.checksum_sha256) == 64

    def test_entry_download_count_starts_at_zero(self):
        entry = get_registry_primitive("modulo/prd-input-schema")
        assert entry is not None
        assert entry.download_count == 0

    def test_dogfood_registry_entries_exist(self):
        dogfood_slugs = [
            "modulo/github-issue-input-schema",
            "modulo/structured-requirements-schema",
            "modulo/code-diff-output-schema",
            "modulo/test-result-output-schema",
            "modulo/pr-output-schema",
            "modulo/modulo-dogfood-pipeline",
        ]
        for slug in dogfood_slugs:
            entry = get_registry_primitive(slug)
            assert entry is not None, f"Missing registry entry: {slug}"

    def test_dogfood_registry_primitives_have_dogfood_reference(self):
        results = list_registry_primitives(search="dogfood")
        assert len(results) == 2  # github-issue-input-schema (desc) + modulo-dogfood-pipeline (name)

    def test_dogfood_registry_workflow_has_hitl_gate(self):
        entry = get_registry_primitive("modulo/modulo-dogfood-pipeline")
        assert entry is not None
        edges = entry.content_json["edges"]
        hitl_edge = edges[3]
        assert "hitl_gate_config" in hitl_edge
        assert hitl_edge["hitl_gate_config"]["gate_id"] == "review_before_pr"

    def test_registry_total_count(self):
        results = list_registry_primitives()
        assert len(results) == 9  # 3 original + 6 dogfood


class _PreserveRegistry:
    """Fixture that snapshots and restores _BUILTIN_REGISTRY around each test."""

    @pytest.fixture(autouse=True)
    def _preserve_registry(self):
        saved = copy.deepcopy(_BUILTIN_REGISTRY)
        yield
        _BUILTIN_REGISTRY.clear()
        _BUILTIN_REGISTRY.update(saved)


class TestPublish(_PreserveRegistry):
    async def test_publish_new_primitive(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        original_count = len(_BUILTIN_REGISTRY)

        entry = await publish_primitive(
            author="test-author",
            name="test-primitive",
            primitive_type="schema",
            description="A test schema",
            tags=["test"],
            content_json={"fields": [{"name": "x", "type": "string"}]},
            signing_key_hex=key_hex,
        )

        assert entry.author == "test-author"
        assert entry.name == "test-primitive"
        assert len(_BUILTIN_REGISTRY) == original_count + 1

    async def test_published_primitive_has_valid_signature(self):
        private, public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()

        entry = await publish_primitive(
            author="sig-test",
            name="sig-check",
            primitive_type="agent",
            description="Checking signatures",
            tags=[],
            content_json={},
            signing_key_hex=key_hex,
        )

        assert verify_primitive_signature(entry, public_key=public)

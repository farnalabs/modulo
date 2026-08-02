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

    @pytest.mark.parametrize(
        "tamper,expected",
        [
            (None, True),
            (lambda p: {"a": 2}, False),
            ("wrong_key", False),
        ],
    )
    def test_sign_and_verify(self, tamper, expected):
        private, public = generate_signing_key()
        payload = {"hello": "world"}
        sig = sign_manifest(payload, private)
        if tamper == "wrong_key":
            _, wrong_public = generate_signing_key()
            assert verify_manifest(payload, sig, wrong_public) is expected
        elif tamper:
            assert verify_manifest(tamper(payload), sig, public) is expected
        else:
            assert verify_manifest(payload, sig, public) is expected

    def test_canonical_json_stable(self):
        a = _canonical_json({"b": 2, "a": 1})
        b = _canonical_json({"a": 1, "b": 2})
        assert a == b
        assert b'"a":1,"b":2' in a

    def test_sha256_digest(self):
        h = _sha256_digest(b"hello")
        assert len(h) == 64
        assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    def test_sign_manifest_rejects_non_serializable_payload(self):
        private, _public = generate_signing_key()
        with pytest.raises(ValueError, match="non-serializable"):
            sign_manifest({"payload": object()}, private)

    def test_verify_manifest_non_serializable_payload_returns_false(self):
        _private, public = generate_signing_key()
        assert verify_manifest({"payload": object()}, "00" * 64, public) is False

    def test_verify_manifest_bad_signature_hex_returns_false(self):
        private, public = generate_signing_key()
        payload = {"hello": "world"}
        sign_manifest(payload, private)
        assert verify_manifest(payload, "zz-not-hex", public) is False

    def test_verify_manifest_tampered_signature_returns_false(self):
        private, public = generate_signing_key()
        payload = {"hello": "world"}
        sig = sign_manifest(payload, private)
        tampered = list(sig)
        tampered[0] = "f" if tampered[0] != "f" else "0"
        assert verify_manifest(payload, "".join(tampered), public) is False

    def test_compute_bundle_hash_rejects_non_serializable_bundle(self):
        with pytest.raises(ValueError, match="non-serializable"):
            compute_bundle_hash({"payload": object()})


class TestBundleIntegrity:
    @pytest.mark.parametrize(
        "bundle,hash_bundle,expected",
        [
            ({"pipeline": {"name": "test"}}, None, True),
            ({"key": "value"}, {"key": "value"}, True),
            ({"key": "value"}, {"key": "different"}, False),
        ],
    )
    def test_bundle_integrity(self, bundle, hash_bundle, expected):
        if hash_bundle is None:
            h = compute_bundle_hash(bundle)
            assert len(h) == 64
        else:
            h = compute_bundle_hash(hash_bundle)
            assert verify_bundle_integrity(bundle, h) is expected


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

    @pytest.mark.parametrize(
        "slug,expected_author,expected_name",
        [
            ("modulo/prd-input-schema", "modulo", "prd-input-schema"),
            ("my-schema", "modulo", "my-schema"),
            ("community/awesome-schema", "community", "awesome-schema"),
        ],
    )
    def test_resolve_namespaced_slug(self, slug, expected_author, expected_name):
        author, name = resolve_namespaced_slug(slug)
        assert author == expected_author
        assert name == expected_name

    def test_resolve_namespaced_slug_empty_defaults_to_modulo(self, caplog):
        author, name = resolve_namespaced_slug("")
        assert (author, name) == ("modulo", "")
        assert "empty slug" in caplog.text

    def test_resolve_namespaced_slug_empty_author_defaults_to_modulo(self, caplog):
        author, name = resolve_namespaced_slug("/name-only")
        assert (author, name) == ("modulo", "name-only")
        assert "empty author" in caplog.text

    def test_resolve_namespaced_slug_empty_name_keeps_author(self, caplog):
        author, name = resolve_namespaced_slug("author/")
        assert (author, name) == ("author", "")
        assert "empty name" in caplog.text

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

        entry = publish_primitive(
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

        entry = publish_primitive(
            author="sig-test",
            name="sig-check",
            primitive_type="agent",
            description="Checking signatures",
            tags=[],
            content_json={},
            signing_key_hex=key_hex,
        )

        assert verify_primitive_signature(entry, public_key=public)

    async def test_publish_rejects_empty_author(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        with pytest.raises(ValueError, match="author must be a non-empty string"):
            publish_primitive(
                author="",
                name="x",
                primitive_type="schema",
                description="d",
                tags=[],
                content_json={},
                signing_key_hex=key_hex,
            )

    async def test_publish_rejects_empty_name(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        with pytest.raises(ValueError, match="name must be a non-empty string"):
            publish_primitive(
                author="a",
                name="  ",
                primitive_type="schema",
                description="d",
                tags=[],
                content_json={},
                signing_key_hex=key_hex,
            )

    async def test_publish_rejects_empty_primitive_type(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        with pytest.raises(ValueError, match="primitive_type must be a non-empty string"):
            publish_primitive(
                author="a",
                name="x",
                primitive_type="",
                description="d",
                tags=[],
                content_json={},
                signing_key_hex=key_hex,
            )

    async def test_publish_rejects_empty_description(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        with pytest.raises(ValueError, match="description must be a non-empty string"):
            publish_primitive(
                author="a",
                name="x",
                primitive_type="schema",
                description="",
                tags=[],
                content_json={},
                signing_key_hex=key_hex,
            )

    async def test_publish_rejects_non_list_tags(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        with pytest.raises(ValueError, match="tags must be a list of strings"):
            publish_primitive(
                author="a",
                name="x",
                primitive_type="schema",
                description="d",
                tags="t",
                content_json={},
                signing_key_hex=key_hex,
            )

    async def test_publish_rejects_non_string_tag(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        with pytest.raises(ValueError, match="tags must be a list of strings"):
            publish_primitive(
                author="a",
                name="x",
                primitive_type="schema",
                description="d",
                tags=[1],
                content_json={},
                signing_key_hex=key_hex,
            )

    async def test_publish_rejects_non_dict_content_json(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        with pytest.raises(ValueError, match="content_json must be a dict"):
            publish_primitive(
                author="a",
                name="x",
                primitive_type="schema",
                description="d",
                tags=[],
                content_json=[],
                signing_key_hex=key_hex,
            )

    async def test_publish_rejects_non_serializable_content_json(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        with pytest.raises(ValueError, match="not JSON-serializable"):
            publish_primitive(
                author="a",
                name="x",
                primitive_type="schema",
                description="d",
                tags=[],
                content_json={"x": object()},
                signing_key_hex=key_hex,
            )

    async def test_publish_rejects_invalid_signing_key_hex(self):
        with pytest.raises(ValueError, match="invalid signing key hex"):
            publish_primitive(
                author="a",
                name="x",
                primitive_type="schema",
                description="d",
                tags=[],
                content_json={},
                signing_key_hex="not-hex",
            )

    async def test_publish_overwrites_existing_slug(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        original_count = len(_BUILTIN_REGISTRY)

        publish_primitive(
            author="ow",
            name="prim",
            primitive_type="schema",
            description="v1",
            tags=[],
            content_json={"a": 1},
            signing_key_hex=key_hex,
        )
        republished = publish_primitive(
            author="ow",
            name="prim",
            primitive_type="schema",
            description="v2",
            tags=[],
            content_json={"a": 2},
            signing_key_hex=key_hex,
        )

        assert len(_BUILTIN_REGISTRY) == original_count + 1
        assert republished.description == "v2"
        assert republished.content_json == {"a": 2}

    async def test_publish_unknown_fingerprint_without_key_returns_false(self):
        private, _public = generate_signing_key()
        key_hex = private.private_bytes_raw().hex()
        entry = publish_primitive(
            author="unknown-fp",
            name="prim",
            primitive_type="schema",
            description="d",
            tags=[],
            content_json={},
            signing_key_hex=key_hex,
        )
        entry.signing_key_fingerprint = "0000000000000000"

        assert verify_primitive_signature(entry) is False

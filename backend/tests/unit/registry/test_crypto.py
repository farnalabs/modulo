"""Unit tests for the registry protocol v2 crypto module."""

import copy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from modulo.core.registry import _BUILTIN_REGISTRY, verify_primitive_signature
from modulo.core.registry.crypto import (
    generate_keypair,
    sign_primitive,
    verify_signature,
)


class TestCryptoV2:
    def test_generate_keypair_returns_hex_keys(self):
        kp = generate_keypair()
        assert "private_key" in kp
        assert "public_key" in kp
        assert "fingerprint" in kp
        assert len(kp["private_key"]) == 64  # 32 bytes = 64 hex chars
        assert len(kp["public_key"]) == 64  # 32 bytes = 64 hex chars
        assert len(kp["fingerprint"]) == 16  # sha256[:16]

    def test_generate_keypair_different_each_call(self):
        kp1 = generate_keypair()
        kp2 = generate_keypair()
        assert kp1["private_key"] != kp2["private_key"]
        assert kp1["public_key"] != kp2["public_key"]

    def test_sign_and_verify_roundtrip(self):
        kp = generate_keypair()
        data = {"name": "test", "version": "1.0", "tags": ["a", "b"]}

        sig = sign_primitive(data, kp["private_key"])
        assert isinstance(sig, str)
        assert len(sig) == 128  # Ed25519 sig is 64 bytes = 128 hex chars

        assert verify_signature(data, sig, kp["public_key"]) is True

    def test_verify_rejects_tampered_data(self):
        kp = generate_keypair()
        data = {"key": "value"}

        sig = sign_primitive(data, kp["private_key"])

        tampered = {"key": "different"}
        assert verify_signature(tampered, sig, kp["public_key"]) is False

    def test_verify_rejects_wrong_key(self):
        kp1 = generate_keypair()
        kp2 = generate_keypair()
        data = {"msg": "hello"}

        sig = sign_primitive(data, kp1["private_key"])

        assert verify_signature(data, sig, kp2["public_key"]) is False

    def test_verify_rejects_tampered_signature(self):
        kp = generate_keypair()
        data = {"x": 1}

        sig = sign_primitive(data, kp["private_key"])

        # Flip one hex char in the signature
        tampered_sig = list(sig)
        tampered_sig[0] = "f" if tampered_sig[0] != "f" else "0"
        tampered_sig = "".join(tampered_sig)

        assert verify_signature(data, tampered_sig, kp["public_key"]) is False

    def test_sign_primitive_with_fingerprint_roundtrip(self):
        kp = generate_keypair()
        fp = kp["fingerprint"]

        # Re-derive fingerprint from public key hex
        import hashlib

        expected_fp = hashlib.sha256(bytes.fromhex(kp["public_key"])).hexdigest()[:16]
        assert fp == expected_fp

    def test_sign_primitive_rejects_invalid_private_key_hex(self):
        with pytest.raises(ValueError, match="invalid private key hex"):
            sign_primitive({"name": "test"}, "not-hex")

    def test_sign_primitive_rejects_non_serializable_data(self):
        kp = generate_keypair()
        with pytest.raises(ValueError, match="non-serializable"):
            sign_primitive({"name": "test", "payload": object()}, kp["private_key"])

    def test_verify_signature_rejects_invalid_public_key_hex(self):
        kp = generate_keypair()
        sig = sign_primitive({"name": "test"}, kp["private_key"])
        assert verify_signature({"name": "test"}, sig, "not-hex") is False

    def test_verify_signature_rejects_invalid_signature_hex(self):
        kp = generate_keypair()
        assert verify_signature({"name": "test"}, "not-hex", kp["public_key"]) is False

    def test_verify_signature_rejects_non_serializable_data(self):
        kp = generate_keypair()
        sig = sign_primitive({"name": "test"}, kp["private_key"])
        assert verify_signature({"name": "test", "payload": object()}, sig, kp["public_key"]) is False

    def test_verify_signature_empty_signature_returns_false(self):
        kp = generate_keypair()
        assert verify_signature({"name": "test"}, "", kp["public_key"]) is False

    def test_canonical_json_rejects_non_serializable_values(self):
        from modulo.core.registry.crypto import _canonical_json

        with pytest.raises(ValueError, match="non-serializable"):
            _canonical_json({"payload": object()})


class TestCryptoV2WithRegistry:
    """Verify that crypto.py can sign entries compatible with the existing registry."""

    def test_sign_and_verify_through_registry(self):
        kp = generate_keypair()
        data = {
            "author": "test-author",
            "name": "v2-primitive",
            "version": "1.0",
            "primitive_type": "schema",
            "description": "Created via v2 crypto",
            "tags": ["v2"],
            "content_json": {"fields": []},
        }

        sig = sign_primitive(data, kp["private_key"])

        assert verify_signature(data, sig, kp["public_key"]) is True


class _PreserveRegistry:
    @pytest.fixture(autouse=True)
    def _preserve_registry(self):
        saved = copy.deepcopy(_BUILTIN_REGISTRY)
        yield
        _BUILTIN_REGISTRY.clear()
        _BUILTIN_REGISTRY.update(saved)


class TestPublishPullVerifyFlow(_PreserveRegistry):
    """End-to-end tests for the v2 publish-pull-verify workflow via crypto module."""

    def test_full_publish_pull_verify_cycle(self):
        from modulo.core.registry import get_registry_primitive, publish_primitive

        kp = generate_keypair()
        data = {
            "author": "e2e-author",
            "name": "e2e-primitive",
            "version": "1.0",
            "primitive_type": "workflow",
            "description": "E2E test primitive",
            "tags": ["e2e"],
            "content_json": {"nodes": [], "edges": [], "entry": "start"},
        }

        sig = sign_primitive(data, kp["private_key"])
        assert verify_signature(data, sig, kp["public_key"]) is True

        entry = publish_primitive(
            author="e2e-author",
            name="e2e-primitive",
            primitive_type="workflow",
            description="E2E test primitive",
            tags=["e2e"],
            content_json={"nodes": [], "edges": [], "entry": "start"},
            signing_key_hex=kp["private_key"],
        )

        assert entry.slug == "e2e-author/e2e-primitive"
        assert entry.checksum_sha256 is not None
        assert entry.ed25519_signature_hex is not None

        pulled = get_registry_primitive("e2e-author/e2e-primitive")
        assert pulled is not None
        assert pulled.author == "e2e-author"

        public_key_obj = Ed25519PublicKey.from_public_bytes(bytes.fromhex(kp["public_key"]))
        verified = verify_primitive_signature(pulled, public_key=public_key_obj)
        assert verified is True


class TestPEMCryptoV2:
    """Tests for the PEM/base64 Ed25519 crypto module."""

    def test_generate_keypair_returns_pem_strings(self):
        from modulo.registry.crypto import generate_keypair

        priv_pem, pub_pem = generate_keypair()
        assert priv_pem.startswith("-----BEGIN PRIVATE KEY-----")
        assert pub_pem.startswith("-----BEGIN PUBLIC KEY-----")

    def test_generate_keypair_different_each_call(self):
        from modulo.registry.crypto import generate_keypair

        kp1 = generate_keypair()
        kp2 = generate_keypair()
        assert kp1 != kp2

    def test_sign_and_verify_roundtrip(self):
        from modulo.registry.crypto import generate_keypair, sign, verify

        priv_pem, pub_pem = generate_keypair()
        data = b"hello world"
        sig = sign(priv_pem, data)
        assert isinstance(sig, str)
        assert len(sig) > 0
        assert verify(pub_pem, data, sig) is True

    def test_verify_rejects_tampered_data(self):
        from modulo.registry.crypto import generate_keypair, sign, verify

        priv_pem, pub_pem = generate_keypair()
        data = b"original data"
        sig = sign(priv_pem, data)
        assert verify(pub_pem, b"tampered data", sig) is False

    def test_verify_rejects_wrong_key(self):
        from modulo.registry.crypto import generate_keypair, sign, verify

        priv1, _ = generate_keypair()
        _, pub2 = generate_keypair()
        data = b"shared secret"
        sig = sign(priv1, data)
        assert verify(pub2, data, sig) is False

    def test_verify_rejects_wrong_signature(self):
        from modulo.registry.crypto import generate_keypair, sign, verify

        priv_pem, pub_pem = generate_keypair()
        data = b"test"
        sign(priv_pem, data)
        import base64

        mangled = base64.b64encode(b"\x00" * 64).decode()
        assert verify(pub_pem, data, mangled) is False

    def test_verify_rejects_invalid_base64_signature(self):
        from modulo.registry.crypto import generate_keypair, verify

        _, pub_pem = generate_keypair()
        assert verify(pub_pem, b"data", "!!!not-base64!!!") is False

    def test_verify_rejects_garbage_signature_string(self):
        from modulo.registry.crypto import generate_keypair, verify

        _, pub_pem = generate_keypair()
        assert verify(pub_pem, b"data", "\x00\x01\x02\xff") is False

    def test_sign_rejects_non_ed25519_private_key(self):
        from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

        from modulo.registry.crypto import sign

        rsa_priv = generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = rsa_priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        with pytest.raises(TypeError, match="not an Ed25519 private key"):
            sign(priv_pem, b"data")

    def test_sign_rejects_malformed_pem(self):
        from modulo.registry.crypto import sign

        with pytest.raises(ValueError, match=r"(Could not deserialize key data|Unable to load PEM file)"):
            sign("-----BEGIN PRIVATE KEY-----\ngarbage\n-----END PRIVATE KEY-----", b"data")

    def test_verify_rejects_non_ed25519_public_key(self):
        from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

        from modulo.registry.crypto import generate_keypair, sign, verify

        rsa_priv = generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = (
            rsa_priv.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )

        priv_pem, _ = generate_keypair()
        sig = sign(priv_pem, b"data")
        assert verify(pub_pem, b"data", sig) is False

    def test_verify_rejects_malformed_public_key_pem(self):
        from modulo.registry.crypto import generate_keypair, sign, verify

        priv_pem, _ = generate_keypair()
        sig = sign(priv_pem, b"data")
        assert verify("-----BEGIN PUBLIC KEY-----\ngarbage\n-----END PUBLIC KEY-----", b"data", sig) is False

    def test_sign_and_verify_json_payload(self):
        import json

        from modulo.registry.crypto import generate_keypair, sign, verify

        priv_pem, pub_pem = generate_keypair()
        payload = json.dumps({"author": "test", "version": "1.0"}, separators=(",", ":"), sort_keys=True).encode()
        sig = sign(priv_pem, payload)
        assert verify(pub_pem, payload, sig) is True


class TestTrustAnchor:
    """Tests for trust anchor verification."""

    def test_trust_anchor_public_key_is_pem(self):
        from modulo.registry.crypto import get_trust_anchor_public_key_pem

        pub_pem = get_trust_anchor_public_key_pem()
        assert pub_pem.startswith("-----BEGIN PUBLIC KEY-----")

    def test_sign_public_key_and_verify_roundtrip(self):
        from modulo.registry.crypto import (
            generate_keypair,
            sign_with_trust_anchor,
            verify_trust_anchor,
        )

        _, pub_pem = generate_keypair()
        ta_sig = sign_with_trust_anchor(pub_pem)
        assert isinstance(ta_sig, str)
        assert len(ta_sig) > 0
        assert verify_trust_anchor(pub_pem, ta_sig) is True

    def test_verify_trust_anchor_rejects_unsigned_key(self):
        from modulo.registry.crypto import (
            generate_keypair,
            sign_with_trust_anchor,
            verify_trust_anchor,
        )

        _, pub1 = generate_keypair()
        _, pub2 = generate_keypair()
        sig = sign_with_trust_anchor(pub1)
        assert verify_trust_anchor(pub2, sig) is False

    def test_verify_trust_anchor_with_explicit_key(self):
        from modulo.registry.crypto import (
            generate_keypair,
            get_trust_anchor_public_key_pem,
            sign_with_trust_anchor,
            verify_trust_anchor,
        )

        _, pub_pem = generate_keypair()
        ta_sig = sign_with_trust_anchor(pub_pem)
        ta_pub = get_trust_anchor_public_key_pem()
        assert verify_trust_anchor(pub_pem, ta_sig, ta_pub) is True

    def test_verify_trust_anchor_rejects_tampered_sig(self):
        import base64

        from modulo.registry.crypto import (
            generate_keypair,
            verify_trust_anchor,
        )

        _, pub_pem = generate_keypair()
        ta_sig = base64.b64encode(b"\x00" * 64).decode()
        assert verify_trust_anchor(pub_pem, ta_sig) is False

    def test_verify_trust_anchor_rejects_invalid_base64(self):
        from modulo.registry.crypto import (
            generate_keypair,
            verify_trust_anchor,
        )

        _, pub_pem = generate_keypair()
        assert verify_trust_anchor(pub_pem, "!!!not-base64!!!") is False

    def test_verify_trust_anchor_rejects_garbage_sig(self):
        from modulo.registry.crypto import (
            generate_keypair,
            verify_trust_anchor,
        )

        _, pub_pem = generate_keypair()
        assert verify_trust_anchor(pub_pem, "\x00\x01\x02\xff") is False

    def test_verify_trust_anchor_empty_anchor_returns_false(self):
        from modulo.registry.crypto import (
            generate_keypair,
            sign_with_trust_anchor,
            verify_trust_anchor,
        )

        _, pub_pem = generate_keypair()
        ta_sig = sign_with_trust_anchor(pub_pem)
        assert verify_trust_anchor(pub_pem, ta_sig, "") is False


class TestModuleExports:
    def test_public_exports(self):
        import modulo.registry.crypto

        expected = {
            "generate_keypair",
            "get_trust_anchor_public_key_pem",
            "sign",
            "sign_with_trust_anchor",
            "verify",
            "verify_trust_anchor",
        }
        actual = set(modulo.registry.crypto.__all__)
        assert actual == expected

    def test_private_functions_not_in_exports(self):
        import modulo.registry.crypto

        privates = {"_load_private_key", "_load_public_key", "_get_trust_anchor"}
        assert not privates & set(modulo.registry.crypto.__all__)

"""Unit tests for the registry protocol v2 crypto module."""

import base64
import copy
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from modulo.core.registry import _BUILTIN_REGISTRY, verify_primitive_signature
from modulo.core.registry.crypto import (
    generate_keypair,
    sign_primitive,
    verify_signature,
)
from modulo.registry import crypto as pem_crypto


def _rsa_keypair_pems() -> tuple[str, str]:
    """Return an RSA private/public PEM pair (non-Ed25519 key material)."""
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

    private_key = generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


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

    @pytest.mark.parametrize(
        "data",
        [
            {"name": "test", "version": "1.0", "tags": ["a", "b"]},
            {
                "author": "test-author",
                "name": "v2-primitive",
                "version": "1.0",
                "primitive_type": "schema",
                "description": "Created via v2 crypto",
                "tags": ["v2"],
                "content_json": {"fields": []},
            },
        ],
        ids=["flat-payload", "nested-registry-entry"],
    )
    def test_sign_and_verify_roundtrip(self, data):
        kp = generate_keypair()

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

    def test_generate_keypair_fingerprint_matches_sha256_derivation(self):
        kp = generate_keypair()
        fp = kp["fingerprint"]

        # Re-derive fingerprint from public key hex
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

    def test_verify_signature_rejects_short_public_key_hex(self):
        kp = generate_keypair()
        sig = sign_primitive({"name": "test"}, kp["private_key"])
        assert verify_signature({"name": "test"}, sig, "00" * 16) is False

    def test_sign_primitive_rejects_short_private_key_hex(self):
        with pytest.raises(ValueError, match="invalid private key hex"):
            sign_primitive({"name": "test"}, "00" * 16)

    def test_canonical_json_rejects_non_serializable_values(self):
        from modulo.core.registry.crypto import _canonical_json

        with pytest.raises(ValueError, match="non-serializable"):
            _canonical_json({"payload": object()})


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
        priv_pem, pub_pem = pem_crypto.generate_keypair()
        assert priv_pem.startswith("-----BEGIN PRIVATE KEY-----")
        assert pub_pem.startswith("-----BEGIN PUBLIC KEY-----")

    def test_generate_keypair_different_each_call(self):
        assert pem_crypto.generate_keypair() != pem_crypto.generate_keypair()

    def test_sign_and_verify_roundtrip(self):
        priv_pem, pub_pem = pem_crypto.generate_keypair()
        data = b"hello world"
        sig = pem_crypto.sign(priv_pem, data)
        assert isinstance(sig, str)
        assert len(sig) > 0
        assert pem_crypto.verify(pub_pem, data, sig) is True

    def test_verify_rejects_tampered_data(self):
        priv_pem, pub_pem = pem_crypto.generate_keypair()
        sig = pem_crypto.sign(priv_pem, b"original data")
        assert pem_crypto.verify(pub_pem, b"tampered data", sig) is False

    def test_verify_rejects_wrong_key(self):
        priv_pem, _ = pem_crypto.generate_keypair()
        _, other_pub_pem = pem_crypto.generate_keypair()
        data = b"shared secret"
        sig = pem_crypto.sign(priv_pem, data)
        assert pem_crypto.verify(other_pub_pem, data, sig) is False

    @pytest.mark.parametrize(
        "signature",
        [
            base64.b64encode(b"\x00" * 64).decode(),
            "!!!not-base64!!!",
            "\x00\x01\x02\xff",
            "AAAA",
        ],
        ids=["zeroed-64-bytes", "invalid-base64", "binary-garbage", "wrong-length-signature"],
    )
    def test_verify_rejects_malformed_signature(self, signature):
        _, pub_pem = pem_crypto.generate_keypair()
        assert pem_crypto.verify(pub_pem, b"data", signature) is False

    def test_sign_rejects_non_ed25519_private_key(self):
        rsa_priv_pem, _ = _rsa_keypair_pems()
        with pytest.raises(TypeError, match="not an Ed25519 private key"):
            pem_crypto.sign(rsa_priv_pem, b"data")

    def test_sign_rejects_malformed_pem(self):
        with pytest.raises(ValueError, match=r"(Could not deserialize key data|Unable to load PEM file)"):
            pem_crypto.sign("-----BEGIN PRIVATE KEY-----\ngarbage\n-----END PRIVATE KEY-----", b"data")

    def test_verify_rejects_non_ed25519_public_key(self):
        _, rsa_pub_pem = _rsa_keypair_pems()
        priv_pem, _ = pem_crypto.generate_keypair()
        sig = pem_crypto.sign(priv_pem, b"data")
        assert pem_crypto.verify(rsa_pub_pem, b"data", sig) is False

    def test_verify_rejects_malformed_public_key_pem(self):
        priv_pem, _ = pem_crypto.generate_keypair()
        sig = pem_crypto.sign(priv_pem, b"data")
        assert pem_crypto.verify("-----BEGIN PUBLIC KEY-----\ngarbage\n-----END PUBLIC KEY-----", b"data", sig) is False

    def test_sign_and_verify_json_payload(self):
        priv_pem, pub_pem = pem_crypto.generate_keypair()
        payload = json.dumps({"author": "test", "version": "1.0"}, separators=(",", ":"), sort_keys=True).encode()
        sig = pem_crypto.sign(priv_pem, payload)
        assert pem_crypto.verify(pub_pem, payload, sig) is True


class TestTrustAnchor:
    """Tests for trust anchor verification."""

    def test_trust_anchor_public_key_is_pem(self):
        assert pem_crypto.get_trust_anchor_public_key_pem().startswith("-----BEGIN PUBLIC KEY-----")

    def test_sign_public_key_and_verify_roundtrip(self):
        _, pub_pem = pem_crypto.generate_keypair()
        ta_sig = pem_crypto.sign_with_trust_anchor(pub_pem)
        assert isinstance(ta_sig, str)
        assert len(ta_sig) > 0
        assert pem_crypto.verify_trust_anchor(pub_pem, ta_sig) is True

    def test_verify_trust_anchor_rejects_unsigned_key(self):
        _, pub1 = pem_crypto.generate_keypair()
        _, pub2 = pem_crypto.generate_keypair()
        assert pem_crypto.verify_trust_anchor(pub2, pem_crypto.sign_with_trust_anchor(pub1)) is False

    def test_verify_trust_anchor_with_explicit_key(self):
        _, pub_pem = pem_crypto.generate_keypair()
        ta_sig = pem_crypto.sign_with_trust_anchor(pub_pem)
        assert pem_crypto.verify_trust_anchor(pub_pem, ta_sig, pem_crypto.get_trust_anchor_public_key_pem()) is True

    @pytest.mark.parametrize(
        "signature",
        [
            base64.b64encode(b"\x00" * 64).decode(),
            "!!!not-base64!!!",
            "\x00\x01\x02\xff",
        ],
        ids=["zeroed-64-bytes", "invalid-base64", "binary-garbage"],
    )
    def test_verify_trust_anchor_rejects_malformed_sig(self, signature):
        _, pub_pem = pem_crypto.generate_keypair()
        assert pem_crypto.verify_trust_anchor(pub_pem, signature) is False

    def test_verify_trust_anchor_empty_anchor_returns_false(self):
        _, pub_pem = pem_crypto.generate_keypair()
        ta_sig = pem_crypto.sign_with_trust_anchor(pub_pem)
        assert pem_crypto.verify_trust_anchor(pub_pem, ta_sig, "") is False


class TestModuleExports:
    def test_public_exports(self):
        expected = {
            "generate_keypair",
            "get_trust_anchor_public_key_pem",
            "sign",
            "sign_with_trust_anchor",
            "verify",
            "verify_trust_anchor",
        }
        assert set(pem_crypto.__all__) == expected

    def test_private_functions_not_in_exports(self):
        privates = {"_load_private_key", "_load_public_key", "_get_trust_anchor"}
        assert not privates & set(pem_crypto.__all__)

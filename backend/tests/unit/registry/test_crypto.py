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
        assert len(kp["public_key"]) == 64   # 32 bytes = 64 hex chars
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

    async def test_full_publish_pull_verify_cycle(self):
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

        entry = await publish_primitive(
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

        public_key_obj = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(kp["public_key"])
        )
        verified = verify_primitive_signature(pulled, public_key=public_key_obj)
        assert verified is True

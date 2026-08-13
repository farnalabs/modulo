"""Tests for modulo.core.license_signing — enterprise license generation."""

import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modulo.core.license import _LICENSE_PUBLIC_KEY_HEX, parse_and_verify, set_public_key
from modulo.core.license_signing import (
    ENTERPRISE_FEATURES,
    LicenseSigningError,
    build_enterprise_payload,
    encode_license_key,
    generate_enterprise_license,
)
from modulo.core.registry.crypto import generate_keypair

_ORIGINAL_KEY = _LICENSE_PUBLIC_KEY_HEX


@pytest.fixture(autouse=True)
def _reset_key() -> Generator[None, None, None]:
    yield
    set_public_key(_ORIGINAL_KEY)


class TestEncodeLicenseKeyRoundTrip:
    def test_round_trips_through_parse_and_verify(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        payload = {
            "tier": "team",
            "features": ["sso", "team_rbac"],
            "expires_at": "2999-01-01T00:00:00+00:00",
            "org_id": "org-1",
        }
        key = encode_license_key(payload, kp["private_key"])
        result = parse_and_verify(key)
        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.tier == "team"
        assert result.license_data.features == ["sso", "team_rbac"]
        assert result.license_data.org_id == "org-1"
        assert result.license_data.expires_at == "2999-01-01T00:00:00+00:00"

    def test_payload_ordering_does_not_break_verification(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        key = encode_license_key(
            {"tier": "team", "org_id": "o1", "features": [], "expires_at": "2999-01-01T00:00:00+00:00"},
            kp["private_key"],
        )
        # Same dict, different insertion order — canonical sort_keys must still verify.
        reordered = {"expires_at": "2999-01-01T00:00:00+00:00", "org_id": "o1", "features": [], "tier": "team"}
        key2 = encode_license_key(reordered, kp["private_key"])
        assert parse_and_verify(key).valid is True
        assert parse_and_verify(key2).valid is True

    def test_tampered_payload_is_rejected(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        key = encode_license_key({"tier": "team", "org_id": "o1"}, kp["private_key"])
        tampered_payload_b64 = (
            base64.urlsafe_b64encode(json.dumps({"tier": "community"}, separators=(",", ":"), sort_keys=True).encode())
            .decode()
            .rstrip("=")
        )
        tampered = f"{tampered_payload_b64}.{key.split('.')[1]}"
        result = parse_and_verify(tampered)
        assert result.valid is False
        assert "Signature" in (result.error or "")

    def test_signature_by_unknown_key_is_rejected(self) -> None:
        kp = generate_keypair()
        other = generate_keypair()
        set_public_key(kp["public_key"])
        key = encode_license_key({"tier": "team", "org_id": "o1"}, other["private_key"])
        result = parse_and_verify(key)
        assert result.valid is False
        assert result.error == "Signature verification failed"


class TestBuildEnterprisePayload:
    def test_defaults(self) -> None:
        payload = build_enterprise_payload("Acme")
        assert payload["tier"] == "team"
        assert payload["features"] == ENTERPRISE_FEATURES
        assert payload["org_id"]
        expiry = datetime.fromisoformat(payload["expires_at"])
        assert expiry > datetime.now(UTC)

    def test_org_id_derived_stably_from_org_name(self) -> None:
        p1 = build_enterprise_payload("Acme")
        p2 = build_enterprise_payload("Acme")
        assert p1["org_id"] == p2["org_id"]
        assert p1["org_id"] != build_enterprise_payload("Other")["org_id"]

    def test_custom_features_and_org_id(self) -> None:
        payload = build_enterprise_payload("Acme", features=["sso"], org_id="my-org")
        assert payload["features"] == ["sso"]
        assert payload["org_id"] == "my-org"

    def test_term_months_advances_expiry(self) -> None:
        p12 = build_enterprise_payload("Acme", term_months=12)
        p24 = build_enterprise_payload("Acme", term_months=24)
        assert datetime.fromisoformat(p24["expires_at"]) > datetime.fromisoformat(p12["expires_at"])


class TestGenerateEnterpriseLicense:
    def test_private_key_param_signs_verifiable_license(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        key = generate_enterprise_license("Acme", private_key_hex=kp["private_key"])
        result = parse_and_verify(key)
        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.tier == "team"
        assert result.license_data.org_id

    def test_private_key_resolved_from_settings(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        fake_settings = SimpleNamespace(modulo_license_private_key=kp["private_key"])
        with patch("modulo.core.license_signing.get_settings", return_value=fake_settings):
            key = generate_enterprise_license("Acme")
        assert parse_and_verify(key).valid is True

    def test_no_private_key_raises(self) -> None:
        with (
            patch(
                "modulo.core.license_signing.get_settings", return_value=SimpleNamespace(modulo_license_private_key="")
            ),
            pytest.raises(LicenseSigningError, match="MODULO_LICENSE_PRIVATE_KEY"),
        ):
            generate_enterprise_license("Acme")

    def test_malformed_private_key_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid private key hex"):
            generate_enterprise_license("Acme", private_key_hex="not-hex")

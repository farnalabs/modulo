"""FAR-1128 cap-license: real signed licence artefacts through the production path.

Generates Ed25519 keypairs and signs licence payloads with the same crypto
used in production (``modulo.core.registry.crypto`` +
``modulo.core.license_signing``), then runs them through
``modulo.core.license.parse_and_verify`` / the in-memory licence store and
``LicenseKeyTier`` feature gating — no mocks. Covers the four operational
outcomes: valid, expired, tampered signature, wrong tier.
"""

from __future__ import annotations

import base64
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest

from modulo.core.feature_flags import LicenseKeyTier
from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    LicenseData,
    clear_license,
    get_license,
    parse_and_verify,
    set_public_key,
    store_license,
)
from modulo.core.license_signing import TEAM_FEATURES, build_team_payload, encode_license_key
from modulo.core.registry.crypto import generate_keypair

_ORIGINAL_KEY = _LICENSE_PUBLIC_KEY_HEX


@pytest.fixture(autouse=True)
def _reset_license_state() -> Generator[None, None, None]:
    """Restore the module-level public key and clear the licence store."""
    set_public_key(_ORIGINAL_KEY)
    yield
    clear_license()
    set_public_key(_ORIGINAL_KEY)


def _sign(payload: dict[str, object], keypair: dict[str, str]) -> str:
    return encode_license_key(payload, keypair["private_key"])


class TestCapLicenseArtefacts:
    def test_valid_team_artefact_activates_team_flags_through_store(self) -> None:
        keypair = generate_keypair()
        set_public_key(keypair["public_key"])

        artefact = _sign(build_team_payload("Acme Rocketry"), keypair)
        validation = parse_and_verify(artefact)

        assert validation.valid
        assert validation.license_data is not None
        assert validation.license_data.tier == "team"
        assert validation.license_data.org_id
        for feature in TEAM_FEATURES:
            assert feature in validation.license_data.features

        context = LicenseKeyTier(validation.license_data)
        assert context.tier() == "team"
        assert context.feature_enabled("sso") is True
        assert context.feature_enabled("capi_dynamic_workflows") is False

        store_license(artefact, validation.license_data)
        assert get_license() is validation.license_data
        assert get_license().tier == "team"

    def test_expired_artefact_is_rejected(self) -> None:
        keypair = generate_keypair()
        set_public_key(keypair["public_key"])

        payload = build_team_payload("Expired Marina")
        payload["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        artefact = _sign(payload, keypair)

        validation = parse_and_verify(artefact)

        assert not validation.valid
        assert validation.error is not None
        assert "expired" in validation.error.lower()

    def test_tampered_signature_is_rejected(self) -> None:
        keypair = generate_keypair()
        set_public_key(keypair["public_key"])

        artefact = _sign(build_team_payload("Tamper Bros"), keypair)
        payload_b64, sig_b64 = artefact.split(".")
        sig_bytes = bytearray(base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4)))
        sig_bytes[0] ^= 0x01
        tampered_sig = base64.urlsafe_b64encode(bytes(sig_bytes)).decode().rstrip("=")
        tampered = f"{payload_b64}.{tampered_sig}"

        validation = parse_and_verify(tampered)

        assert not validation.valid
        assert validation.error is not None
        assert "Signature" in validation.error

    def test_wrong_tier_artefact_keeps_team_flags_locked(self) -> None:
        keypair = generate_keypair()
        set_public_key(keypair["public_key"])

        payload = build_team_payload("Freeloaders Inc", features=[])
        payload["tier"] = "community"
        artefact = _sign(payload, keypair)

        validation = parse_and_verify(artefact)
        assert validation.valid
        assert validation.license_data is not None
        assert validation.license_data.tier == "community"

        context = LicenseKeyTier(validation.license_data)
        assert context.tier() == "community"
        assert context.feature_enabled("sso") is False
        assert context.feature_enabled("audit_viewer") is False

        store_license(artefact, validation.license_data)
        assert get_license().tier == "community"

    def test_community_artefact_grants_only_explicitly_signed_features(self) -> None:
        """The signed ``features`` list is authoritative on top of tier rank.

        A community-tier artefact carrying ``sso`` in its features list enables
        that flag; a team-tier flag not listed stays locked.
        """
        keypair = generate_keypair()
        set_public_key(keypair["public_key"])

        payload = build_team_payload("Feature Carriers Ltd", features=["sso"])
        payload["tier"] = "community"
        artefact = _sign(payload, keypair)

        validation = parse_and_verify(artefact)
        assert validation.valid
        assert validation.license_data is not None

        context = LicenseKeyTier(validation.license_data)
        assert context.feature_enabled("sso") is True
        assert context.feature_enabled("audit_viewer") is False

    def test_store_evicts_expired_artefact_on_read(self) -> None:
        keypair = generate_keypair()
        set_public_key(keypair["public_key"])

        expired = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        data = LicenseData(
            tier="team",
            features=list(TEAM_FEATURES),
            expires_at=expired,
            org_id="org-evict-me",
            raw_payload={},
            raw_key="unused",
        )

        store_license("unused", data)
        assert get_license() is None
        clear_license()

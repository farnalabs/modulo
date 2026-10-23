"""FAR-2091: the licence trust anchor is explicit and fails closed.

There are TWO licence keypairs: the DEVELOPMENT keypair whose public half is
built into ``core/license.py``, and the PRODUCTION keypair Farnalabs signs
customer licences with (its public half is published to customers as
``MODULO_LICENSE_PUBLIC_KEY``). These tests pin the boundary between them:

- a production-signed key under the built-in development anchor is reported as
  an ANCHOR mismatch, not a generic signature failure;
- a configured anchor verifies the matching key and reports a plain signature
  mismatch for a different signer;
- startup fails closed when a licence is configured without an anchor, while a
  Community instance with no licence still boots.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from modulo.api import main as api_main
from modulo.core import license as license_mod
from modulo.core.license import (
    LICENCE_GRACE_DAYS,
    REASON_ANCHOR_MISMATCH,
    REASON_EXPIRED,
    REASON_MALFORMED,
    REASON_SIGNATURE_MISMATCH,
    parse_and_verify,
    set_public_key,
    using_builtin_dev_anchor,
)
from modulo.core.license_signing import encode_license_key
from modulo.core.registry.crypto import generate_keypair


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier": "team",
        "features": ["sso"],
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "org_id": "anchor-test-org",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _isolate_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no configured override (the built-in anchor)."""
    monkeypatch.setattr(license_mod, "_configured_public_key_hex", None)


# ── Anchor selection ─────────────────────────────────────────────────────


class TestAnchorSelection:
    def test_builtin_dev_anchor_is_the_default(self) -> None:
        assert using_builtin_dev_anchor() is True

    def test_configured_key_replaces_the_builtin_anchor(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        assert using_builtin_dev_anchor() is False

    def test_configured_dev_key_is_still_the_dev_anchor(self) -> None:
        set_public_key(license_mod._BUILTIN_DEV_PUBLIC_KEY_HEX)
        assert using_builtin_dev_anchor() is True


# ── Verification failure reasons ─────────────────────────────────────────


class TestVerificationReasons:
    def test_production_key_under_builtin_anchor_reports_anchor_mismatch(self) -> None:
        # A key signed by a keypair the deployment does not trust: with the
        # built-in DEVELOPMENT anchor this is an anchor problem, not a corrupt
        # key, and the operator must be told to set MODULO_LICENSE_PUBLIC_KEY.
        kp = generate_keypair()
        result = parse_and_verify(encode_license_key(_payload(), kp["private_key"]))

        assert result.valid is False
        assert result.reason == REASON_ANCHOR_MISMATCH
        assert "MODULO_LICENSE_PUBLIC_KEY" in (result.hint or "")

    def test_wrong_signer_under_configured_anchor_reports_signature_mismatch(self) -> None:
        authority = generate_keypair()
        other = generate_keypair()
        set_public_key(authority["public_key"])

        result = parse_and_verify(encode_license_key(_payload(), other["private_key"]))

        assert result.valid is False
        assert result.reason == REASON_SIGNATURE_MISMATCH
        assert "anchor mismatch" in (result.hint or "").lower()

    def test_configured_anchor_verifies_a_matching_key(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])

        result = parse_and_verify(encode_license_key(_payload(), kp["private_key"]))

        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.tier == "team"

    def test_expired_key_reports_expired(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        expired = (datetime.now(UTC) - timedelta(days=LICENCE_GRACE_DAYS + 1)).isoformat()

        result = parse_and_verify(encode_license_key(_payload(expires_at=expired), kp["private_key"]))

        assert result.valid is False
        assert result.reason == REASON_EXPIRED
        assert result.error == "License has expired"

    def test_malformed_key_reports_malformed(self) -> None:
        result = parse_and_verify("not-a-license-key")

        assert result.valid is False
        assert result.reason == REASON_MALFORMED

    def test_bad_expiry_format_reports_malformed(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])

        result = parse_and_verify(encode_license_key(_payload(expires_at="not-a-timestamp"), kp["private_key"]))

        assert result.valid is False
        assert result.reason == REASON_MALFORMED


# ── Startup anchor policy ────────────────────────────────────────────────


class TestStartupAnchorPolicy:
    @staticmethod
    def _settings(**overrides: Any) -> SimpleNamespace:
        values: dict[str, Any] = {
            "debug": False,
            "modulo_license_key": "",
            "modulo_license_public_key": "",
            "modulo_public_url": "https://modulo.test",
            "modulo_db": "postgres",
            "redis_url": "redis://localhost:6379/0",
            "modulo_otel_service_name": "modulo-test",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @pytest.fixture(autouse=True)
    def _stub_otel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(api_main, "setup_otel", MagicMock())
        monkeypatch.setattr(api_main, "is_telemetry_enabled", lambda: False)

    def test_licence_without_anchor_fails_closed(self) -> None:
        settings = self._settings(modulo_license_key="env-licence-key")

        with pytest.raises(RuntimeError, match="MODULO_LICENSE_PUBLIC_KEY"):
            api_main._configure_license_and_otel(settings)

    def test_community_instance_without_anchor_boots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No licence configured: an anchor is not needed, so a plain Community
        # self-host must still boot.
        setup_otel = MagicMock()
        monkeypatch.setattr(api_main, "setup_otel", setup_otel)

        api_main._configure_license_and_otel(self._settings())

        assert setup_otel.call_count == 1

    def test_debug_instance_with_licence_and_no_anchor_boots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup_otel = MagicMock()
        monkeypatch.setattr(api_main, "setup_otel", setup_otel)

        api_main._configure_license_and_otel(self._settings(debug=True, modulo_license_key="env-licence-key"))

        assert setup_otel.call_count == 1

    def test_valid_anchor_is_applied(self) -> None:
        kp = generate_keypair()

        api_main._configure_license_and_otel(self._settings(modulo_license_public_key=kp["public_key"]))

        assert using_builtin_dev_anchor() is False

    def test_invalid_anchor_fails_closed(self) -> None:
        settings = self._settings(modulo_license_public_key="not-hex")

        with pytest.raises(RuntimeError, match="MODULO_LICENSE_PUBLIC_KEY"):
            api_main._configure_license_and_otel(settings)

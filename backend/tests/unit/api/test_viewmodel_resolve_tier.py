"""Unit tests for ``viewmodel._resolve_tier`` — the ``/viewmodel/current`` plan.tier ladder.

FAR-1720 made rung 3 verify the env-var licence key via ``parse_and_verify``
(before that it trusted mere presence — any non-empty
``settings.modulo_license_key`` returned ``"team"``). FAR-1733 routes that rung
through the shared ``core.license.verify_license_key`` helper, so the ladder
mirrors ``core.feature_flags._resolve_paid_plan`` rungs 2-3 exactly: the
in-memory licence wins, the env-var key is trusted only after verification,
and an unverifiable key falls through to ``"community"``.

These tests pin rung 2 (in-memory win) and rung 3 (verified env key) so the
shared-helper refactor cannot silently change the ladder.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from modulo.api.routes.viewmodel import _resolve_tier
from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    LicenseData,
    clear_license,
    set_public_key,
    store_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.settings import Settings

_VALID_32 = "a" * 32

# Known test keypair: signed payloads verify only while set_public_key(_TEST_PUB)
# is in effect (the autouse fixture below manages that per test).
_TEST_KP = generate_keypair()
_TEST_PRIV = _TEST_KP["private_key"]
_TEST_PUB = _TEST_KP["public_key"]


def _settings(modulo_license_key: str = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key=modulo_license_key,
    )


def _sign_license_payload(payload: dict[str, Any], private_key: str = _TEST_PRIV) -> str:
    sig_hex = sign_primitive(payload, private_key)
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig_hex)).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier": "team",
        "features": ["sso", "team_rbac", "audit_viewer"],
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "org_id": "test-org",
    }
    payload.update(overrides)
    return payload


def _org(settings_json: dict[str, Any] | None = None) -> MagicMock:
    org = MagicMock()
    # ``_valid_payload`` claims "test-org"; the tenant must match it (FAR-2060).
    org.id = "test-org"
    org.settings_json = settings_json if settings_json is not None else {}
    return org


@pytest.fixture(autouse=True)
def _reset_license_state() -> Generator[None, None, None]:
    """In-memory licence + configured public key are module globals; reset per test."""
    clear_license()
    set_public_key(_TEST_PUB)
    yield
    clear_license()
    set_public_key(_LICENSE_PUBLIC_KEY_HEX)


class TestResolveTierEnvKeyVerification:
    """FAR-1733: the env-var rung is verified through the shared helper (FAR-1720 semantics)."""

    def test_no_env_key_returns_community(self) -> None:
        assert _resolve_tier(_settings()) == "community"

    def test_invalid_env_key_is_not_trusted(self) -> None:
        """An env-var key present but failing verification -> community.

        Regression guard for the shared-helper refactor: a present-but-invalid
        env-var key must never be trusted on presence (the pre-FAR-1720 rung 3
        returned "team" for ANY non-empty key).
        """
        assert _resolve_tier(_settings(modulo_license_key="not-a-valid-key")) == "community"

    def test_expired_env_key_is_not_trusted(self) -> None:
        """An expired but correctly signed key falls through to community."""
        key = _sign_license_payload(_valid_payload(expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()))
        assert _resolve_tier(_settings(modulo_license_key=key)) == "community"

    def test_valid_env_key_reports_its_tier(self) -> None:
        """A signed env-var key verifies -> the LICENCE's tier, not a presence default."""
        key = _sign_license_payload(_valid_payload(tier="team"))
        assert _resolve_tier(_settings(modulo_license_key=key)) == "team"

    def test_valid_env_key_reports_payload_tier_verbatim(self) -> None:
        """The tier comes from the verified payload, not a presence-based default."""
        key = _sign_license_payload(_valid_payload(tier="pilot"))
        assert _resolve_tier(_settings(modulo_license_key=key)) == "pilot"


class TestResolveTierLadderOrder:
    """Rung order stays: org key, then in-memory, then verified env key."""

    def test_in_memory_license_wins_over_env_state(self) -> None:
        stored = LicenseData(
            tier="team",
            features=["sso"],
            expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
            org_id="stored-org",
            raw_payload={},
            raw_key="stored-key",
        )
        store_license("stored-key", stored)
        assert _resolve_tier(_settings()) == "team"
        # An invalid env key does not displace the in-memory rung either.
        assert _resolve_tier(_settings(modulo_license_key="not-a-valid-key")) == "team"

    def test_valid_org_key_wins_and_is_verified(self) -> None:
        key = _sign_license_payload(_valid_payload(tier="team"))
        assert _resolve_tier(_settings(), org=_org({"license_key": key})) == "team"
        # An org key that fails verification falls through to the next rung.
        assert _resolve_tier(_settings(), org=_org({"license_key": "not-a-valid-key"})) == "community"

    def test_org_plan_id_alone_does_not_grant_paid_tier(self) -> None:
        org = _org()
        org.plan_id = "team"
        assert _resolve_tier(_settings(), org=org) == "community"

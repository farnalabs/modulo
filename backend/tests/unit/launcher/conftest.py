"""Shared launcher-test key fixtures (FAR-675).

The SHIPPED manifest module fails closed with an EMPTY trust store; these
fixtures are where the test/CI signing keypairs live instead — private key
material ships in tests only, never in production code. Tests inject the
store explicitly (``test_trust_store``) or patch the module store for flow
tests that exercise the full upgrade path (``patched_trust_store``).
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from modulo.launcher import manifest as manifest_module
from modulo.launcher.manifest import (
    _KEY_ID_CURRENT,
    _KEY_ID_NEXT,
)

# Test/CI-only ed25519 keypairs (private hex — never shipped in src/).
TEST_SIGNING_KEY_HEX_CURRENT = "101acdeda0cd35fdb51f4dae6eff9838b2d07c97641e41a09deb72a2a1a2254d"
TEST_SIGNING_KEY_HEX_NEXT = "f12a59a076a0adba5fd1962c3d99abec05b6ed9103be741b4fe7530145c342ac"

# The public halves of the SAME test pairs (the trust-store rows the tests
# inject). Server-side pairing is asserted by the suite itself: a signature
# created with the private half must verify under the public half.
TEST_TRUST_ROWS: dict[str, tuple[str, str]] = {
    _KEY_ID_CURRENT: ("test-2026-a", "f2c4702958fb649e4114bec4c895b0ff908b0f463669b4cc1c50d42bf01ff734"),
    _KEY_ID_NEXT: ("test-2026-a-next", "17df6bc9bc3f0e109040621a2c45f7320905f38317f91d29d6ed2fb0e7a2ae10"),
}


@pytest.fixture
def test_signing_keys() -> dict[str, str]:
    """key_id -> private signing key hex (tests pass these EXPLICITLY)."""
    return {
        _KEY_ID_CURRENT: TEST_SIGNING_KEY_HEX_CURRENT,
        _KEY_ID_NEXT: TEST_SIGNING_KEY_HEX_NEXT,
    }


def _rows_to_store(rows: dict[str, tuple[str, str]]) -> dict[str, Ed25519PublicKey]:
    return {
        key_id: Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        for key_id, (_label, public_hex) in rows.items()
    }


@pytest.fixture
def test_trust_store() -> dict[str, Ed25519PublicKey]:
    """The test trust store (current + next) to pass as ``trust_store=``."""
    return _rows_to_store(TEST_TRUST_ROWS)


@pytest.fixture
def test_signing_key_hex(test_signing_keys: dict[str, str]) -> str:
    """The CURRENT test signing key's hex (the most common single need)."""
    return test_signing_keys[_KEY_ID_CURRENT]


@pytest.fixture
def patched_trust_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Ed25519PublicKey]:
    """Patch the MODULE store with the test rows and return the built store.

    For flow tests (perform_upgrade) that reach verification through the
    ship-path default store instead of an explicit ``trust_store=``. Tests
    that assert the shipped FAIL-CLOSED behavior must NOT use this fixture.
    """
    store = _rows_to_store(TEST_TRUST_ROWS)
    monkeypatch.setattr(manifest_module, "_TRUST_ROWS", dict(TEST_TRUST_ROWS))
    return store


@pytest.fixture
def sig_factory(test_signing_keys: dict[str, str]) -> Any:
    """Build a .sig dict signed by the given test key over *payload*."""

    from modulo.launcher.manifest import sign_release_bytes

    def _factory(payload: bytes, key_id: str | None = None) -> dict[str, str]:
        resolved_id = key_id if key_id is not None else _KEY_ID_CURRENT
        private_hex = test_signing_keys[resolved_id]
        return sign_release_bytes(payload, key_id=resolved_id, private_key_hex=private_hex)

    return _factory

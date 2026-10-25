"""FAR-2146 — the signed ``starts_at`` claim and the parked successor licence.

A customer may choose when their licence starts, future-only. The two things
these tests pin:

1. BACKWARD COMPATIBILITY — a key with no ``starts_at`` (every key minted
   before this slice) verifies and behaves EXACTLY as it does today: it is
   active immediately and nothing is parked.
2. The parked-successor lifecycle — a key whose start is in the future is
   accepted, does not displace the active licence, and becomes active when its
   start arrives with no operator action (controlled clock).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import modulo.core.license as license_module
import modulo.core.license_signing as license_signing_module
from modulo.core.license import (
    LICENCE_GRACE_DAYS,
    REASON_MALFORMED,
    LicenseData,
    clear_license,
    get_license,
    get_parked_successor,
    is_parked,
    parse_and_verify,
    set_public_key,
    store_license,
)
from modulo.core.license_signing import (
    LicenseSigningError,
    build_team_payload,
    generate_team_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive

_FROZEN_NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is pinned to :data:`_FROZEN_NOW`."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return _FROZEN_NOW if tz is not None else _FROZEN_NOW.replace(tzinfo=None)


def _frozen_class_at(instant: datetime) -> type[datetime]:
    """A ``datetime`` subclass whose ``now()`` is pinned to *instant*."""

    class _AtInstant(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return instant if tz is not None else instant.replace(tzinfo=None)

    return _AtInstant


@pytest.fixture(autouse=True)
def _reset_store() -> Generator[None, None, None]:
    """The in-memory licence store is process-global: never leak between tests."""
    clear_license()
    yield
    clear_license()


@pytest.fixture
def kp() -> Generator[dict[str, str], None, None]:
    original = license_module._LICENSE_PUBLIC_KEY_HEX
    generated = generate_keypair()
    set_public_key(generated["public_key"])
    clear_license()
    yield generated
    clear_license()
    set_public_key(original)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin BOTH clocks (verify/store and signing) to the same instant."""
    monkeypatch.setattr(license_module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(license_signing_module, "datetime", _FrozenDatetime)
    return _FROZEN_NOW


def _sign(payload: dict[str, Any], private_key_hex: str) -> str:
    """Sign an arbitrary payload into a licence key (bypasses the builder)."""
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sign_primitive(payload, private_key_hex))).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _licence(**overrides: Any) -> LicenseData:
    values: dict[str, Any] = {
        "tier": "team",
        "features": ["sso"],
        "expires_at": "2999-01-01T00:00:00+00:00",
        "org_id": "org-1",
        "raw_payload": {},
        "raw_key": "key",
    }
    values.update(overrides)
    return LicenseData(**values)


# ---------------------------------------------------------------------------
# 1. Backward compatibility — the highest-risk path in this slice
# ---------------------------------------------------------------------------


class TestOldFormatKeyStillBehavesExactlyAsBefore:
    def test_a_key_without_starts_at_verifies_and_is_not_parked(self, kp: dict[str, str]) -> None:
        key = _sign(
            {
                "tier": "team",
                "features": ["sso"],
                "expires_at": "2999-01-01T00:00:00+00:00",
                "org_id": "org-legacy",
            },
            kp["private_key"],
        )

        validation = parse_and_verify(key)

        assert validation.valid is True
        assert validation.license_data is not None
        assert validation.license_data.starts_at is None

    def test_an_old_key_is_active_immediately_and_nothing_is_parked(self, kp: dict[str, str]) -> None:
        """Exactly today's behaviour: install -> active, no successor slot."""
        key = _sign(
            {"tier": "team", "features": ["sso"], "expires_at": "2999-01-01T00:00:00+00:00", "org_id": "org-legacy"},
            kp["private_key"],
        )
        data = parse_and_verify(key).license_data
        assert data is not None

        store_license(key, data)

        assert get_license() is data
        assert is_parked(data) is False
        assert get_parked_successor() is None

    def test_an_immediate_start_minted_key_carries_no_claim(self, kp: dict[str, str]) -> None:
        """A normal purchase mints the same payload shape as before FAR-2146."""
        key = generate_team_license("Acme", private_key_hex=kp["private_key"])

        data = parse_and_verify(key).license_data

        assert data is not None
        assert data.starts_at is None
        assert "starts_at" not in data.raw_payload


# ---------------------------------------------------------------------------
# 2. The signed claim round-trips, and the invariant is enforced
# ---------------------------------------------------------------------------


class TestSignedStartsAtClaim:
    def test_chosen_start_round_trips_and_computes_the_expiry_from_it(
        self, kp: dict[str, str], frozen_now: datetime
    ) -> None:
        start = frozen_now + timedelta(days=60)

        key = generate_team_license(
            "Acme",
            term_months=12,
            private_key_hex=kp["private_key"],
            starts_at=start,
        )
        data = parse_and_verify(key).license_data

        assert data is not None
        assert data.starts_at == start.isoformat()
        # The invariant: expires_at = starts_at + term (NOT issued_at + term).
        assert data.expires_at == "2027-12-24T12:00:00+00:00"
        # issued_at remains the TRUE mint time — it is not overloaded to
        # express when the window opens.
        assert data.issued_at == frozen_now.isoformat()

    def test_a_naive_starts_at_claim_is_rejected_not_read_as_local(
        self, kp: dict[str, str], frozen_now: datetime
    ) -> None:
        key = _sign(
            {
                "tier": "team",
                "expires_at": "2999-01-01T00:00:00+00:00",
                "org_id": "org-1",
                "starts_at": "2027-03-12T00:00:00",
            },
            kp["private_key"],
        )

        validation = parse_and_verify(key)

        assert validation.valid is False
        assert validation.reason == REASON_MALFORMED
        assert "timezone-aware" in (validation.error or "")

    def test_starts_at_after_expires_at_is_rejected(self, kp: dict[str, str]) -> None:
        """The window invariant ``starts_at <= expires_at`` is enforced on read."""
        key = _sign(
            {
                "tier": "team",
                "expires_at": "2027-01-01T00:00:00+00:00",
                "org_id": "org-1",
                "starts_at": "2028-01-01T00:00:00+00:00",
            },
            kp["private_key"],
        )

        validation = parse_and_verify(key)

        assert validation.valid is False
        assert validation.reason == REASON_MALFORMED
        assert "after expires_at" in (validation.error or "")

    def test_the_signer_rejects_a_naive_start(self) -> None:
        with pytest.raises(LicenseSigningError, match="starts_at must be timezone-aware"):
            build_team_payload("Acme", starts_at=datetime(2027, 3, 12, 0, 0, 0))

    def test_the_signer_rejects_a_start_combined_with_stacking(self) -> None:
        """Chosen start and stacking are alternatives — one must not silently win."""
        with pytest.raises(LicenseSigningError, match="mutually exclusive"):
            build_team_payload(
                "Acme",
                starts_at=_FROZEN_NOW + timedelta(days=30),
                expires_from=_FROZEN_NOW + timedelta(days=10),
            )


# ---------------------------------------------------------------------------
# 3. Parking — valid but not yet active
# ---------------------------------------------------------------------------


class TestParkedSuccessor:
    def test_a_future_start_is_parked_and_does_not_displace_the_active_licence(self, frozen_now: datetime) -> None:
        active = _licence(expires_at=(frozen_now + timedelta(days=40)).isoformat())
        successor = _licence(
            starts_at=(frozen_now + timedelta(days=30)).isoformat(),
            expires_at=(frozen_now + timedelta(days=395)).isoformat(),
        )

        store_license("active", active)
        store_license("successor", successor)

        assert is_parked(successor) is True
        assert get_license() is active
        assert get_parked_successor() is successor

    def test_a_lone_parked_licence_grants_nothing_yet(self, frozen_now: datetime) -> None:
        """Valid but not active: the instance keeps running on Community."""
        successor = _licence(
            starts_at=(frozen_now + timedelta(days=30)).isoformat(),
            expires_at=(frozen_now + timedelta(days=395)).isoformat(),
        )

        store_license("successor", successor)

        assert get_license() is None
        assert get_parked_successor() is successor

    def test_the_successor_becomes_active_when_its_start_arrives(
        self,
        monkeypatch: pytest.MonkeyPatch,
        frozen_now: datetime,
    ) -> None:
        """No operator action: the next resolution after the start promotes it."""
        start = frozen_now + timedelta(days=30)
        successor = _licence(
            starts_at=start.isoformat(),
            expires_at=(frozen_now + timedelta(days=395)).isoformat(),
        )
        store_license("successor", successor)

        # Just before the start: still parked.
        monkeypatch.setattr(license_module, "datetime", _frozen_class_at(start - timedelta(seconds=1)))
        assert get_license() is None
        assert get_parked_successor() is successor

        # At the start: promoted with no other call.
        monkeypatch.setattr(license_module, "datetime", _frozen_class_at(start))
        assert get_license() is successor
        assert get_parked_successor() is None

    def test_promotion_replaces_the_licence_that_was_active(
        self,
        monkeypatch: pytest.MonkeyPatch,
        frozen_now: datetime,
    ) -> None:
        active = _licence(expires_at="2999-01-01T00:00:00+00:00")
        start = frozen_now + timedelta(days=5)
        successor = _licence(
            starts_at=start.isoformat(),
            expires_at=(start + timedelta(days=365)).isoformat(),
        )
        store_license("active", active)
        store_license("successor", successor)

        monkeypatch.setattr(license_module, "datetime", _frozen_class_at(start))

        assert get_license() is successor
        assert get_parked_successor() is None

    def test_a_lapsed_active_licence_does_not_discard_the_parked_successor(self, frozen_now: datetime) -> None:
        """The previous licence lapsing is exactly when the successor matters."""
        lapsed = _licence(
            expires_at=(frozen_now - timedelta(days=LICENCE_GRACE_DAYS + 1)).isoformat(),
        )
        successor = _licence(
            starts_at=(frozen_now + timedelta(days=30)).isoformat(),
            expires_at=(frozen_now + timedelta(days=395)).isoformat(),
        )
        store_license("lapsed", lapsed)
        store_license("successor", successor)

        assert get_license() is None
        assert get_parked_successor() is successor

    def test_clear_license_clears_the_parked_successor_too(self, frozen_now: datetime) -> None:
        successor = _licence(
            starts_at=(frozen_now + timedelta(days=30)).isoformat(),
            expires_at=(frozen_now + timedelta(days=395)).isoformat(),
        )
        store_license("successor", successor)

        clear_license()

        assert get_parked_successor() is None
        assert get_license() is None


# ---------------------------------------------------------------------------
# 4. The licence ladder honours the claim for CONFIGURED keys too
# ---------------------------------------------------------------------------


class TestLadderHonoursStartsAt:
    """A configured key is not "installed" through the park slot.

    A successor can arrive as the ``MODULO_LICENSE_KEY`` env var or an org-level
    key. The ladder must refuse to APPLY such a key before its start — otherwise
    writing configuration would grant Team capability for days before the paid
    window begins — and must apply it automatically once the start arrives.
    """

    def test_a_configured_future_key_is_not_applied_before_its_start(
        self, kp: dict[str, str], frozen_now: datetime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from modulo.core.feature_flags import resolve_effective_license

        start = frozen_now + timedelta(days=30)
        key = generate_team_license(
            "Acme",
            private_key_hex=kp["private_key"],
            starts_at=start,
        )
        settings = SimpleNamespace(modulo_license_key=key)

        assert resolve_effective_license(settings, None) is None

        monkeypatch.setattr(license_module, "datetime", _frozen_class_at(start))

        resolved = resolve_effective_license(settings, None)

        assert resolved is not None
        assert resolved.starts_at == start.isoformat()

    def test_an_active_in_memory_licence_survives_a_future_configured_key(
        self, kp: dict[str, str], frozen_now: datetime
    ) -> None:
        from modulo.core.feature_flags import resolve_effective_license

        active = _licence(expires_at="2999-01-01T00:00:00+00:00")
        store_license("active", active)
        start = frozen_now + timedelta(days=30)
        future_key = generate_team_license("Acme", private_key_hex=kp["private_key"], starts_at=start)
        settings = SimpleNamespace(modulo_license_key=future_key)

        # The future env-var rung falls through; the active in-memory licence wins.
        assert resolve_effective_license(settings, None) is active

"""FAR-2109 — POST /api/v1/webhooks/stripe/renewal-claim.

The purchase surface has no pre-payment input: a Stripe purchase mints a signed
key and emails it. So the early-renewal claim is a POST-PURCHASE step the
customer performs with the keys they hold — no session, no tenant at the
boundary. Admission is the signature of BOTH keys, verified against the
deployment's anchor. These tests drive the real endpoint (no mocked verification)
and pin its contract:

* a valid claim returns the stacked key;
* a forged / cross-org / malformed previous key is rejected with a
  machine-readable ``detail.reason`` and the unchanged purchase expiry;
* omitting the key returns an explained, recoverable ``unstacked`` outcome;
* a retry returns the SAME key rather than extending a second time.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    REASON_MALFORMED,
    REASON_SIGNATURE_MISMATCH,
    parse_and_verify,
    set_public_key,
)
from modulo.core.license_signing import generate_team_license
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.core.stripe_fulfilment import (
    REASON_NO_PRESENTED_KEY,
    REASON_ORG_MISMATCH,
    _renewal_claims,
)
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_KP = generate_keypair()
_TEST_PRIV = _KP["private_key"]
_TEST_PUB = _KP["public_key"]

_ENDPOINT = "/api/v1/webhooks/stripe/renewal-claim"
_NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _trusted_anchor() -> Generator[None, None, None]:
    _renewal_claims.clear()
    set_public_key(_TEST_PUB)
    yield
    _renewal_claims.clear()
    set_public_key(_LICENSE_PUBLIC_KEY_HEX)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_private_key=_TEST_PRIV,
        redis_url="",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def _signed_key(payload: dict[str, Any], private_key: str = _TEST_PRIV) -> str:
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sign_primitive(payload, private_key))).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _purchase_key() -> str:
    return generate_team_license("Acme", term_months=12, private_key_hex=_TEST_PRIV)


def _purchase_data(key: str) -> Any:
    data = parse_and_verify(key).license_data
    assert data is not None
    return data


def _previous_key(purchase_key: str, *, days_remaining: int = 60, org_id: str | None = None) -> str:
    purchase = _purchase_data(purchase_key)
    return _signed_key(
        {
            "tier": "team",
            "features": ["sso"],
            "expires_at": (_NOW + timedelta(days=days_remaining)).isoformat(),
            "org_id": purchase.org_id if org_id is None else org_id,
            "renewal_mode": "manual",
            "term": 12,
            "licence_id": "licence-previous",
            "issued_at": (_NOW - timedelta(days=300)).isoformat(),
        }
    )


def _post(client: TestClient, *, purchase_key: str, presented_key: str | None) -> Any:
    return client.post(
        _ENDPOINT,
        json={"purchase_key": purchase_key, "presented_key": presented_key},
    )


class TestStackedClaim:
    def test_a_valid_claim_returns_the_stacked_key(self, client: TestClient) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase)

        resp = _post(client, purchase_key=purchase, presented_key=previous)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "stacked"
        assert body["reason"] is None
        assert body["previous_expires_at"] == _purchase_data(previous).expires_at
        stacked = _purchase_data(body["license_key"])
        assert stacked.licence_id == _purchase_data(purchase).licence_id
        assert datetime.fromisoformat(stacked.expires_at) > datetime.fromisoformat(_purchase_data(purchase).expires_at)

    def test_the_endpoint_needs_no_session(self, client: TestClient) -> None:
        """Pre-auth by design: the caller is a purchaser with no account here.

        A 401 would mean an authenticated dependency crept onto the route and
        the claim is unreachable for the only people who use it.
        """
        purchase = _purchase_key()

        resp = _post(client, purchase_key=purchase, presented_key=None)

        assert resp.status_code != 401


class TestUnstackedClaim:
    def test_omitting_the_key_returns_an_explanation_not_a_silent_fallback(self, client: TestClient) -> None:
        purchase = _purchase_key()

        resp = _post(client, purchase_key=purchase, presented_key=None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "unstacked"
        assert body["reason"] == REASON_NO_PRESENTED_KEY
        assert body["license_key"] == purchase
        assert body["expires_at"] == _purchase_data(purchase).expires_at
        assert "No previous licence was applied" in body["detail"]
        assert "re-submit" in body["detail"]


class TestRejectedClaims:
    def test_a_tampered_previous_key_is_rejected_with_its_reason(self, client: TestClient) -> None:
        purchase = _purchase_key()
        valid = _previous_key(purchase)
        payload_b64, signature = valid.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        payload["expires_at"] = "2099-01-01T00:00:00+00:00"
        tampered_payload = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).decode()
        tampered = f"{tampered_payload.rstrip('=')}.{signature}"

        resp = _post(client, purchase_key=purchase, presented_key=tampered)

        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == REASON_SIGNATURE_MISMATCH
        assert _purchase_data(purchase).expires_at in body["detail"]

    def test_a_malformed_previous_key_is_rejected_with_its_reason(self, client: TestClient) -> None:
        purchase = _purchase_key()

        resp = _post(client, purchase_key=purchase, presented_key="not-a-key")

        assert resp.status_code == 422
        assert resp.json()["code"] == REASON_MALFORMED

    def test_a_key_from_another_organisation_is_rejected(self, client: TestClient) -> None:
        purchase = _purchase_key()
        foreign = _previous_key(purchase, org_id="org-someone-else")

        resp = _post(client, purchase_key=purchase, presented_key=foreign)

        assert resp.status_code == 422
        assert resp.json()["code"] == REASON_ORG_MISMATCH

    def test_an_unverifiable_purchase_key_is_rejected_without_minting(self, client: TestClient) -> None:
        purchase = _purchase_key()
        forged = _signed_key(_purchase_data(purchase).raw_payload, generate_keypair()["private_key"])

        resp = _post(client, purchase_key=forged, presented_key=purchase)

        assert resp.status_code == 422
        assert resp.json()["code"] == REASON_SIGNATURE_MISMATCH
        assert not _renewal_claims


class TestIdempotency:
    def test_a_retried_claim_returns_the_same_key(self, client: TestClient) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase)

        first = _post(client, purchase_key=purchase, presented_key=previous)
        second = _post(client, purchase_key=purchase, presented_key=previous)

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["license_key"] == first.json()["license_key"]
        assert second.json()["repeated"] is True

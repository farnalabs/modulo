"""Unit tests for POST /api/v1/webhooks/stripe — signature verification + fulfilment dispatch.

Signature tests use the REAL Stripe signature scheme (HMAC-SHA256 over
``"<timestamp>.<raw_body>"`` with the webhook secret) signed in-test and passed
through the actual endpoint — no mock bypasses signature verification.
"""

import hashlib
import hmac
import json
import re
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    RENEWAL_MODE_AUTO,
    RENEWAL_MODE_MANUAL,
    parse_and_verify,
    set_public_key,
)
from modulo.core.registry.crypto import generate_keypair
from modulo.core.stripe_fulfilment import _processed_event_ids
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_TEST_SECRET = "whsec_test_123"

_FULFIL_MODULE = "modulo.api.routes.stripe_webhook.fulfil_team_purchase"

_KP = generate_keypair()
_LICENSE_KEY_PATTERN = re.compile(r"MODULO_LICENSE_KEY=(\S+)")


@pytest.fixture(autouse=True)
def _stub_audit_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-1516: stub the fresh ``audit_session`` the ``audited_system(...)``
    dependency writes on (a real engine — no database in the unit tier); the
    dependency itself still runs."""
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    @asynccontextmanager
    async def _factory() -> AsyncGenerator[AsyncMock, None]:
        yield session

    monkeypatch.setattr("modulo.core.audit_coverage._shared_session_factory", lambda: _factory)


def _make_stripe_settings(
    secret_key: str = "sk_test_123",
    webhook_secret: str = _TEST_SECRET,
    *,
    license_private_key: str | None = None,
    redis_url: str | None = None,
) -> Settings:
    overrides: dict[str, str] = {}
    if license_private_key is not None:
        overrides["modulo_license_private_key"] = license_private_key
    if redis_url is not None:
        overrides["redis_url"] = redis_url
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        stripe_secret_key=secret_key,
        stripe_webhook_secret=webhook_secret,
        **overrides,
    )


def _sign(body: bytes, *, secret: str = _TEST_SECRET, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _checkout_event(event_id: str, *, payment_status: str = "paid", email: str = "bob@acme.com") -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{event_id}",
                "mode": "subscription",
                "payment_status": payment_status,
                "customer_email": email,
                "customer_details": {"email": email, "name": "Acme Inc"},
            }
        },
    }


def _invoice_event(event_id: str = "evt_inv_paid", *, email: str = "bob@acme.com", name: str = "Acme Inc") -> dict:
    return {
        "id": event_id,
        "type": "invoice.paid",
        "data": {"object": {"id": f"in_{event_id}", "customer_email": email, "customer_name": name}},
    }


def _invoice_event_with_subscription(event_id: str, subscription: str | None) -> dict:
    """An ``invoice.paid`` event carrying Stripe's ``Invoice.subscription``.

    That field is the id of the subscription the invoice was raised for, or
    ``null`` for a one-off invoice — the signal FAR-2136 reads.
    """
    event = _invoice_event(event_id)
    event["data"]["object"]["subscription"] = subscription
    return event


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_stripe_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def disabled_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = lambda: _make_stripe_settings(secret_key="", webhook_secret="")
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def fulfilment_client() -> Generator[TestClient, None, None]:
    """A client whose webhook runs the REAL fulfilment path (FAR-2136).

    Only ``get_settings`` is overridden: the signing key and the trust anchor
    are a matching test keypair, and the idempotency fallback is used
    (``redis_url=""``) so no Redis is needed. Outbound email is patched per
    test, so the key the test parses is one that was actually signed and
    emailed through ``fulfil_team_purchase``.
    """
    app.dependency_overrides[get_settings] = lambda: _make_stripe_settings(
        license_private_key=_KP["private_key"],
        redis_url="",
    )
    _processed_event_ids.clear()
    set_public_key(_KP["public_key"])
    try:
        yield TestClient(app)
    finally:
        _processed_event_ids.clear()
        set_public_key(_LICENSE_PUBLIC_KEY_HEX)
        app.dependency_overrides.clear()


def _minted_key(mock_send: MagicMock) -> str:
    """The licence key carried by the email the fulfilment sent."""
    body_text = mock_send.call_args.args[4]
    match = _LICENSE_KEY_PATTERN.search(body_text)
    assert match is not None, "the fulfilment email carried no MODULO_LICENSE_KEY"
    return match.group(1)


def _post(client: TestClient, body: bytes, signature: str | bytes | None) -> object:
    headers = {"Stripe-Signature": signature} if signature is not None else {}
    return client.post("/api/v1/webhooks/stripe", content=body, headers=headers)


class TestSignatureVerification:
    def test_valid_signature_accepted(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        assert resp.json()["event_id"] == "evt_inv_paid"
        mock_fulfil.assert_awaited_once()

    def test_invalid_signature_rejected(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, "t=1,v1=deadbeef")
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_tampered_body_rejected(self, client: TestClient) -> None:
        # Sign one payload, then send a different one — must fail closed.
        signed_body = json.dumps(_invoice_event(event_id="evt_a")).encode()
        sent_body = json.dumps(_invoice_event(event_id="evt_b")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, sent_body, _sign(signed_body))
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_stale_timestamp_rejected(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        stale = int(time.time()) - 10_000
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body, timestamp=stale))
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_missing_signature_header_rejected(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, None)
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_malformed_signature_header_rejected(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, "not-a-stripe-signature")
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_non_ascii_signature_header_rejected_without_raising(self, client: TestClient) -> None:
        """A latin-1-decoded non-ASCII signature must be a 400, not a 500.

        ``hmac.compare_digest`` raises TypeError on str operands containing
        non-ASCII characters. The header bytes are sent raw so Starlette
        decodes 0xE9 to 'é' exactly as it would from a real client.
        """
        body = json.dumps(_invoice_event()).encode()
        timestamp = str(int(time.time())).encode()
        signature = b"t=" + timestamp + b",v1=\xe9"
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, signature)
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_non_json_body_rejected(self, client: TestClient) -> None:
        body = b"not json"
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()


class TestFulfilmentDispatch:
    def test_checkout_session_completed_is_noop(self, client: TestClient) -> None:
        """checkout.session.completed is accepted (200) but NEVER fulfils — even
        when payment_status=paid. invoice.paid is the single fulfilment event
        (FAR-180)."""
        body = json.dumps(_checkout_event("evt_checkout_paid")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()

    def test_checkout_session_noop_regardless_of_payment_status(self, client: TestClient) -> None:
        body = json.dumps(_checkout_event("evt_unpaid", payment_status="unpaid")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()

    def test_checkout_then_invoice_fulfils_exactly_once(self, client: TestClient) -> None:
        """A card-paid subscription sends checkout.session.completed AND
        invoice.paid for the same purchase. Only invoice.paid may fulfil — the
        checkout no-op must not add a second fulfilment (FAR-180)."""
        body_checkout = json.dumps(_checkout_event("evt_checkout_paid")).encode()
        body_invoice = json.dumps(_invoice_event("evt_inv_paid")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp_checkout = _post(client, body_checkout, _sign(body_checkout))
            resp_invoice = _post(client, body_invoice, _sign(body_invoice))
        assert resp_checkout.status_code == 200
        assert resp_invoice.status_code == 200
        mock_fulfil.assert_awaited_once()
        assert mock_fulfil.await_args.kwargs["event_id"] == "evt_inv_paid"
        assert mock_fulfil.await_args.kwargs["customer_email"] == "bob@acme.com"
        assert mock_fulfil.await_args.kwargs["org_name"] == "Acme Inc"

    def test_invoice_paid_dispatches_with_customer_email(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event("evt_inv_1")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_awaited_once()
        assert mock_fulfil.await_args.kwargs["customer_email"] == "bob@acme.com"
        assert mock_fulfil.await_args.kwargs["org_name"] == "Acme Inc"

    def test_event_without_customer_email_does_not_dispatch(self, client: TestClient) -> None:
        event = _invoice_event("evt_no_email", email=None, name=None)
        body = json.dumps(event).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()

    def test_unrelated_event_returns_200_without_dispatch(self, client: TestClient) -> None:
        body = json.dumps({"id": "evt_other", "type": "customer.subscription.updated", "data": {"object": {}}}).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()


class TestRenewalModeFromEvent:
    """FAR-2136 — the minted key's ``renewal_mode`` comes from the event.

    These drive the REAL route and the REAL ``fulfil_team_purchase`` (only the
    outbound email seam is patched), so the mode is asserted on a key that was
    actually signed and emailed — not on a stubbed signer's arguments.
    """

    def _fulfil(self, client: TestClient, event: dict) -> str:
        body = json.dumps(event).encode()
        with patch(
            "modulo.core.stripe_fulfilment.send_email",
            new=MagicMock(return_value=True),
        ) as mock_send:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_send.assert_called_once()
        return _minted_key(mock_send)

    def test_subscription_invoice_mints_auto_renewal(self, fulfilment_client: TestClient) -> None:
        license_key = self._fulfil(
            fulfilment_client,
            _invoice_event_with_subscription("evt_subscription", "sub_123"),
        )

        data = parse_and_verify(license_key).license_data
        assert data is not None
        assert data.renewal_mode == RENEWAL_MODE_AUTO

    def test_one_off_invoice_mints_manual_renewal(self, fulfilment_client: TestClient) -> None:
        license_key = self._fulfil(
            fulfilment_client,
            _invoice_event_with_subscription("evt_one_off", None),
        )

        data = parse_and_verify(license_key).license_data
        assert data is not None
        assert data.renewal_mode == RENEWAL_MODE_MANUAL


class TestWebhookDisabled:
    def test_returns_404_when_stripe_not_configured(self, disabled_client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(disabled_client, body, _sign(body))
        assert resp.status_code == 404
        mock_fulfil.assert_not_awaited()

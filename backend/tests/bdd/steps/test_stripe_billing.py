"""Step definitions for the Stripe purchase webhook fulfilment surface.

Wired into the executing BDD suite, closing the ``feat-license`` "stripe
webhook cited as an adjacent but not behaviour-covered" gap
(``docs/product-map/licensing/license.md``): ``stripe_billing.feature`` ships
under ``tests/bdd/features/licensing/`` and drives the REAL
``POST /api/v1/webhooks/stripe`` route (``modulo.api.routes.stripe_webhook``)
with only the outbound fulfilment seam patched (``fulfil_team_purchase`` runs
as a FastAPI BackgroundTask) and the ``get_settings`` seam swapped for a
stripe-configured Settings object — mirroring the hermetic-mock pattern of the
sibling licensing step modules.

Signature verification runs for real: every payload is HMAC-SHA256-signed over
``"<timestamp>.<raw_body>"`` exactly as Stripe does (the ``test_stripe_webhook.py``
unit-suite scheme), so the scenarios assert the actual contract: the ±300s
replay window, fail-closed refusals, the single-fulfilment ``invoice.paid``
guard, and the 404 when Stripe is not configured.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/licensing/stripe_billing.feature")

_SECRET = "whsec_test_123"
_FULFIL_MODULE = "modulo.api.routes.stripe_webhook.fulfil_team_purchase"

_FULFIL_MOCK_ATTR = "_stripe_fulfil_mock"


def _sign(body: bytes, *, secret: str = _SECRET, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _invoice_event(
    event_id: str = "evt_inv_paid", *, email: str = "bob@acme.com", name: str = "Acme Inc"
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "invoice.paid",
        "data": {"object": {"id": f"in_{event_id}", "customer_email": email, "customer_name": name}},
    }


def _checkout_event(event_id: str = "evt_checkout_paid") -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{event_id}",
                "mode": "subscription",
                "payment_status": "paid",
                "customer_email": "bob@acme.com",
                "customer_details": {"email": "bob@acme.com", "name": "Acme Inc"},
            }
        },
    }


def _setup_client(ctx: dict[str, Any]) -> None:
    """Override ``get_settings`` with a Stripe-configured Settings object.

    The webhook route only depends on ``get_settings`` and the raw request —
    no DB session, no auth principal — so this is the single seam the steps
    swap. The disabled variant passes empty Stripe credentials, making
    ``stripe_enabled`` False so the route refuses with 404 before any
    signature work.
    """
    from modulo.api.main import app as _app
    from modulo.settings import Settings, get_settings

    configured = ctx.get("stripe_configured", True)
    _settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        stripe_secret_key="sk_test_123" if configured else "",
        stripe_webhook_secret=_SECRET if configured else "",
    )
    _app.dependency_overrides[get_settings] = lambda: _settings
    get_settings.cache_clear()


def _fulfil_mock(request: Any) -> AsyncMock:
    mock = getattr(request.node, _FULFIL_MOCK_ATTR)
    assert isinstance(mock, AsyncMock)
    return mock


def _await_kwargs(fulfil: AsyncMock) -> dict[str, Any]:
    """The ``kwargs`` of the single awaited ``fulfil_team_purchase`` call."""
    await_args = fulfil.await_args
    assert await_args is not None, "fulfil_team_purchase was never dispatched"
    return dict(await_args.kwargs)


def _post(request: Any, ctx: dict[str, Any], body: bytes, signature: str | None) -> None:
    """POST a raw signed/unsigned body through the real webhook route.

    A single ``AsyncMock`` fulfils the whole scenario so multi-event scenarios
    ("checkout followed by invoice.paid") can assert a total dispatch count
    (the FAR-180 single-fulfilment guard), with the patch kept live on the
    shared ``patches`` collector (stopped at scenario teardown by the autouse
    conftest fixture).
    """
    from fastapi.testclient import TestClient

    from modulo.api.main import app

    _setup_client(ctx)
    fulfil = getattr(request.node, _FULFIL_MOCK_ATTR, None)
    if fulfil is None:
        fulfil = AsyncMock(return_value="key")
        setattr(request.node, _FULFIL_MOCK_ATTR, fulfil)
        capture = patch(_FULFIL_MODULE, new=fulfil)
        capture.start()
        request.getfixturevalue("patches").append(capture)
    headers = {"Stripe-Signature": signature} if signature is not None else {}
    resp = TestClient(app).post("/api/v1/webhooks/stripe", content=body, headers=headers)
    request.node._resp = resp


# ── Given steps ──────────────────────────────────────────────────────────────


@given("the purchase webhook is configured with a webhook secret")
def _given_stripe_configured(ctx: dict[str, Any]) -> None:
    ctx["stripe_configured"] = True


@given("the purchase webhook is disabled")
def _given_stripe_disabled(ctx: dict[str, Any]) -> None:
    ctx["stripe_configured"] = False


# ── When steps ───────────────────────────────────────────────────────────────


@when(parsers.parse('Stripe sends a signed invoice.paid event for "{email}" of "{org}"'))
def _when_invoice_paid(email: str, org: str, request: Any, ctx: dict[str, Any]) -> None:
    body = json.dumps(_invoice_event(email=email, name=org)).encode()
    _post(request, ctx, body, _sign(body))


@when("Stripe sends a signed invoice.paid event with no customer email")
def _when_invoice_no_email(request: Any, ctx: dict[str, Any]) -> None:
    body = json.dumps({"id": "evt_no_email", "type": "invoice.paid", "data": {"object": {"id": "in_x"}}}).encode()
    _post(request, ctx, body, _sign(body))


@when("Stripe sends a signed checkout.session.completed event for a paid checkout")
def _when_checkout(request: Any, ctx: dict[str, Any]) -> None:
    body = json.dumps(_checkout_event()).encode()
    _post(request, ctx, body, _sign(body))


@when("Stripe sends a signed customer.subscription.updated event")
def _when_unrelated_event(request: Any, ctx: dict[str, Any]) -> None:
    body = json.dumps({"id": "evt_other", "type": "customer.subscription.updated", "data": {"object": {}}}).encode()
    _post(request, ctx, body, _sign(body))


@when("Stripe sends an event with a bogus signature")
def _when_bogus_signature(request: Any, ctx: dict[str, Any]) -> None:
    body = json.dumps(_invoice_event()).encode()
    _post(request, ctx, body, "t=1,v1=deadbeef")


@when("Stripe sends a tampered invoice.paid event")
def _when_tampered_body(request: Any, ctx: dict[str, Any]) -> None:
    signed_body = json.dumps(_invoice_event(event_id="evt_a")).encode()
    sent_body = json.dumps(_invoice_event(event_id="evt_b")).encode()
    _post(request, ctx, sent_body, _sign(signed_body))


@when("Stripe sends a signed event with a stale timestamp")
def _when_stale_timestamp(request: Any, ctx: dict[str, Any]) -> None:
    body = json.dumps(_invoice_event()).encode()
    stale = int(time.time()) - 10_000
    _post(request, ctx, body, _sign(body, timestamp=stale))


@when("Stripe sends a signed non-JSON payload")
def _when_non_json_body(request: Any, ctx: dict[str, Any]) -> None:
    body = b"not json"
    _post(request, ctx, body, _sign(body))


# ── Then steps ───────────────────────────────────────────────────────────────


@then("no fulfilment is dispatched")
def _then_no_fulfilment(request: Any) -> None:
    _fulfil_mock(request).assert_not_awaited()


@then(parsers.parse('fulfilment is dispatched exactly once with event id "{event_id}"'))
def _then_fulfilment_once(event_id: str, request: Any) -> None:
    fulfil = _fulfil_mock(request)
    fulfil.assert_awaited_once()
    assert _await_kwargs(fulfil).get("event_id") == event_id


@then(parsers.parse('the fulfilment carries customer email "{email}"'))
def _then_fulfilment_email(email: str, request: Any) -> None:
    fulfil = _fulfil_mock(request)
    assert _await_kwargs(fulfil).get("customer_email") == email


@then(parsers.parse('the fulfilment carries org name "{org}"'))
def _then_fulfilment_org(org: str, request: Any) -> None:
    fulfil = _fulfil_mock(request)
    assert _await_kwargs(fulfil).get("org_name") == org


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}

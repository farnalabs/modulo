"""Stripe purchase webhook — verify Stripe-Signature and fulfil purchases.

URL: POST /api/v1/webhooks/stripe

Auth: the ``Stripe-Signature`` header (``t=<timestamp>,v1=<signature>``). The
signature is an HMAC-SHA256 hex digest of ``"<timestamp>.<raw_body>"`` using the
webhook secret, with a ±300s replay window. Verification is against the RAW
request body bytes and FAILS CLOSED: any missing/invalid signature returns 400
and never reaches fulfilment.

Events handled (both idempotent, keyed on the Stripe event id):
  * ``checkout.session.completed`` — subscription checkout that finished with
    ``payment_status=paid`` (the first payment has been taken).
  * ``invoice.paid`` — the first subscription invoice was paid.

The heavy work (license generation + email) runs as a FastAPI BackgroundTask so
the response returns 200 immediately; Stripe treats a 2xx as delivered.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel

from modulo.core.stripe_fulfilment import fulfil_enterprise_purchase
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["stripe-webhook"])

# Stripe signs webhook payloads with a tolerance window of 5 minutes.
_SIGNATURE_TOLERANCE_SECONDS = 300

# Events that trigger fulfilment (first successful payment for a subscription).
_FULFILMENT_EVENTS = frozenset({"checkout.session.completed", "invoice.paid"})


class StripeWebhookResponse(BaseModel):
    received: bool
    event_id: str


def verify_stripe_signature(
    secret: str,
    signature_header: str | None,
    payload: bytes,
    tolerance_seconds: int = _SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Verify a ``Stripe-Signature`` header against the raw request body.

    Header format: ``t=<unix_ts>,v1=<hex_hmac_sha256>`` where the HMAC input is
    ``"<unix_ts>.<raw_body>"`` keyed with *secret*. Returns True only when the
    signature matches (constant-time) AND the timestamp is within the tolerance
    window (replay protection). Fails closed on any malformed input.
    """
    if not secret or not signature_header:
        return False
    parts: dict[str, str] = {}
    for item in signature_header.split(","):
        key, _, value = item.partition("=")
        if key and value:
            parts[key] = value
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance_seconds:
        return False
    signed_payload = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _event_object(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    return obj if isinstance(obj, dict) else {}


def _extract_customer(event: dict[str, Any]) -> tuple[str | None, str]:
    """Extract ``(customer_email, org_name)`` from a checkout session or invoice event."""
    obj = _event_object(event)
    email: str | None = obj.get("customer_email") or None
    if not email:
        customer_details = obj.get("customer_details")
        if isinstance(customer_details, dict):
            email = customer_details.get("email") or None

    name: str | None = None
    customer_details = obj.get("customer_details")
    if isinstance(customer_details, dict):
        name = customer_details.get("name") or None
    if not name:
        name = obj.get("customer_name") or None

    org_name = f"Customer {email}" if not name and email else name or "Modulo Enterprise customer"
    return email, org_name


@router.post("/stripe", response_model=StripeWebhookResponse)
async def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> StripeWebhookResponse:
    if not settings.stripe_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    raw_body = await request.body()
    signature_header = request.headers.get("Stripe-Signature")
    if not verify_stripe_signature(settings.stripe_webhook_secret, signature_header, raw_body):
        _log.warning("stripe.webhook.invalid_signature")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    try:
        event = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid event payload")

    event_type = event["type"]
    if event_type == "checkout.session.completed":
        session = _event_object(event)
        if session.get("mode") == "subscription" and session.get("payment_status") != "paid":
            # Async subscriptions can complete before the first payment settles;
            # the invoice.paid event is the authoritative "money received" signal.
            _log.info("stripe.webhook.checkout_completed_pending_payment event_id=%s", event["id"])
            return StripeWebhookResponse(received=True, event_id=event["id"])

    if event_type in _FULFILMENT_EVENTS:
        customer_email, org_name = _extract_customer(event)
        if customer_email:
            _log.info("stripe.webhook.fulfilling event=%s event_id=%s", event_type, event["id"])
            background_tasks.add_task(
                fulfil_enterprise_purchase,
                settings,
                event_id=event["id"],
                customer_email=customer_email,
                org_name=org_name,
            )
        else:
            _log.warning(
                "stripe.webhook.no_customer_email event=%s event_id=%s — no fulfilment attempted",
                event_type,
                event["id"],
            )

    return StripeWebhookResponse(received=True, event_id=event["id"])

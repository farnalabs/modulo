"""Stripe purchase webhook — verify Stripe-Signature and fulfil purchases.

URL: POST /api/v1/webhooks/stripe

Auth: the ``Stripe-Signature`` header (``t=<timestamp>,v1=<signature>``). The
signature is an HMAC-SHA256 hex digest of ``"<timestamp>.<raw_body>"`` using the
webhook secret, with a ±300s replay window. Verification is against the RAW
request body bytes and FAILS CLOSED: any missing/invalid signature returns 400
and never reaches fulfilment.

Events handled (all idempotent, keyed on the Stripe event id):
  * ``invoice.paid`` — the authoritative "money received" signal. This is the
    ONLY event that triggers fulfilment. For a card-paid subscription checkout
    Stripe sends BOTH ``checkout.session.completed`` and ``invoice.paid`` for
    the same purchase, so fulfilling on both would issue two licences/emails.
    ``checkout.session.completed`` is therefore treated purely as an ack
    (returns 200, awaits the corresponding ``invoice.paid``).
  * ``checkout.session.completed`` — acknowledged but NEVER fulfilled; the
    first payment is signalled by the matching ``invoice.paid``.

The heavy work (license generation + email) runs as a FastAPI BackgroundTask so
the response returns 200 immediately; Stripe treats a 2xx as delivered.

Chosen licence start (FAR-2146)
-------------------------------
A purchase may state when its licence should begin, as ``starts_at`` in the
checkout/subscription metadata (an ISO 8601 date such as ``2027-03-12``). The
value is validated HERE, before the background task is queued: a past date, one
more than 90 days ahead, or an unparseable value is rejected with 422 and a
machine-readable ``error`` code (``starts_at_in_past`` /
``starts_at_too_far_ahead`` / ``starts_at_malformed``), never silently clamped.
The job is left unclaimed, so the rejection is visible in Stripe's webhook log
rather than becoming a licence that begins on the wrong day.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel

from modulo.api.constants import MSG_NOT_FOUND
from modulo.core.audit_coverage import audited_system, bind_audit_org
from modulo.core.license import DEFAULT_RENEWAL_MODE, RENEWAL_MODE_AUTO
from modulo.core.stripe_fulfilment import (
    StartsAtRejectedError,
    fulfil_team_purchase,
    resolve_starts_at,
)
from modulo.core.trigger_engine import TimestampExpiredError, verify_timestamp
from modulo.db.models.organisation import SYSTEM_ORG_ID
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["stripe-webhook"])

# The ONLY event that triggers fulfilment. Stripe sends BOTH
# ``checkout.session.completed`` and ``invoice.paid`` for a single card-paid
# subscription checkout (distinct event ids), so fulfilling on both would
# double-issue licences. ``invoice.paid`` is the authoritative "money received"
# signal; ``checkout.session.completed`` is ack-only (see the route handler).
_FULFILMENT_EVENTS = frozenset({"invoice.paid"})


class StripeWebhookResponse(BaseModel):
    received: bool
    event_id: str


def verify_stripe_signature(
    secret: str,
    signature_header: str | None,
    payload: bytes,
) -> bool:
    """Verify a ``Stripe-Signature`` header against the raw request body.

    Header format: ``t=<unix_ts>,v1=<hex_hmac_sha256>`` where the HMAC input is
    ``"<unix_ts>.<raw_body>"`` keyed with *secret*. Returns True only when the
    signature matches (constant-time) AND the timestamp is within the ±300s
    replay window (replay protection). Fails closed on any malformed input.
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
    # Reuse the shared ±300s replay-window check from core/trigger_engine rather
    # than re-implementing timestamp tolerance inline (see review feedback).
    try:
        verify_timestamp(timestamp)
    except TimestampExpiredError:
        return False
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    # Compare as UTF-8 bytes: ``hmac.compare_digest`` raises ``TypeError`` on str
    # operands containing non-ASCII characters. ``signature`` is
    # attacker-controlled and arrives latin-1-decoded from the header, so a high
    # byte must fail closed with False instead of escaping as an uncaught 500.
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


def _event_object(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    return obj if isinstance(obj, dict) else {}


def _renewal_mode_for(event: dict[str, Any]) -> str:
    """Return the ``renewal_mode`` the purchase's licence must carry (FAR-2136).

    Stripe's Invoice object already answers this in the event: ``subscription``
    holds the subscription id (or the expanded subscription object) for an
    invoice raised by a subscription, and is ``null`` for a one-off invoice.
    That single field is the whole signal — no Stripe API call and no
    seller-side record of what a customer bought.

    Staleness is accepted deliberately, not engineered around: a subscription
    cancelled AFTER this purchase leaves the minted key claiming ``"auto"``,
    and a self-hosted instance has no phone-home to learn otherwise. The
    consequence is mild and already covered — the customer does not renew, the
    licence lapses into the delivery grace window, and the grace dialog offers
    BOTH the renew path and the buy-a-new-licence path. Tracking cancellation
    would add seller-side state (and a callback the instance cannot receive)
    for a case that flow already handles.
    """
    return RENEWAL_MODE_AUTO if _event_object(event).get("subscription") else DEFAULT_RENEWAL_MODE


def _extract_starts_at(event: dict[str, Any]) -> str | None:
    """Read the chosen licence start from the event's metadata (FAR-2146).

    Stripe carries metadata on several objects and which one holds it depends
    on how the purchase was created, so every location an invoice can carry it
    is checked, in order:

    1. the invoice's own ``metadata``;
    2. ``subscription_details.metadata`` (the subscription's metadata, as
       copied onto the invoice);
    3. ``parent.subscription_details.metadata`` (the newer Invoice API shape).

    Returns the first non-blank ``starts_at`` string, or ``None`` when no
    object states one — which mints from now, exactly as before FAR-2146.
    """
    obj = _event_object(event)
    candidates: list[Any] = [obj.get("metadata")]
    subscription_details = obj.get("subscription_details")
    if isinstance(subscription_details, dict):
        candidates.append(subscription_details.get("metadata"))
    parent = obj.get("parent")
    if isinstance(parent, dict):
        parent_subscription_details = parent.get("subscription_details")
        if isinstance(parent_subscription_details, dict):
            candidates.append(parent_subscription_details.get("metadata"))
    for metadata in candidates:
        if isinstance(metadata, dict):
            raw = metadata.get("starts_at")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


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

    # Prefer a stable org identifier. The Stripe customer id never changes for a
    # given buyer, whereas a display name or email can vary between purchases —
    # build_team_payload derives org_id from org_name, so an unstable name
    # would fragment a customer into multiple orgs across purchases.
    customer_id = obj.get("customer")
    if name:
        org_name = name
    elif isinstance(customer_id, str) and customer_id:
        org_name = f"Customer {customer_id}"
    elif email:
        org_name = f"Customer {email}"
    else:
        org_name = "Modulo Team License customer"
    return email, org_name


# FAR-1516: signature-verified ingress with no tenant at the request boundary
# (the org is resolved later, inside the fulfilment background task), so the
# receipt is recorded with a SYSTEM actor under the system sentinel org. The org
# is published only AFTER the signature verifies — an event must never claim
# signature_verified for a request that failed verification.
@router.post(
    "/stripe",
    dependencies=[
        Depends(
            audited_system(
                "stripe_webhook_received",
                "stripe_event",
                actor_source="signature_verified",
            )
        )
    ],
)
async def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StripeWebhookResponse:
    if not settings.stripe_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_NOT_FOUND)

    raw_body = await request.body()
    signature_header = request.headers.get("Stripe-Signature")
    if not signature_header:
        _log.warning("stripe.webhook.invalid_signature")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")
    if not verify_stripe_signature(settings.stripe_webhook_secret, signature_header, raw_body):
        _log.warning("stripe.webhook.invalid_signature")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")
    bind_audit_org(request, SYSTEM_ORG_ID)

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
        # Deliberate no-op: checkout completion is NOT a fulfilment event —
        # invoice.paid is the authoritative "money received" signal. Stripe
        # sends both for a card-paid subscription; fulfilling on both would
        # generate two licence keys for one purchase. Log for observability
        # and return without dispatching.
        _log.info("stripe.webhook.checkout_completed_noop event_id=%s", event["id"])
        return StripeWebhookResponse(received=True, event_id=event["id"])

    if event_type in _FULFILMENT_EVENTS:
        customer_email, org_name = _extract_customer(event)
        if customer_email:
            renewal_mode = _renewal_mode_for(event)
            starts_at = _extract_starts_at(event)
            # Validate the customer-chosen start AT THE BOUNDARY (FAR-2146), so
            # a bad date is reported synchronously to the caller instead of
            # disappearing into a background task. Nothing is minted and the
            # event is not claimed: the rejection is explicit rather than a
            # silent clamp to "now" or to the 90-day horizon.
            try:
                resolve_starts_at(starts_at)
            except StartsAtRejectedError as exc:
                _log.exception(
                    "stripe.webhook.starts_at_rejected event=%s event_id=%s reason=%s value=%r detail=%s",
                    event_type,
                    event["id"],
                    exc.reason,
                    exc.value,
                    exc.detail,
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"error": exc.reason, "detail": exc.detail},
                ) from exc
            _log.info(
                "stripe.webhook.fulfilling event=%s event_id=%s renewal_mode=%s starts_at=%s",
                event_type,
                event["id"],
                renewal_mode,
                starts_at,
            )
            background_tasks.add_task(
                fulfil_team_purchase,
                settings,
                event_id=event["id"],
                customer_email=customer_email,
                org_name=org_name,
                renewal_mode=renewal_mode,
                starts_at=starts_at,
            )
        else:
            _log.warning(
                "stripe.webhook.no_customer_email event=%s event_id=%s — no fulfilment attempted",
                event_type,
                event["id"],
            )
    else:
        _log.info("stripe.webhook.unhandled_event event=%s event_id=%s", event_type, event["id"])

    return StripeWebhookResponse(received=True, event_id=event["id"])

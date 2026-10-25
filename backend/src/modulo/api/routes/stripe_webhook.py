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

Early-renewal claim (FAR-2109)
------------------------------
``POST /api/v1/webhooks/stripe/renewal-claim`` is the post-purchase step for an
EARLY renewal. The purchase surface here has no pre-payment input: a customer
buys through Stripe and the key arrives by email (this module plus
``core.stripe_fulfilment``), so there is nowhere to collect the previous key at
checkout. The customer therefore presents it afterwards, together with the key
just issued for the purchase. Both are signature-verified — the purchase key is
the record of the purchase and its signed ``licence_id`` anchors idempotency —
so the call needs no session and no tenant at the request boundary. It lives on
this router because it is the purchase flow's ingress and because the
``/api/v1/webhooks`` prefix is CSRF-exempt by default (the call carries no
cookie; its admission basis is the signed keys, not a browser session).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from modulo.api.constants import MSG_NOT_FOUND
from modulo.core.audit_coverage import audited_system, bind_audit_org
from modulo.core.license import DEFAULT_RENEWAL_MODE, RENEWAL_MODE_AUTO
from modulo.core.stripe_fulfilment import (
    RenewalClaimRejectedError,
    RenewalClaimUnavailableError,
    claim_stacked_renewal,
    fulfil_team_purchase,
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
            _log.info(
                "stripe.webhook.fulfilling event=%s event_id=%s renewal_mode=%s",
                event_type,
                event["id"],
                renewal_mode,
            )
            background_tasks.add_task(
                fulfil_team_purchase,
                settings,
                event_id=event["id"],
                customer_email=customer_email,
                org_name=org_name,
                renewal_mode=renewal_mode,
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


class RenewalClaimRequest(BaseModel):
    """A purchase key plus the previous licence whose remaining term to add."""

    #: The key issued for the purchase. Required: it is the proof that a licence
    #: was bought and the anchor for idempotency (its signed ``licence_id``).
    purchase_key: str = Field(min_length=1)
    #: The previous licence. Optional: omitting it is a valid outcome that
    #: returns the purchase key unchanged WITH an explanation (never silent).
    presented_key: str | None = None


class RenewalClaimResponse(BaseModel):
    #: ``stacked`` when the previous term was applied; ``unstacked`` when the
    #: purchase key was returned unchanged (``reason`` says why).
    status: Literal["stacked", "unstacked"]
    #: The licence the customer should install.
    license_key: str
    expires_at: str
    #: The presented licence's expiry that the new term started from, if stacked.
    previous_expires_at: str | None = None
    #: Machine-readable outcome when ``status`` is ``unstacked``.
    reason: str | None = None
    #: Plain-English explanation of what happened and what it cost.
    detail: str
    #: True when an earlier claim for this purchase already produced this key
    #: (a retry, not a second extension).
    repeated: bool = False


# Published in the OpenAPI document (FAR-2114): the customer-facing claim page
# consumes the generated `frontend/src/lib/api/schema.ts` types, so this route
# must appear there. Publishing it is also what makes the endpoint discoverable
# to the public claim surface; the caller is admitted by the signed keys, not a
# session (see the module docstring).
@router.post(
    "/stripe/renewal-claim",
    dependencies=[
        Depends(
            audited_system(
                "license_renewal_claimed",
                "license",
                actor_source="license_signature_verified",
            )
        )
    ],
    response_model=RenewalClaimResponse,
    include_in_schema=True,
)
async def stripe_renewal_claim(
    req: RenewalClaimRequest,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RenewalClaimResponse:
    """Stack an early renewal onto a customer-presented previous licence.

    The expiry is read only from a signature-verified key — never from the
    request body — and there is no start-date parameter, so backdating cannot be
    expressed: the rule is ``max(now, presented_expiry) + term``, implemented
    once in ``license_signing.build_team_payload``'s ``expires_from`` seam.

    A rejected claim RAISES (it never returns a failure-shaped response) so the
    ``audited_system`` event is recorded with ``outcome="error"``.
    """
    try:
        claim = await claim_stacked_renewal(
            settings,
            purchase_key=req.purchase_key,
            presented_key=req.presented_key,
        )
    except RenewalClaimRejectedError as exc:
        if exc.purchase_verified:
            # The purchase key verified, so the attempt is attributable enough
            # to record. The purchaser has no account on this deployment, so
            # there is no tenant org to bind — use the system sentinel.
            bind_audit_org(request, SYSTEM_ORG_ID)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            # House error shape: ``error`` becomes the RFC 9457 ``code`` and
            # ``detail`` the human prose, so a client discriminates on the
            # machine-readable reason instead of substring-matching prose.
            detail={
                "error": exc.reason,
                "detail": exc.detail,
            },
        ) from exc
    except RenewalClaimUnavailableError as exc:
        bind_audit_org(request, SYSTEM_ORG_ID)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "renewal_claim_unavailable",
                "detail": str(exc),
            },
        ) from exc

    bind_audit_org(request, SYSTEM_ORG_ID)
    return RenewalClaimResponse(
        status="stacked" if claim.stacked else "unstacked",
        license_key=claim.license_key,
        expires_at=claim.expires_at,
        previous_expires_at=claim.previous_expires_at,
        reason=claim.reason,
        detail=claim.detail,
        repeated=claim.repeated,
    )

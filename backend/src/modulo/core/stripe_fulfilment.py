"""Stripe purchase fulfilment — idempotent license generation + email delivery.

Flow (FAR-178/180): Stripe webhook -> verify signature (in the route) ->
generate an Ed25519-signed enterprise license -> email it to the customer ->
claim the event id. Runs as a FastAPI BackgroundTask so the webhook responds
200 immediately.

Only ``invoice.paid`` triggers fulfilment — the authoritative "money received"
signal. ``checkout.session.completed`` is deliberately a no-op in the route:
Stripe sends both events for a card-paid subscription, and fulfilling on both
would generate two licence keys for one purchase.

Idempotency: event ids are claimed in Redis (SETNX, 90-day TTL) so Stripe's
automatic retries and any manual replay never double-fulfil. The claim happens
ONLY AFTER a successful email send: a transient SMTP failure leaves the event
unclaimed, so Stripe's automatic retries re-attempt and the customer
eventually gets a key instead of permanently losing it. When Redis is
unavailable a process-local set is used — single-instance only. Production
runs Redis, so the limitation does not apply there.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging

from redis.asyncio import Redis

from modulo.core.email_service import EmailSendingError, send_email
from modulo.core.license import parse_and_verify
from modulo.core.license_signing import LicenseSigningError, generate_enterprise_license
from modulo.settings import Settings

_log = logging.getLogger(__name__)

# Idempotency key TTL — far beyond Stripe's retry window (hours-to-days) so
# duplicate deliveries and manual replays never double-fulfil.
_IDEMPOTENCY_TTL_SECONDS = 90 * 24 * 3600

_RENEWAL_NOTE = "Your Modulo Enterprise license renews annually at list price."

# Process-local fallback for event-id idempotency when Redis is unavailable
# (single process only; production uses Redis).
_processed_event_ids: set[str] = set()


def _licence_email_html(license_key: str, expires_at: str) -> str:
    key_html = html.escape(license_key)
    expiry_html = html.escape(expires_at or "N/A")
    return f"""\
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;">
  <p>Thank you for purchasing <strong>Modulo Enterprise</strong>. Your license key is:</p>
  <pre style="background:#f3f4f6;padding:12px;border-radius:6px;overflow-x:auto;">MODULO_LICENSE_KEY={key_html}</pre>
  <p>This license is valid until <strong>{expiry_html}</strong>.</p>
  <p>To activate it:</p>
  <ol>
    <li>Set <code>MODULO_LICENSE_KEY</code> to the value above in your Modulo instance's environment.</li>
    <li>Restart the service, then confirm the <strong>Team</strong> badge in the sidebar.</li>
  </ol>
  <p>See the installation guide at https://docs.modulo.run for configuration instructions.</p>
  <p>{html.escape(_RENEWAL_NOTE)}</p>
</body>
</html>
"""


def _licence_email_text(license_key: str, expires_at: str) -> str:
    return (
        "Thank you for purchasing Modulo Enterprise.\n\n"
        f"Your license key is:\n\n  MODULO_LICENSE_KEY={license_key}\n\n"
        f"This license is valid until {expires_at or 'N/A'}.\n\n"
        "To activate it, set MODULO_LICENSE_KEY to the value above in your "
        "Modulo instance's environment and restart the service. See "
        "https://docs.modulo.run for full instructions.\n\n"
        f"{_RENEWAL_NOTE}"
    )


async def _claim_event_id(settings: Settings, event_id: str) -> bool:
    """Atomically claim *event_id*. Returns True if newly claimed, False if already seen."""
    if settings.redis_url:
        redis = None
        try:
            redis = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            claimed = await redis.set(
                f"stripe:fulfilled:{event_id}",
                "1",
                nx=True,
                ex=_IDEMPOTENCY_TTL_SECONDS,
            )
            return bool(claimed)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("stripe.fulfilment.redis_claim_failed event_id=%s", event_id, exc_info=True)
        finally:
            if redis is not None:
                with contextlib.suppress(Exception):
                    await redis.aclose()
    if event_id in _processed_event_ids:
        return False
    _processed_event_ids.add(event_id)
    return True


async def _is_event_claimed(settings: Settings, event_id: str) -> bool:
    """Read-only check for whether *event_id* has already been fulfilled.

    Unlike :func:`_claim_event_id` this does NOT claim — it is the pre-email
    duplicate guard. The claim itself happens only AFTER the email send, so a
    transient SMTP failure leaves the event unclaimed and Stripe's retries can
    re-attempt. A duplicate delivery is skipped BEFORE emailing.
    """
    if settings.redis_url:
        redis = None
        try:
            redis = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            return bool(await redis.exists(f"stripe:fulfilled:{event_id}"))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("stripe.fulfilment.redis_check_failed event_id=%s", event_id, exc_info=True)
        finally:
            if redis is not None:
                with contextlib.suppress(Exception):
                    await redis.aclose()
    return event_id in _processed_event_ids


async def email_enterprise_license(settings: Settings, to: str, license_key: str, expires_at: str) -> bool:
    """Send the enterprise license key to *to*.

    Returns True on success, False on failure — never raises. The caller
    (``fulfil_enterprise_purchase``) relies on the bool to decide whether the
    event may be claimed: a False result leaves the event unclaimed so Stripe's
    retries re-attempt the email.
    """
    try:
        await asyncio.to_thread(
            send_email,
            settings,
            [to],
            "Your Modulo Enterprise license",
            _licence_email_html(license_key, expires_at),
            _licence_email_text(license_key, expires_at),
        )
        return True
    except EmailSendingError:
        _log.exception("stripe.fulfilment.email_failed to=%s", to)
        return False
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("stripe.fulfilment.email_unexpected_error to=%s", to)
        return False


async def fulfil_enterprise_purchase(
    settings: Settings,
    *,
    event_id: str,
    customer_email: str,
    org_name: str,
    term_months: int = 12,
    send_key_email: bool = True,
) -> str | None:
    """Idempotently fulfil a Stripe purchase: generate + email an enterprise license.

    Ordering (FAR-180): generate -> duplicate-check -> email -> claim. The event
    id is claimed ONLY AFTER a successful email send, so a transient SMTP
    failure (``email_enterprise_license`` returns False) leaves the event
    unclaimed and Stripe's automatic retries re-attempt the fulfilment — the
    customer never permanently loses their key to an SMTP outage. A duplicate
    delivery (already claimed) is detected BEFORE emailing and returns ``None``
    without re-sending. Returns the generated license key, or ``None`` when the
    event was already fulfilled or generation/email failed.
    """
    try:
        license_key = generate_enterprise_license(
            org_name,
            term_months=term_months,
            private_key_hex=settings.modulo_license_private_key or None,
        )
    except (LicenseSigningError, ValueError):
        _log.exception("stripe.fulfilment.license_generation_failed event_id=%s", event_id)
        return None

    if await _is_event_claimed(settings, event_id):
        _log.info("stripe.fulfilment.duplicate_event event_id=%s", event_id)
        return None

    validation = parse_and_verify(license_key)
    expires_at = ""
    if validation.valid and validation.license_data is not None:
        expires_at = validation.license_data.expires_at
    else:
        _log.warning(
            "stripe.fulfilment.generated_license_failed_verification event_id=%s error=%s",
            event_id,
            validation.error,
        )

    if send_key_email and not await email_enterprise_license(settings, customer_email, license_key, expires_at):
        _log.error(
            "stripe.fulfilment.email_failed_event_not_claimed event_id=%s — "
            "event left unclaimed so Stripe's retries will re-attempt",
            event_id,
        )
        return None

    if not await _claim_event_id(settings, event_id):
        _log.info("stripe.fulfilment.duplicate_event event_id=%s", event_id)
        return None

    _log.info("stripe.fulfilment.license_generated event_id=%s org_name=%s", event_id, org_name)
    return license_key

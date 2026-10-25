"""Stripe purchase fulfilment — idempotent license generation + email delivery.

Flow (FAR-178/180): Stripe webhook -> verify signature (in the route) ->
generate an Ed25519-signed team license -> email it to the customer ->
claim the Stripe event id. Runs as a FastAPI BackgroundTask so the webhook
responds 200 immediately.

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

Early-renewal stacking (FAR-2109)
---------------------------------
A purchase is minted at the charge date, so a renewal charged on time already
lands at ``now + term`` — the same instant ``max(now, previous_expiry) + term``
produces when the charge lands at period end. That equivalence is why an AUTO
renewal needs no upload.

Stacking only matters for an EARLY renewal, where days remain on the previous
licence and ``now + term`` would silently burn them. There is deliberately no
seller-side register of issued licences: a licence key is itself a signed,
tamper-evident record of its expiry, so the customer presents it. The
``purchase_key`` they were just issued is the record of the purchase; its
signed ``licence_id`` is the idempotency key, so one purchase can never mint
two stacked licences — and a claim-minted key can never be used as proof of a
second purchase, because the stacked key KEEPS the purchase's ``licence_id``.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

from modulo.core.email_service import EmailSendingError, send_email
from modulo.core.license import (
    DEFAULT_RENEWAL_MODE,
    REASON_MALFORMED,
    LicenseData,
    LicenseValidation,
    parse_and_verify,
)
from modulo.core.license_signing import LicenseSigningError, generate_team_license
from modulo.settings import Settings

_log = logging.getLogger(__name__)

# Idempotency key TTL — far beyond Stripe's retry window (hours-to-days) so
# duplicate deliveries and manual replays never double-fulfil.
_IDEMPOTENCY_TTL_SECONDS = 90 * 24 * 3600

_RENEWAL_NOTE = "Your Modulo Team License renews annually at list price."

# Process-local fallback for event-id idempotency when Redis is unavailable
# (single process only; production uses Redis).
_processed_event_ids: set[str] = set()

# ---------------------------------------------------------------------------
# Early-renewal claim reasons (FAR-2109)
#
# A presented key that fails signature/format/expiry checks REUSES the
# ``REASON_*`` vocabulary from ``modulo.core.license`` (malformed /
# signature_mismatch / anchor_mismatch / expired) — this module never invents a
# verification vocabulary. The constants below are the failures that only the
# claim can detect, so they are stated here as stable, machine-readable reasons.
# ---------------------------------------------------------------------------

#: The presented key belongs to a different organisation than the purchase key.
REASON_ORG_MISMATCH = "org_mismatch"
#: The "purchase key" lacks the signed claims (``licence_id``/``term``) a
#: purchase carries, so it cannot identify or size a renewal.
REASON_NOT_A_PURCHASE_KEY = "not_a_purchase_key"
#: The presented key IS the purchase key — stacking it would grant a second
#: term for one purchase.
REASON_SAME_LICENCE = "same_licence"
#: No previous licence was presented; the purchase key stands unstacked.
REASON_NO_PRESENTED_KEY = "no_presented_key"
#: The key has no ``expires_at`` (a perpetual licence), so there is no term to
#: extend or stack onto.
REASON_NO_EXPIRY = "no_expiry"

#: Redis key prefix for the per-purchase stacked-key record.
_RENEWAL_CLAIM_KEY_PREFIX = "stripe:renewal-claim:"

# Process-local fallback for renewal-claim idempotency when Redis is
# unavailable (single process only; production uses Redis).
_renewal_claims: dict[str, dict[str, str]] = {}


def _licence_email_html(license_key: str, expires_at: str) -> str:
    key_html = html.escape(license_key)
    expiry_html = html.escape(expires_at or "N/A")
    return f"""\
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;">
  <p>Thank you for purchasing <strong>Modulo Team License</strong>. Your license key is:</p>
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
        "Thank you for purchasing Modulo Team License.\n\n"
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


async def email_team_license(settings: Settings, to: str, license_key: str, expires_at: str) -> bool:
    """Send the team license key to *to*.

    Returns True on success, False on failure — never raises. The caller
    (``fulfil_team_purchase``) relies on the bool to decide whether the
    event may be claimed: a False result leaves the event unclaimed so Stripe's
    retries re-attempt the email.
    """
    try:
        return await asyncio.to_thread(
            send_email,
            settings,
            [to],
            "Your Modulo Team License",
            _licence_email_html(license_key, expires_at),
            _licence_email_text(license_key, expires_at),
        )
    except EmailSendingError:
        _log.exception("stripe.fulfilment.email_failed to=%s", to)
        return False
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("stripe.fulfilment.email_unexpected_error to=%s", to)
        return False


async def _check_duplicate_event(settings: Settings, event_id: str) -> bool:
    """Check if event was already claimed. Returns True if duplicate."""
    if await _is_event_claimed(settings, event_id):
        _log.info("stripe.fulfilment.duplicate_event event_id=%s", event_id)
        return True
    return False


async def _generate_and_verify_license(
    settings: Settings,
    org_name: str,
    term_months: int,
    event_id: str,
    renewal_mode: str = DEFAULT_RENEWAL_MODE,
) -> tuple[str, str] | None:
    """Generate license key and verify it. Returns (license_key, expires_at) or None on failure."""
    try:
        license_key = generate_team_license(
            org_name,
            term_months=term_months,
            private_key_hex=settings.modulo_license_private_key or None,
            renewal_mode=renewal_mode,
        )
    except ValueError:
        _log.exception("stripe.fulfilment.license_generation_failed event_id=%s", event_id)
        return None

    validation = parse_and_verify(license_key)
    if not validation.valid or validation.license_data is None:
        _log.error(
            "stripe.fulfilment.generated_license_failed_verification event_id=%s error=%s "
            "reason=%s hint=%s — unverifiable key not emailed; event left unclaimed so Stripe's "
            "retries re-attempt. The signing key (MODULO_LICENSE_PRIVATE_KEY) and the trust "
            "anchor (MODULO_LICENSE_PUBLIC_KEY) must be the two halves of one Ed25519 keypair.",
            event_id,
            validation.error,
            validation.reason,
            validation.hint,
        )
        return None
    return license_key, validation.license_data.expires_at


async def _send_license_email(
    settings: Settings, customer_email: str, license_key: str, expires_at: str, event_id: str
) -> bool:
    """Send license email. Returns True on success, False on failure."""
    if not await email_team_license(settings, customer_email, license_key, expires_at):
        _log.error(
            "stripe.fulfilment.email_failed_event_not_claimed event_id=%s — "
            "event left unclaimed so Stripe's retries will re-attempt",
            event_id,
        )
        return False
    return True


async def _claim_event(settings: Settings, event_id: str) -> bool:
    """Claim event ID after successful email. Returns True if newly claimed."""
    if not await _claim_event_id(settings, event_id):
        _log.info("stripe.fulfilment.duplicate_event event_id=%s", event_id)
        return False
    return True


async def fulfil_team_purchase(
    settings: Settings,
    *,
    event_id: str,
    customer_email: str,
    org_name: str,
    term_months: int = 12,
    send_key_email: bool = True,
    renewal_mode: str = DEFAULT_RENEWAL_MODE,
) -> str | None:
    """Idempotently fulfil a Stripe purchase: generate + email a team license.

    ``renewal_mode`` is the mode the emitted key will carry (FAR-2136). The
    caller derives it from the Stripe event: a subscription invoice mints
    ``"auto"``, a one-off mints ``"manual"``. The default stays ``"manual"`` so
    a caller that does not state the mode can never promise a renewal that was
    not bought.

    Ordering (FAR-180): duplicate-check -> generate -> email -> claim. The event
    id is claimed ONLY AFTER a successful email send, so a transient SMTP
    failure (``email_team_license`` returns False) leaves the event
    unclaimed and Stripe's automatic retries re-attempt the fulfilment — the
    customer never permanently loses their key to an SMTP outage. A duplicate
    delivery (already claimed) is detected BEFORE generation/emailing and
    returns ``None`` without re-sending. Returns the generated license key, or
    ``None`` when the event was already fulfilled or generation/email failed.
    """
    if await _check_duplicate_event(settings, event_id):
        return None

    license_data = await _generate_and_verify_license(settings, org_name, term_months, event_id, renewal_mode)
    if license_data is None:
        return None
    license_key, expires_at = license_data

    if send_key_email and not await _send_license_email(settings, customer_email, license_key, expires_at, event_id):
        return None

    if not await _claim_event(settings, event_id):
        return None

    _log.info("stripe.fulfilment.license_generated event_id=%s org_name=%s", event_id, org_name)
    return license_key


# ---------------------------------------------------------------------------
# Early-renewal claim (FAR-2109)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenewalClaim:
    """The outcome of an early-renewal claim.

    ``stacked`` is the single authoritative flag: ``True`` means the returned
    key's term starts at the previous licence's expiry (or at now, when that
    expiry had already passed); ``False`` means the purchase key was returned
    unchanged with ``reason`` explaining why nothing was stacked.
    """

    license_key: str
    expires_at: str
    stacked: bool
    detail: str
    reason: str | None = None
    previous_expires_at: str | None = None
    #: True when an earlier claim for this purchase already minted the returned
    #: key (a retry/duplicate request, not a second extension).
    repeated: bool = False


class RenewalClaimRejectedError(Exception):
    """A claim that cannot be honoured; the caller must be told WHY.

    ``purchase_verified`` states whether the purchase key itself verified —
    the route uses it to decide whether the attempt is attributable enough to
    publish an audit org for. ``purchase_expires_at`` is the (unchanged)
    expiry the customer still has, so a rejection is always recoverable.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        purchase_verified: bool = True,
        purchase_expires_at: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.purchase_verified = purchase_verified
        self.purchase_expires_at = purchase_expires_at


class RenewalClaimUnavailableError(Exception):
    """A server-side fault prevented a claim: nothing was minted or published."""


#: Fields of a stored stacked-key record. Every one is required: a partial
#: record is an integrity fault, not a cache miss.
_CLAIM_RECORD_FIELDS = frozenset({"license_key", "expires_at", "previous_expires_at"})


def _decode_claim_record(raw: str | bytes) -> dict[str, str]:
    """Decode a stored claim record, failing CLOSED on a corrupt one.

    A corrupt record means idempotency cannot be trusted for this purchase. The
    safe direction is to refuse the claim (nothing is minted, the customer
    retries later) rather than to mint a second stacked key and hand out
    another term.
    """
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RenewalClaimUnavailableError(
            "The stored renewal record for this purchase is unreadable, so no new licence was issued. "
            "Try again later; if it keeps failing, contact support with the key from your purchase email."
        ) from exc
    if not isinstance(record, dict) or any(not isinstance(record.get(field), str) for field in _CLAIM_RECORD_FIELDS):
        raise RenewalClaimUnavailableError(
            "The stored renewal record for this purchase is incomplete, so no new licence was issued. "
            "Try again later; if it keeps failing, contact support with the key from your purchase email."
        )
    return {field: str(record[field]) for field in _CLAIM_RECORD_FIELDS}


def _renewal_claim_ttl_seconds(term_months: int) -> int:
    """TTL for a purchase's stacked-key record.

    The record must OUTLIVE the licence it corrects: once it expires, the same
    purchase could be claimed again and — for a previous licence that had
    lapsed in the meantime — extension would resume from a LATER ``now``,
    granting free time. Two months of slack covers the delivery grace and the
    month-arithmetic clamp.
    """
    return (term_months + 2) * 31 * 24 * 3600


def _aware_expiry(expires_at: str) -> datetime:
    """Parse a signed expiry into an aware instant.

    A naive ``expires_at`` is read as LOCAL time — the convention
    ``core.license._expiry_failure`` and ``admin_license.derive_licence_state``
    already use for this key — and never compared against an aware instant.
    """
    expiry = datetime.fromisoformat(expires_at)
    return expiry if expiry.tzinfo is not None else expiry.astimezone()


async def _read_renewal_claim(settings: Settings, licence_id: str) -> dict[str, str] | None:
    """Read the stacked key already issued for *licence_id*, if any."""
    if settings.redis_url:
        redis = None
        try:
            redis = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            raw = await redis.get(f"{_RENEWAL_CLAIM_KEY_PREFIX}{licence_id}")
            if raw:
                return _decode_claim_record(raw)
        except asyncio.CancelledError:
            raise
        except RenewalClaimUnavailableError:
            raise
        except Exception:
            _log.warning("stripe.renewal_claim.redis_read_failed licence_id=%s", licence_id, exc_info=True)
        finally:
            if redis is not None:
                with contextlib.suppress(Exception):
                    await redis.aclose()
    return _renewal_claims.get(licence_id)


async def _store_renewal_claim(
    settings: Settings,
    licence_id: str,
    record: dict[str, str],
    ttl_seconds: int,
) -> dict[str, str]:
    """Atomically record *record* as the stacked key for *licence_id*.

    SETNX makes this a first-writer-wins race: if a concurrent claim stored its
    key first, the winner's record is returned and the caller publishes THAT
    key, so one purchase still yields exactly one stacked licence.

    When Redis is unavailable the process-local fallback is used — like the
    event-id idempotency above, that is single-instance only; production runs
    Redis, so the limitation does not apply there.
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
            created = await redis.set(
                f"{_RENEWAL_CLAIM_KEY_PREFIX}{licence_id}",
                json.dumps(record),
                nx=True,
                ex=ttl_seconds,
            )
            if created:
                return record
            raw = await redis.get(f"{_RENEWAL_CLAIM_KEY_PREFIX}{licence_id}")
            if raw:
                return _decode_claim_record(raw)
            raise RenewalClaimUnavailableError(
                "Another claim for this purchase recorded its key at the same moment and it could not be "
                "read back, so no new licence was issued. Try again — the retry returns the key already issued."
            )
        except asyncio.CancelledError:
            raise
        except RenewalClaimUnavailableError:
            raise
        except Exception:
            _log.warning("stripe.renewal_claim.redis_store_failed licence_id=%s", licence_id, exc_info=True)
        finally:
            if redis is not None:
                with contextlib.suppress(Exception):
                    await redis.aclose()
    stored = _renewal_claims.get(licence_id)
    if stored is not None:
        return stored
    _renewal_claims[licence_id] = record
    return record


def _purchase_rejection(validation: LicenseValidation) -> RenewalClaimRejectedError:
    """Reject a purchase key that does not verify — never mint on it."""
    hint = f" {validation.hint}" if validation.hint else ""
    return RenewalClaimRejectedError(
        validation.reason or REASON_MALFORMED,
        "The key issued for this purchase could not be verified, so no licence was issued. "
        "Check that you pasted the whole key from your purchase email and that this deployment "
        f"trusts the authority that signed it.{hint}",
        purchase_verified=False,
    )


def _stacked_detail(previous: LicenseData, purchase: LicenseData, expires_at: str, now: datetime) -> str:
    """Explain the stacking outcome in the customer's own terms."""
    previous_expiry = _aware_expiry(previous.expires_at)
    if previous_expiry > now:
        return (
            "The unused days on your previous licence were added: this licence now expires "
            f"{expires_at}, instead of {purchase.expires_at} without the previous term."
        )
    return (
        "Your previous licence had already expired, so this renewal starts a fresh term from today and "
        f"expires {expires_at} — no days were lost. Without the previous key it would have expired "
        f"{purchase.expires_at}."
    )


def _verified_purchase(purchase_key: str) -> LicenseData:
    """Verify the purchase key and require the claims a renewal needs.

    A purchase key is the proof that a licence was bought and the idempotency
    anchor, so nothing may be minted from one that does not verify or that
    lacks the signed ``licence_id``/``term`` claims.
    """
    purchase_validation = parse_and_verify(purchase_key)
    if not purchase_validation.valid or purchase_validation.license_data is None:
        raise _purchase_rejection(purchase_validation)
    purchase = purchase_validation.license_data

    if purchase.licence_id is None or purchase.term is None:
        raise RenewalClaimRejectedError(
            REASON_NOT_A_PURCHASE_KEY,
            "This key does not carry the claims a purchase key needs before it can be renewed (a licence id "
            "and a term), so no licence was issued. Paste the full key from your latest purchase email.",
        )
    if not purchase.expires_at:
        raise RenewalClaimRejectedError(
            REASON_NO_EXPIRY,
            "This licence has no expiry date, so there is no term to extend and no new licence was issued.",
            purchase_expires_at=None,
        )
    return purchase


def _verified_previous(candidate: str, purchase: LicenseData) -> LicenseData:
    """Verify the presented licence and prove it may be stacked onto the purchase.

    Every check here refuses to stack rather than correcting an unverifiable or
    mismatched key: the purchase key the customer already holds is unchanged.
    """
    presented_validation = parse_and_verify(candidate)
    if not presented_validation.valid or presented_validation.license_data is None:
        reason = presented_validation.reason or REASON_MALFORMED
        hint = f" {presented_validation.hint}" if presented_validation.hint else ""
        raise RenewalClaimRejectedError(
            reason,
            f"The previous licence key you presented could not be verified ({reason}), so it was not stacked "
            "and no new licence was issued. The key from this purchase is unchanged and still expires "
            f"{purchase.expires_at}; if you meant to renew early, re-submit the claim with your current, "
            f"valid licence key.{hint}",
            purchase_expires_at=purchase.expires_at,
        )
    previous = presented_validation.license_data

    if previous.org_id != purchase.org_id:
        raise RenewalClaimRejectedError(
            REASON_ORG_MISMATCH,
            "The previous licence key belongs to a different organisation than the key issued for this "
            "purchase, so it was not stacked and no new licence was issued. The purchase key is unchanged "
            f"and expires {purchase.expires_at}.",
            purchase_expires_at=purchase.expires_at,
        )
    if previous.licence_id is not None and previous.licence_id == purchase.licence_id:
        raise RenewalClaimRejectedError(
            REASON_SAME_LICENCE,
            "The key you presented is the key issued for this purchase, so stacking it would grant a second "
            "term for one purchase. No new licence was issued; the purchase key still expires "
            f"{purchase.expires_at}.",
            purchase_expires_at=purchase.expires_at,
        )
    if not previous.expires_at:
        raise RenewalClaimRejectedError(
            REASON_NO_EXPIRY,
            "The previous licence key you presented has no expiry date, so there is nothing to stack onto. "
            f"No new licence was issued; the purchase key still expires {purchase.expires_at}.",
            purchase_expires_at=purchase.expires_at,
        )
    return previous


def _mint_stacked_licence(
    settings: Settings,
    purchase: LicenseData,
    previous: LicenseData,
) -> tuple[str, LicenseData]:
    """Sign the stacked licence, failing closed if our own anchor rejects it."""
    assert purchase.licence_id is not None  # narrowed by _verified_purchase
    assert purchase.term is not None
    try:
        stacked_key = generate_team_license(
            # ``org_name`` only derives ``org_id`` when none is supplied; this
            # renewal carries the purchase's verified ``org_id`` so the name is
            # not used and must not imply a new customer identity.
            "Modulo Team License renewal",
            term_months=purchase.term,
            private_key_hex=settings.modulo_license_private_key or None,
            org_id=purchase.org_id,
            features=purchase.features,
            renewal_mode=purchase.renewal_mode,
            licence_id=purchase.licence_id,
            expires_from=_aware_expiry(previous.expires_at),
        )
    except LicenseSigningError as exc:
        _log.error("stripe.renewal_claim.signing_failed licence_id=%s", purchase.licence_id, exc_info=True)
        raise RenewalClaimUnavailableError(
            "This deployment cannot sign a replacement licence (no signing key is configured), so no new "
            "licence was issued. Your purchase key is unchanged."
        ) from exc

    minted = parse_and_verify(stacked_key)
    if not minted.valid or minted.license_data is None:
        # Same fail-closed rule as ``fulfil_team_purchase``: a key we just signed
        # that our OWN anchor rejects must never be handed to a customer.
        _log.error(
            "stripe.renewal_claim.generated_license_failed_verification licence_id=%s error=%s reason=%s "
            "hint=%s — unverifiable stacked key not issued. MODULO_LICENSE_PRIVATE_KEY and "
            "MODULO_LICENSE_PUBLIC_KEY must be the two halves of one Ed25519 keypair.",
            purchase.licence_id,
            minted.error,
            minted.reason,
            minted.hint,
        )
        raise RenewalClaimUnavailableError(
            "This deployment could not verify the replacement licence it just signed, so it was not issued. "
            "Your purchase key is unchanged; try again later."
        )
    return stacked_key, minted.license_data


def _unstacked_claim(purchase_key: str, purchase: LicenseData) -> RenewalClaim:
    """Return the purchase key unchanged when no previous licence was presented."""
    return RenewalClaim(
        license_key=purchase_key,
        expires_at=purchase.expires_at,
        stacked=False,
        reason=REASON_NO_PRESENTED_KEY,
        detail=(
            "No previous licence was applied, so no time was added: the key from this purchase already "
            f"grants a full term and expires {purchase.expires_at}. If you renewed before your previous "
            "licence expired, re-submit this claim with that previous licence key and the remaining days "
            "will be added."
        ),
    )


def _repeated_claim(stored: dict[str, str]) -> RenewalClaim:
    """Return the key already issued for this purchase (a retry, not an extension)."""
    return RenewalClaim(
        license_key=stored["license_key"],
        expires_at=stored["expires_at"],
        stacked=True,
        previous_expires_at=stored["previous_expires_at"],
        repeated=True,
        detail=(
            "This purchase was already stacked onto a previous licence, so the key issued for it is "
            "returned unchanged rather than extended a second time."
        ),
    )


def _fresh_claim(
    previous: LicenseData,
    purchase: LicenseData,
    stored: dict[str, str],
    now: datetime,
) -> RenewalClaim:
    """Return the stacked key this claim minted and stored."""
    return RenewalClaim(
        license_key=stored["license_key"],
        expires_at=stored["expires_at"],
        stacked=True,
        previous_expires_at=stored["previous_expires_at"],
        repeated=False,
        detail=_stacked_detail(previous, purchase, stored["expires_at"], now),
    )


async def claim_stacked_renewal(
    settings: Settings,
    *,
    purchase_key: str,
    presented_key: str | None,
) -> RenewalClaim:
    """Stack an early renewal onto the licence the customer presents.

    ``purchase_key`` is the key issued for the purchase — the proof that a
    licence was bought, and the idempotency anchor (its signed ``licence_id``).
    ``presented_key`` is the previous licence the customer wants its remaining
    term carried over from; omitting it is a legitimate outcome that returns
    the purchase key unchanged WITH an explanation (never a silent fallback).

    The expiry is read ONLY from a signature-verified presented key; there is
    no caller-supplied start date, so backdating cannot be expressed. The new
    expiry is ``max(now, presented_expiry) + term`` — delegated wholesale to
    ``license_signing.build_team_payload``'s ``expires_from`` seam. The stacked
    key preserves the purchase's ``licence_id``, so it is the same entitlement
    corrected, not a second licence.
    """
    purchase = _verified_purchase(purchase_key)
    assert purchase.licence_id is not None  # narrowed by _verified_purchase
    assert purchase.term is not None

    candidate = (presented_key or "").strip()
    if not candidate:
        _log.info("stripe.renewal_claim.no_presented_key licence_id=%s", purchase.licence_id)
        return _unstacked_claim(purchase_key, purchase)

    previous = _verified_previous(candidate, purchase)

    stored = await _read_renewal_claim(settings, purchase.licence_id)
    if stored is not None:
        _log.info("stripe.renewal_claim.reused licence_id=%s", purchase.licence_id)
        return _repeated_claim(stored)

    stacked_key, minted = _mint_stacked_licence(settings, purchase, previous)

    record = {
        "license_key": stacked_key,
        "expires_at": minted.expires_at,
        "previous_expires_at": previous.expires_at,
    }
    stored = await _store_renewal_claim(
        settings,
        purchase.licence_id,
        record,
        _renewal_claim_ttl_seconds(purchase.term),
    )
    now = datetime.now(UTC)
    _log.info(
        "stripe.renewal_claim.stacked licence_id=%s purchase_expires_at=%s expires_at=%s",
        purchase.licence_id,
        purchase.expires_at,
        stored["expires_at"],
    )
    if stored is not record:
        return _repeated_claim(stored)
    return _fresh_claim(previous, purchase, stored, now)

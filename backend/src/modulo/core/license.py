"""License key parsing, verification, and storage.

License keys are base64-encoded, Ed25519-signed JSON payloads in the
format::

    <base64(payload)>.<base64(signature)>

The payload is a JSON object with keys:

    tier          — "community" | "team"
    features      — list of feature flag names
    expires_at    — ISO 8601 expiration timestamp
    org_id        — organisation identifier
    renewal_mode  — "auto" | "manual" (FAR-2075; absent on older keys)
    term          — licence term in MONTHS (FAR-2075; absent on older keys)
    licence_id    — opaque stable identifier for this licence (FAR-2075)
    issued_at     — ISO 8601 mint timestamp (FAR-2075)
    starts_at     — ISO 8601, timezone-aware instant this licence becomes
                    active (FAR-2146; absent on older keys and on a key minted
                    to start immediately)

The FAR-2075 and FAR-2146 claims are ADDITIVE. A key minted before FAR-2075
carries only the first four and still verifies unchanged: ``renewal_mode``
defaults to ``"manual"`` (never ``"auto"`` — auto promises a replacement key
is on its way), and ``term`` / ``licence_id`` / ``issued_at`` resolve to
``None``.

``starts_at`` is likewise additive. When it is ABSENT the licence is valid from
``issued_at`` — exactly the behaviour before FAR-2146 — so every existing key
keeps verifying and keeps behaving identically. A key carrying a FUTURE
``starts_at`` is valid but not yet active: it is parked (see
:func:`store_license`) and becomes the active licence when its start arrives,
without operator action.

INSTANCE-SCOPE PERSISTENCE (FAR-2153)
-------------------------------------
The system-scope licence is an INSTANCE setting, not an organisation one, so it
is persisted in the RLS-free ``system_config`` key-value table (the same
instance store that holds the product-analytics instance identity) under
:data:`SYSTEM_LICENSE_CONFIG_KEY`. The in-memory slot
(``_current_license`` / ``_parked_successor``) stays the fast read path; the
persisted row is what makes an install survive a process restart (a rolling
deploy discards process memory on every machine) and what lets the SAQ worker —
a separate process — resolve the same tier as the API.

PRECEDENCE. The persisted system licence is loaded into rung 2 of the ladder
(``core.feature_flags.resolve_effective_license``), which sits ABOVE the
deployment's ``MODULO_LICENSE_KEY`` (rung 3). So the env key is the deployment
FLOOR, an explicit operator install OVERRIDES it, and removing the installed key
falls back to the floor — then to Community. The org-level key (rung 1) still
wins over both.

A separate process observes an install made while it is running by re-reading
the persisted row: :func:`refresh_persisted_system_license` is TTL-throttled,
and both startup paths restore once at boot (``core.license_bootstrap``). The
API also refreshes on its plan-resolution and admin-licence paths; the SAQ
worker refreshes at the start of every job (``_before_process_hook``), which is
the point at which it resolves a tier.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from modulo.core.registry.crypto import verify_signature

#: Fixed delivery grace after ``expires_at``, in days.
#:
#: A self-hosted licence is an OFFLINE SIGNED KEY: a renewal is minted at the
#: charge date and delivered by email, so the customer can legitimately still
#: be holding the previous (now expired) key while the replacement is in
#: transit. Capability therefore survives for this window past ``expires_at``
#: — the grace covers KEY DELIVERY, it does not grant free use. Fourteen days
#: is that delivery window.
#:
#: Deliberately a module-level constant, NOT an env var, a ``Settings`` field or
#: a database column: an operator must not be able to extend the paid
#: capability window by configuration.
LICENCE_GRACE_DAYS = 14

#: How a licence renews, as carried by the signed ``renewal_mode`` claim.
#:
#: A self-hosted instance has no phone-home, so the signed key is the ONLY
#: trustworthy statement of how the licence renews. ``"auto"`` means Farnalabs
#: mints and delivers a replacement at the charge date; ``"manual"`` means the
#: customer must buy/install a new key.
RENEWAL_MODE_AUTO = "auto"
RENEWAL_MODE_MANUAL = "manual"

#: The vocabulary a signed ``renewal_mode`` claim may hold.
RENEWAL_MODES: frozenset[str] = frozenset({RENEWAL_MODE_AUTO, RENEWAL_MODE_MANUAL})

#: Value used when a key carries no (or an unrecognised) ``renewal_mode`` claim.
#:
#: Deliberately NEVER ``auto``: ``auto`` asserts that a replacement key is
#: already on its way, and a key that does not say so must not promise a
#: renewal the customer has not bought (offering a purchase path to someone
#: already paying — or the reverse — is a double charge).
DEFAULT_RENEWAL_MODE = RENEWAL_MODE_MANUAL

#: The BUILT-IN DEVELOPMENT trust anchor (Ed25519 public key, hex-encoded, 64 chars).
#:
#: This is the real public half of the DEVELOPMENT signing keypair held in the
#: Farnalabs vault. It is NOT the anchor Farnalabs signs production licences
#: with: production keys are signed by a separate keypair whose public half is
#: published to customers and must be supplied via ``MODULO_LICENSE_PUBLIC_KEY``
#: (``Settings.modulo_license_public_key``). A production licence therefore
#: CANNOT verify against this key.
#:
#: It stays as the no-configuration default so a developer running the app
#: locally can exercise licensing without an env override. Outside debug, a
#: deployment that configures a licence MUST also configure the production
#: anchor: startup fails closed when a licence key is present without one
#: (FAR-2091), and the install path returns an actionable anchor-mismatch error.
_BUILTIN_DEV_PUBLIC_KEY_HEX: str = "a101dd3f34a806455d25ca247fb84a0a61decff4cf1a467b9456175634b0fc51"

#: Backwards-compatible alias for the historical constant name.
#:
#: Several call sites (and the unit tests) import ``_LICENSE_PUBLIC_KEY_HEX``;
#: keep it pointing at the built-in development anchor so renaming the constant
#: does not silently change what those imports mean.
_LICENSE_PUBLIC_KEY_HEX: str = _BUILTIN_DEV_PUBLIC_KEY_HEX

# Optional override for the public key (set via env var or set_public_key).
#: ``None`` means "no override configured" — the built-in development anchor
#: is in use (and :func:`using_builtin_dev_anchor` reports True).
_configured_public_key_hex: str | None = None

#: Whether the BUILT-IN development key may be used to ACCEPT a licence.
#:
#: Fail-closed: ``False`` by default, so only a process that has explicitly
#: opted in (``set_dev_anchor_trusted(settings.debug)`` at startup) honours a
#: licence signed against the shipped development anchor. An entry point that
#: never configures verification therefore REFUSES such a licence instead of
#: silently trusting a key whose private half ships with the product.
_dev_anchor_trusted: bool = False

#: Actionable refusal returned when a licence would only verify against the
#: built-in development key outside debug.
DEV_ANCHOR_NOT_TRUSTED_ERROR = (
    "License could not be verified against a trusted key: MODULO_LICENSE_PUBLIC_KEY is not "
    "configured, so only the built-in development public key is available and that key is not "
    "a trust anchor outside debug. Set MODULO_LICENSE_PUBLIC_KEY to the Ed25519 public key "
    "(64 hex chars) that signed this license, then restart."
)

#: Actionable startup error for an instance that is configured to ACCEPT a
#: licence (``MODULO_LICENSE_KEY``) while only the development anchor exists.
DEV_ANCHOR_STARTUP_ERROR = (
    "MODULO_LICENSE_KEY is configured but MODULO_LICENSE_PUBLIC_KEY is not (or is the built-in "
    "development key), and debug is off. The built-in development public key is not a trust "
    "anchor outside debug, so no licence can be verified. Set MODULO_LICENSE_PUBLIC_KEY to the "
    "Ed25519 public key (64 hex chars) that signed your licence, or unset MODULO_LICENSE_KEY to "
    "run on the Community tier."
)

# In-memory store for the current validated license.
_current_license: LicenseData | None = None

#: A verified licence whose signed ``starts_at`` is still in the future
#: (FAR-2146). It is VALID but not yet ACTIVE: it never displaces
#: ``_current_license`` before its start, and :func:`get_license` promotes it
#: when the start arrives.
_parked_successor: LicenseData | None = None

#: ``system_config`` key holding the persisted INSTANCE-SCOPE licence (FAR-2153).
#:
#: The value is a JSON object with the two in-memory slots:
#: ``{"active": <raw key>|null, "parked": <raw key>|null}``. Both slots are kept
#: because installing a future-start successor must not discard the licence the
#: instance is running on today (FAR-2146): otherwise a deploy during the park
#: window would drop a paying customer to Community until the successor starts.
#:
#: ``system_config`` is the existing instance store and has NO RLS (the app role
#: owns it), which is what makes it readable from the SAQ worker as well as the
#: API. The org-scope licence uses ``org.settings_json["license_key"]`` instead
#: (FAR-2059) — never this key.
SYSTEM_LICENSE_CONFIG_KEY = "system_license_key"

#: How long this process may serve the in-memory slot before re-reading the
#: persisted instance licence, in seconds.
#:
#: A licence installed while a process is running must become visible without a
#: restart. The API re-reads on its plan-resolution and admin-licence paths; the
#: worker re-reads at the start of every job (``_before_process_hook``), which
#: is where it resolves a tier. The window is deliberately short (a licence
#: change is rare, a stale tier is not acceptable) but non-zero so the read
#: never lands on every request / every job.
SYSTEM_LICENSE_REFRESH_TTL_SECONDS = 15.0


@dataclass(frozen=True)
class PersistedLicenseState:
    """The two system-licence slots as persisted in ``system_config``.

    ``active_key`` is the licence the instance runs on today; ``parked_key`` is
    a verified future-start successor (FAR-2146). Either may be ``None``. The
    state is compared by value so the refresh can tell "unchanged" from
    "installed / rotated / removed" without re-verifying the key on every read.
    """

    active_key: str | None = None
    parked_key: str | None = None

    def is_empty(self) -> bool:
        """Whether neither slot holds a licence (the Community configuration)."""
        return self.active_key is None and self.parked_key is None


class PersistedLicenseAnchorError(RuntimeError):
    """A persisted licence cannot verify against this deployment's anchor.

    Raised at startup, and deliberately FATAL, mirroring the env-var rule
    (FAR-2091): outside debug a configured licence with only the built-in
    development anchor can never verify, so booting would silently run a paying
    customer on Community. The operator must set ``MODULO_LICENSE_PUBLIC_KEY``.
    """


#: The persisted state this process last observed; ``None`` = never observed.
#: Used to detect a change made by another process (an install or a removal)
#: without re-verifying an unchanged key on every refresh.
_persisted_state: PersistedLicenseState | None = None

#: Monotonic time of the last persisted-state read attempt (TTL throttle).
_persisted_state_checked_at: float = 0.0

_log = logging.getLogger(__name__)


def _validate_public_key_hex(hex_key: str) -> None:
    if len(hex_key) != 64:
        msg = f"License public key must be 64 hex chars, got {len(hex_key)}"
        raise ValueError(msg)
    try:
        bytes.fromhex(hex_key)
    except ValueError as exc:
        raise ValueError(f"License public key is not valid hex: {exc}") from exc


# Validate key format at module load time.
_validate_public_key_hex(_BUILTIN_DEV_PUBLIC_KEY_HEX)


def _get_effective_public_key() -> str:
    """Return the configured key if set, otherwise fall back to the built-in key."""
    return _configured_public_key_hex if _configured_public_key_hex is not None else _LICENSE_PUBLIC_KEY_HEX


def using_builtin_dev_anchor() -> bool:
    """Whether verification is effectively using the built-in DEVELOPMENT anchor.

    ``True`` when no production anchor is configured, or when the configured
    key IS the development key. A production (Farnalabs-signed) licence cannot
    verify against that anchor, so a signature failure is an ANCHOR mismatch
    rather than a malformed/corrupt key.
    """
    return _get_effective_public_key() == _LICENSE_PUBLIC_KEY_HEX


def set_public_key(hex_key: str) -> None:
    global _configured_public_key_hex
    _validate_public_key_hex(hex_key)
    _configured_public_key_hex = hex_key


def is_dev_anchor_in_use() -> bool:
    """Whether the effective verification key is the BUILT-IN development anchor.

    True both when nothing is configured (the fall-back in
    ``_get_effective_public_key``) and when the development key has been
    configured explicitly — the two are the same trust decision.
    """
    return _get_effective_public_key() == _LICENSE_PUBLIC_KEY_HEX


def set_dev_anchor_trusted(allowed: bool) -> None:
    """Allow (debug) or refuse (everything else) the built-in development anchor.

    Called once at startup with ``settings.debug``. Refusing is the DEFAULT, so
    a process that never calls this inherits the secure behaviour.
    """
    global _dev_anchor_trusted
    _dev_anchor_trusted = allowed


@dataclass
class LicenseData:
    tier: str
    features: list[str]
    expires_at: str
    org_id: str
    raw_payload: dict[str, Any] = field(repr=False)
    raw_key: str = field(repr=False)
    #: How this licence renews (``"auto"`` | ``"manual"``). A key minted before
    #: FAR-2075 carries no claim and resolves to ``"manual"``.
    renewal_mode: str = DEFAULT_RENEWAL_MODE
    #: Licence term in MONTHS — the unit ``license_signing._add_months`` uses to
    #: derive ``expires_at``, so a renewal can compute the next expiry from it.
    #: ``None`` for a key minted before FAR-2075.
    term: int | None = None
    #: Opaque, stable identifier for this licence. A renewal is tied to the
    #: licence it replaces by this value. ``None`` for a key minted before
    #: FAR-2075.
    licence_id: str | None = None
    #: ISO 8601 mint timestamp (informational). ``None`` for a pre-FAR-2075 key.
    issued_at: str | None = None
    #: ISO 8601, timezone-aware instant this licence becomes ACTIVE (FAR-2146).
    #: ``None`` when the key carries no claim — such a key is valid from
    #: ``issued_at``, exactly as before FAR-2146 (never gated on activation).
    starts_at: str | None = None


def _parse_renewal_mode(payload: dict[str, Any]) -> str:
    """Read the signed ``renewal_mode`` claim, defaulting to ``"manual"``.

    A missing claim is the documented OLD-KEY case and is not an error. An
    unknown value is a signing-side defect (or a tampered-but-signed payload),
    so it is logged and treated as ``"manual"`` — the safe direction: it can
    never claim an automatic renewal that has not been bought.
    """
    raw = payload.get("renewal_mode")
    if isinstance(raw, str) and raw in RENEWAL_MODES:
        return raw
    if raw is not None:
        _log.warning("license.unrecognised_renewal_mode value=%r", raw)
    return DEFAULT_RENEWAL_MODE


def _parse_term(payload: dict[str, Any]) -> int | None:
    """Read the signed ``term`` claim (months), or ``None`` when absent/invalid."""
    raw = payload.get("term")
    # ``bool`` is an ``int`` subclass in Python; JSON ``true`` is not a term.
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    if raw is not None:
        _log.warning("license.unrecognised_term value=%r", raw)
    return None


def _parse_optional_str(payload: dict[str, Any], claim: str) -> str | None:
    """Read an optional non-empty string claim, or ``None`` when absent/invalid."""
    raw = payload.get(claim)
    if isinstance(raw, str) and raw:
        return raw
    if raw is not None:
        _log.warning("license.unrecognised_claim claim=%s value=%r", claim, raw)
    return None


#: Machine-readable failure reasons carried on :class:`LicenseValidation`.
#:
#: They are a STABLE API for the install path and the admin UI: the human
#: ``error``/``hint`` prose may change, the ``reason`` must not.
REASON_MALFORMED = "malformed"
REASON_SIGNATURE_MISMATCH = "signature_mismatch"
REASON_ANCHOR_MISMATCH = "anchor_mismatch"
REASON_DEV_ANCHOR_NOT_TRUSTED = "dev_anchor_not_trusted"
REASON_EXPIRED = "expired"

_ANCHOR_MISMATCH_HINT = (
    "This deployment is verifying licences with the built-in development anchor, which cannot "
    "verify a production licence. Set MODULO_LICENSE_PUBLIC_KEY to the Ed25519 public key "
    "Farnalabs issued with your licence (anchor mismatch)."
)
_SIGNATURE_MISMATCH_HINT = (
    "The key was not signed by the authority this deployment trusts. If Farnalabs issued it, "
    "MODULO_LICENSE_PUBLIC_KEY does not match the signing keypair (anchor mismatch)."
)


@dataclass
class LicenseValidation:
    valid: bool
    license_data: LicenseData | None = None
    error: str | None = None
    #: Machine-readable reason (one of the ``REASON_*`` constants) when invalid.
    reason: str | None = None
    #: Actionable operator guidance for an invalid key.
    hint: str | None = None


class LicenseError(ValueError): ...


def _decode_license_key(key: str) -> tuple[bytes, bytes]:
    try:
        payload_b64, sig_b64 = key.split(".", 1)
    except ValueError as exc:
        raise LicenseError("Invalid license key format: expected <payload>.<signature>") from exc

    def _pad(b64: str) -> str:
        return b64 + "=" * (-len(b64) % 4)

    try:
        payload_bytes = base64.urlsafe_b64decode(_pad(payload_b64))
        sig_bytes = base64.urlsafe_b64decode(_pad(sig_b64))
    except Exception as exc:
        raise LicenseError(f"Invalid base64 encoding: {exc}") from exc

    return payload_bytes, sig_bytes


def _expiry_failure(expires_at: str) -> tuple[str, str] | None:
    """Return ``(error, reason)`` when the licence's CAPABILITY has lapsed.

    Capability expiry is ``expires_at + LICENCE_GRACE_DAYS``, not
    ``expires_at``: the fixed delivery grace exists so a renewal key can arrive
    after the old key's expiry without stranding the instance. Within the grace
    window the key still grants its tier; past it, it grants nothing.
    """
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp + timedelta(days=LICENCE_GRACE_DAYS) < datetime.now(exp.tzinfo):
            return ("License has expired", REASON_EXPIRED)
    except ValueError:
        return (f"Invalid expires_at format: {expires_at}", REASON_MALFORMED)
    return None


def _check_expired(expires_at: str) -> str | None:
    """Return an error when the licence's CAPABILITY has lapsed (see :func:`_expiry_failure`)."""
    failure = _expiry_failure(expires_at)
    return failure[0] if failure is not None else None


def _starts_at_failure(payload: dict[str, Any], expires_at: str) -> str | None:
    """Return an error when the signed ``starts_at`` claim is unusable.

    ABSENT is the documented OLD-KEY case (and the normal immediate-start
    case) and is not an error — the licence is then valid from ``issued_at``,
    exactly as before FAR-2146.

    A PRESENT claim is part of the signed window, so it must be a
    timezone-aware ISO 8601 instant no later than ``expires_at``. Anything else
    is a signing-side defect (or a tampered-but-validly-signed payload) and is
    rejected rather than silently ignored: ignoring it would activate a
    licence before the window the customer chose, granting time that was not
    paid for.
    """
    raw = payload.get("starts_at")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        return f"Invalid starts_at claim: {raw!r}"
    try:
        start = datetime.fromisoformat(raw)
    except ValueError:
        return f"Invalid starts_at format: {raw}"
    if start.tzinfo is None:
        return f"starts_at must be timezone-aware: {raw}"
    if not expires_at:
        return None
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        # Reported by ``_expiry_failure`` with its own message.
        return None
    if expiry.tzinfo is None:
        expiry = expiry.astimezone()
    if start > expiry:
        return f"starts_at {raw} is after expires_at {expires_at}"
    return None


def parse_and_verify(key: str) -> LicenseValidation:
    try:
        payload_bytes, sig_bytes = _decode_license_key(key)
    except LicenseError as exc:
        return LicenseValidation(valid=False, error=str(exc), reason=REASON_MALFORMED)

    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return LicenseValidation(
            valid=False,
            error=f"Invalid JSON payload: {exc}",
            reason=REASON_MALFORMED,
        )

    if not isinstance(payload, dict):
        return LicenseValidation(
            valid=False,
            error="Payload must be a JSON object",
            reason=REASON_MALFORMED,
        )

    sig_hex = sig_bytes.hex()

    # Verify the signature BEFORE applying the built-in-dev-anchor refusal, so
    # the two distinct failures stay distinguishable outside debug:
    #
    # - a key signed by some OTHER keypair (a production key with no anchor
    #   configured) fails verification, and the cause is the ANCHOR, not the
    #   key: reported as an anchor mismatch with the fix (FAR-2091);
    # - a key that VERIFIES against the built-in development anchor was signed
    #   with a private half that ships with the product, and must never
    #   activate a licence on a real deployment: refused (FAR-2061).
    #
    # Refusing before the signature check conflated the two and reported every
    # production key as a generic configuration refusal with no classification.
    if not verify_signature(payload, sig_hex, _get_effective_public_key()):
        if using_builtin_dev_anchor():
            return LicenseValidation(
                valid=False,
                error="Signature verification failed",
                reason=REASON_ANCHOR_MISMATCH,
                hint=_ANCHOR_MISMATCH_HINT,
            )
        return LicenseValidation(
            valid=False,
            error="Signature verification failed",
            reason=REASON_SIGNATURE_MISMATCH,
            hint=_SIGNATURE_MISMATCH_HINT,
        )

    if is_dev_anchor_in_use() and not _dev_anchor_trusted:
        _log.error("license.dev_anchor_verification_refused")
        return LicenseValidation(
            valid=False,
            error=DEV_ANCHOR_NOT_TRUSTED_ERROR,
            reason=REASON_DEV_ANCHOR_NOT_TRUSTED,
        )

    tier = payload.get("tier", "community")
    features = payload.get("features", [])
    expires_at = payload.get("expires_at", "")
    org_id = payload.get("org_id", "")

    starts_at_error = _starts_at_failure(payload, expires_at)
    if starts_at_error is not None:
        return LicenseValidation(valid=False, error=starts_at_error, reason=REASON_MALFORMED)

    if expires_at:
        failure = _expiry_failure(expires_at)
        if failure is not None:
            error, reason = failure
            return LicenseValidation(valid=False, error=error, reason=reason)

    data = LicenseData(
        tier=tier,
        features=features,
        expires_at=expires_at,
        org_id=org_id,
        raw_payload=payload,
        raw_key=key,
        renewal_mode=_parse_renewal_mode(payload),
        term=_parse_term(payload),
        licence_id=_parse_optional_str(payload, "licence_id"),
        issued_at=_parse_optional_str(payload, "issued_at"),
        starts_at=_parse_optional_str(payload, "starts_at"),
    )

    return LicenseValidation(valid=True, license_data=data)


def _starts_at_instant(data: LicenseData) -> datetime | None:
    """The licence's signed start as an aware instant, or ``None``.

    ``None`` covers both "no claim" (an old key, or an immediate-start key) and
    an unparseable claim. ``parse_and_verify`` rejects the latter, so in
    practice only hand-built ``LicenseData`` reaches the second case; it is
    logged and treated as "starts now", the pre-FAR-2146 behaviour, rather than
    parking a licence forever on a value a real key can never carry.
    """
    if not data.starts_at:
        return None
    try:
        start = datetime.fromisoformat(data.starts_at)
    except ValueError:
        _log.warning("license.unparseable_starts_at value=%r", data.starts_at)
        return None
    # A naive claim is rejected by ``parse_and_verify``; read it as LOCAL time
    # (core's convention for a naive ``expires_at``) if one reaches here.
    return start if start.tzinfo is not None else start.astimezone()


def is_parked(data: LicenseData) -> bool:
    """Whether ``data`` is VALID but not yet ACTIVE (its start is in the future).

    The single definition of "parked", used by :func:`store_license` to decide
    the slot and by the admin licence view to tell an operator that an install
    took effect as a successor rather than as the active licence.
    """
    start = _starts_at_instant(data)
    return start is not None and start > datetime.now(start.tzinfo)


def store_license(_key: str, data: LicenseData) -> None:
    """Install a verified licence: ACTIVE now, or PARKED until its start.

    A key whose signed ``starts_at`` is in the future must not be rejected —
    the customer has legitimately bought a licence that begins later — but it
    must not displace the licence the instance is running on today either. It
    is parked, and :func:`get_license` promotes it when the start arrives.

    A key without ``starts_at`` (every key minted before FAR-2146, and every
    key minted to start immediately) becomes the active licence exactly as
    before.
    """
    global _current_license, _parked_successor
    if is_parked(data):
        _parked_successor = data
        _log.info(
            "license.parked_successor starts_at=%s expires_at=%s",
            data.starts_at,
            data.expires_at,
        )
        return
    _current_license = data


def _promote_due_successor() -> None:
    """Move a parked successor into the active slot once its start has arrived.

    Runs on every :func:`get_license` (and :func:`get_parked_successor`) call,
    so activation needs no operator action and no restart: the next plan
    resolution after ``starts_at`` sees the successor as the active licence.

    Deliberately unconditional once due — the successor REPLACES whatever is
    active, because its window is the one the customer chose.
    """
    global _current_license, _parked_successor
    parked = _parked_successor
    if parked is None or is_parked(parked):
        return
    _current_license = parked
    _parked_successor = None
    _log.info(
        "license.promoted_successor starts_at=%s expires_at=%s",
        parked.starts_at,
        parked.expires_at,
    )


def get_parked_successor() -> LicenseData | None:
    """The verified licence waiting for its start, or ``None``.

    Promotes a due successor first, so a caller can never be told about a
    successor whose start has already arrived — it is the active licence then.
    """
    _promote_due_successor()
    return _parked_successor


def get_license() -> LicenseData | None:
    """Return the in-memory license if present and still within its capability window.

    A parked successor whose ``starts_at`` has arrived is promoted first (no
    operator action). A licence past ``expires_at + LICENCE_GRACE_DAYS`` is
    cleared and resolves to ``None`` (the instance then runs on Community);
    within the grace window it is returned unchanged and keeps granting its
    tier.
    """
    global _current_license

    _promote_due_successor()
    if _current_license is None:
        return None
    if _current_license.expires_at:
        error = _check_expired(_current_license.expires_at)
        if error:
            # Clear ONLY the lapsed active licence. ``clear_license()`` would
            # also discard a parked successor — a paid licence waiting for its
            # start must survive the previous licence lapsing.
            _log.info("license.active_lapsed_expired expires_at=%s", _current_license.expires_at)
            _current_license = None
            return None
    return _current_license


def clear_license() -> None:
    """Forget the installed licence AND any parked successor."""
    global _current_license, _parked_successor
    _current_license = None
    _parked_successor = None


# ---------------------------------------------------------------------------
# Instance-scope persistence (FAR-2153)
#
# The functions below are the ONLY DB access in this module (lazy imports keep
# ``modulo.core.license`` importable without the ORM). They are deliberately
# separate from the memory setters above: a caller persists the target state
# FIRST and installs it into memory only once the write has committed, so a
# failed write can never leave this process believing in a licence that will
# vanish on the next restart.
# ---------------------------------------------------------------------------


def _state_from_value(value: Any) -> PersistedLicenseState:
    """Read a persisted ``SystemConfig.value`` into the two-slot state.

    Tolerates a bare string (an earlier build could have written just the key)
    as the active slot, so a deploy that upgrades across this change still
    restores the licence.
    """
    if isinstance(value, dict):
        active = value.get("active")
        parked = value.get("parked")
        return PersistedLicenseState(
            active_key=active if isinstance(active, str) and active else None,
            parked_key=parked if isinstance(parked, str) and parked else None,
        )
    if isinstance(value, str) and value:
        return PersistedLicenseState(active_key=value)
    return PersistedLicenseState()


def _value_from_state(state: PersistedLicenseState) -> dict[str, str | None]:
    """The JSON value persisted for ``state`` (both slots explicit)."""
    return {"active": state.active_key, "parked": state.parked_key}


async def _session_in_transaction(session: Any) -> bool:
    """Whether ``session`` already owns a transaction (sync, or async double)."""
    in_tx = session.in_transaction()
    if inspect.isawaitable(in_tx):
        in_tx = await in_tx
    return bool(in_tx)


async def _run_in_transaction[T](session: Any, operation: Callable[[], Awaitable[T]]) -> T:
    """Run ``operation`` inside the caller's transaction, or a fresh one.

    ``AsyncSession`` is yielded by the API with ``autobegin=False``, so a bare
    ``execute`` would raise ``InvalidRequestError``; the worker's sessions may
    already be in a transaction (a nested ``session.begin()`` would raise).
    Mirrors ``FeatureFlagRegistry.load_from_db``'s contract.
    """
    if await _session_in_transaction(session):
        return await operation()
    async with session.begin():
        return await operation()


async def read_persisted_license_state(session: Any) -> PersistedLicenseState:
    """Read the persisted instance licence (an EMPTY state when absent).

    Callers must guarantee a transaction (see :func:`_run_in_transaction`).
    """
    from sqlalchemy import select

    from modulo.db.models.system_config import SystemConfig

    result = await session.execute(select(SystemConfig.value).where(SystemConfig.key == SYSTEM_LICENSE_CONFIG_KEY))
    value = result.scalar_one_or_none()
    # A test double may expose the scalar reader asynchronously (the same
    # contract ``FeatureFlagRegistry.load_from_db`` honours for
    # ``in_transaction``); a real ``CursorResult`` reader is synchronous.
    if inspect.isawaitable(value):
        value = await value
    return _state_from_value(value)


async def _write_persisted_license_state(session: Any, state: PersistedLicenseState) -> None:
    """Upsert the persisted instance licence (last-writer-wins; a deliberate write)."""
    from sqlalchemy import select

    from modulo.db.models.system_config import SystemConfig

    result = await session.execute(select(SystemConfig).where(SystemConfig.key == SYSTEM_LICENSE_CONFIG_KEY))
    row = result.scalar_one_or_none()
    value = _value_from_state(state)
    if row is None:
        session.add(SystemConfig(key=SYSTEM_LICENSE_CONFIG_KEY, value=value))
    else:
        row.value = value
    await session.flush()


def _install_persisted_state(state: PersistedLicenseState) -> None:
    """Record the state this process has just persisted (avoids an immediate re-read)."""
    global _persisted_state, _persisted_state_checked_at
    _persisted_state = state
    _persisted_state_checked_at = time.monotonic()


async def persist_installed_system_license(session: Any, raw_key: str, *, parked: bool) -> PersistedLicenseState:
    """Persist an operator install of the instance-scope licence.

    ``parked`` is the install's ``is_parked`` verdict. A PARKED (future-start)
    successor is written to the parked slot and PRESERVES the active slot — the
    licence the instance runs on today must survive the deploy that precedes its
    successor's start. An immediately-active install replaces the ACTIVE slot;
    it preserves a parked successor, because :func:`store_license` leaves that
    slot alone and the persisted state must equal what memory will hold.

    Writes in the caller's transaction when there is one, else its own; the
    process's observed state is updated only after the write returns, so a
    failed commit leaves memory untouched (the route persists BEFORE it calls
    :func:`store_license`).
    """
    if parked:
        active_key = _current_license.raw_key if _current_license is not None else None
        state = PersistedLicenseState(active_key=active_key, parked_key=raw_key)
    else:
        parked_key = _parked_successor.raw_key if _parked_successor is not None else None
        state = PersistedLicenseState(active_key=raw_key, parked_key=parked_key)

    async def _write() -> None:
        await _write_persisted_license_state(session, state)

    await _run_in_transaction(session, _write)
    _install_persisted_state(state)
    return state


async def clear_persisted_system_license(session: Any) -> bool:
    """Delete the persisted instance licence; returns whether a row existed.

    Idempotent: a missing row is not an error (the licence may never have been
    persisted, or another process may already have removed it).
    """
    from sqlalchemy import delete, select

    from modulo.db.models.system_config import SystemConfig

    async def _delete() -> bool:
        existing = await session.execute(select(SystemConfig.id).where(SystemConfig.key == SYSTEM_LICENSE_CONFIG_KEY))
        if existing.scalar_one_or_none() is None:
            return False
        await session.execute(delete(SystemConfig).where(SystemConfig.key == SYSTEM_LICENSE_CONFIG_KEY))
        return True

    removed = await _run_in_transaction(session, _delete)
    _install_persisted_state(PersistedLicenseState())
    return removed


def _verified_or_current(raw_key: str | None, current: LicenseData | None) -> LicenseData | None:
    """Verify ``raw_key``; on failure KEEP the slot this process already has.

    A persisted key that no longer verifies (an anchor change, a corrupt value)
    must not be treated as "the operator removed the licence": dropping
    capability is a silent downgrade, while keeping the last verified licence is
    the safe direction. Absence (``raw_key is None``) is the explicit-removal
    signal and does clear the slot.
    """
    if raw_key is None:
        return None
    if current is not None and current.raw_key == raw_key:
        return current
    data = verify_license_key(raw_key)
    if data is None:
        _log.warning("license.persisted_key_unverified")
        return current
    return data


def restore_license_slots(active: LicenseData | None, parked: LicenseData | None) -> None:
    """Set both in-memory licence slots from a persisted state.

    The explicit two-slot setter the restore/refresh path needs: :func:`store_license`
    can only place a key in a slot, so it cannot express "the parked successor
    was removed while the active licence stays".
    """
    global _current_license, _parked_successor
    _current_license = active
    _parked_successor = parked


def _apply_persisted_state(state: PersistedLicenseState) -> bool:
    """Apply a non-empty persisted state to memory; returns whether anything changed."""
    active = _verified_or_current(state.active_key, _current_license)
    parked = _verified_or_current(state.parked_key, _parked_successor)
    if active is _current_license and parked is _parked_successor:
        return False
    restore_license_slots(active, parked)
    return True


def _adopt_persisted_state(state: PersistedLicenseState) -> bool:
    """Reconcile memory with an observed persisted state.

    Only a TRANSITION from a known non-empty state to empty clears memory. That
    is what keeps a licence installed directly through :func:`store_license`
    (a unit test, or any future non-persisting caller) alive when the store has
    never held one: the first observation of "empty" cannot be distinguished
    from "never persisted", so it must not destroy capability.
    """
    global _persisted_state
    previous = _persisted_state
    if state == previous:
        return False
    if state.is_empty():
        cleared = previous is not None and not previous.is_empty()
        if cleared:
            clear_license()
            _log.info("license.persisted_removed")
        _persisted_state = state
        return cleared
    changed = _apply_persisted_state(state)
    _persisted_state = state
    if changed:
        _log.info(
            "license.persisted_state_applied active=%s parked=%s",
            state.active_key is not None,
            state.parked_key is not None,
        )
    return changed


async def restore_persisted_system_license(session: Any, settings: Any | None = None) -> PersistedLicenseState:
    """Restore the persisted instance licence into this process at startup.

    An empty store leaves memory alone (a fresh process has none, and a SAME-process
    re-run must not wipe a directly-installed licence). A non-empty store is applied.

    Outside debug, a persisted licence combined with only the built-in
    DEVELOPMENT anchor raises :class:`PersistedLicenseAnchorError` — the same
    fail-closed rule ``MODULO_LICENSE_KEY`` already has, applied to the licence
    an operator installed at runtime.
    """
    global _persisted_state, _persisted_state_checked_at
    state = await read_persisted_license_state(session)
    if (
        settings is not None
        and not bool(getattr(settings, "debug", False))
        and using_builtin_dev_anchor()
        and not state.is_empty()
    ):
        raise PersistedLicenseAnchorError(DEV_ANCHOR_STARTUP_ERROR)
    if state.is_empty():
        if _persisted_state is not None and not _persisted_state.is_empty():
            clear_license()
    else:
        _apply_persisted_state(state)
    _persisted_state = state
    _persisted_state_checked_at = time.monotonic()
    return state


def persisted_license_refresh_due() -> bool:
    """Whether the TTL window has elapsed since the last persisted-state read.

    Lets a caller that would otherwise open a DB session on a hot path (the
    worker's per-job hook) skip the session entirely when the in-memory slot is
    still fresh. :func:`refresh_persisted_system_license` re-checks the window,
    so a race between the two is a redundant read at worst.
    """
    if _persisted_state_checked_at <= 0.0:
        return True
    return (time.monotonic() - _persisted_state_checked_at) >= SYSTEM_LICENSE_REFRESH_TTL_SECONDS


async def refresh_persisted_system_license(session: Any, *, force: bool = False) -> bool:
    """Re-read the persisted instance licence, TTL-throttled.

    The cross-process visibility mechanism: a licence installed (or removed) by
    ANOTHER process becomes visible here within ``SYSTEM_LICENSE_REFRESH_TTL_SECONDS``
    without a restart. Reads are fail-open — a DB blip keeps the current memory
    and is logged — because a licence cache miss must degrade to the last known
    tier, never to an error on every plan resolution.

    Returns whether memory changed.
    """
    global _persisted_state_checked_at
    now = time.monotonic()
    # Throttle on the attempt timestamp alone (not on the observed state): a
    # persistently failing read must also honour the window, otherwise every
    # plan resolution would retry AND log a traceback.
    if (
        not force
        and _persisted_state_checked_at > 0.0
        and (now - _persisted_state_checked_at) < (SYSTEM_LICENSE_REFRESH_TTL_SECONDS)
    ):
        return False
    # Advance the throttle BEFORE the read so a persistently failing read is
    # retried once per window, not once per call.
    _persisted_state_checked_at = now
    try:
        state = await _run_in_transaction(session, lambda: read_persisted_license_state(session))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("license.persisted_state_read_failed", exc_info=True)
        return False
    return _adopt_persisted_state(state)


def verify_license_key(key: str) -> LicenseData | None:
    """Return the parsed licence when ``key`` verifies, else ``None``.

    The synchronous, session-free per-key form of the canonical
    licence-ladder check (``core.feature_flags.resolve_effective_license``,
    whose key rungs share this fall-through contract via
    ``_verified_license_key``): an absent key, a bad signature, a licence
    lapsed past its fixed delivery grace (``LICENCE_GRACE_DAYS``), a licence
    that would only verify against the built-in development anchor outside
    debug (FAR-2061), or any unexpected parse failure all resolve to ``None``
    so the caller falls through to the next rung (or the unlicensed default).
    A key is NEVER trusted on mere presence, and a key inside its grace window
    is NOT treated as lapsed.
    """
    if not key:
        return None
    try:
        validation = parse_and_verify(key)
    except Exception:
        _log.warning("license.key_verify_error", exc_info=True)
        return None
    if validation.valid and validation.license_data is not None:
        return validation.license_data
    return None


def check_production_public_key(settings: Any) -> None:
    """Log a CRITICAL warning when the built-in DEVELOPMENT anchor is used outside debug.

    The built-in key is a real development keypair's public half, NOT the
    production anchor, so a Farnalabs-issued licence cannot verify against it.
    Startup refuses to boot when a licence is configured without an anchor
    (see ``api.main._configure_license_and_otel``); this check is the backstop
    for a Community instance that later receives a licence at runtime.

    Advisory only: the enforcing gate is ``parse_and_verify``, which refuses any
    licence that would only verify against this anchor unless a process has
    opted in through ``set_dev_anchor_trusted``.
    """
    if using_builtin_dev_anchor() and not getattr(settings, "debug", False):
        _log.critical(
            "LICENSE_CRITICAL: the built-in DEVELOPMENT licence anchor is in use outside debug. "
            "Production licences are signed by a different keypair and will FAIL verification. "
            "Set MODULO_LICENSE_PUBLIC_KEY to the public key Farnalabs issued with your licence.",
        )

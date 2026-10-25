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
"""

from __future__ import annotations

import base64
import json
import logging
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

import asyncio
import logging
import re
import smtplib
import time
import uuid
from collections.abc import Callable
from email.message import EmailMessage

from modulo.settings import Settings

_log = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY = 1.0
_REDACTED = "********"
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120

# Pragmatic SMTP-recipient shape check (no third-party validator dependency).
# Requires exactly one ``@``, a non-empty local part, and a domain containing a
# dot and no leading/trailing dot or consecutive dots.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


class EmailSendingError(Exception):
    pass


class EmailSendLimiter:
    """In-memory fixed-window test-send budget, keyed per organisation.

    The test-send endpoint relays mail to an arbitrary recipient, so it is a
    potential abuse vector for SMTP relay enumeration (PRD §8.11 / the product
    map's "test-send relay abuse" gap). This limiter caps how often an org may
    fire a test email: at most ``limit`` sends per ``window_seconds``, enforced
    with a per-org ``asyncio.Lock`` so concurrent sends cannot overdraw the
    budget. The state is deliberately in-memory and per-process — consistent
    with the other admin cooldowns in this codebase — and only ever *blocks*
    (a limiter failure fails open, it never breaks a legitimate test-send).

    ``now_fn`` is injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        limit: int = 3,
        window_seconds: int = 3600,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        self.limit = limit
        self.window_seconds = window_seconds
        self._now = now_fn or time.monotonic
        self._buckets: dict[uuid.UUID, tuple[float, int]] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    def _lock_for(self, org_id: uuid.UUID) -> asyncio.Lock:
        lock = self._locks.get(org_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[org_id] = lock
        return lock

    async def acquire(self, org_id: uuid.UUID) -> int:
        """Consume one test-send slot for *org_id*.

        Returns the number of seconds until a slot frees when the budget is
        exhausted, or ``0`` when the send may proceed.
        """
        lock = self._lock_for(org_id)
        async with lock:
            now = self._now()
            window_start, count = self._buckets.get(org_id, (now, 0))
            if now - window_start >= self.window_seconds:
                window_start = now
                count = 0
            if count >= self.limit:
                return int(self.window_seconds - (now - window_start)) + 1
            self._buckets[org_id] = (window_start, count + 1)
            return 0

    def reset(self, org_id: uuid.UUID | None = None) -> None:
        """Clear the budget for one org (or every org when ``None``)."""
        if org_id is None:
            self._buckets.clear()
            self._locks.clear()
        else:
            self._buckets.pop(org_id, None)
            self._locks.pop(org_id, None)


def _is_valid_recipient(address: str) -> bool:
    """Reject malformed or header-injecting test-send recipients.

    The address must be a bare RFC-5322-shaped ``local@domain.tld`` string:
    no angle brackets, no display names, no CR/LF (header injection) and no
    obviously non-email garbage such as URLs or shell metacharacters.
    """
    if not isinstance(address, str) or not address.strip():
        return False
    candidate = address.strip()
    if len(candidate) > 320:
        return False
    if any(ch in candidate for ch in ("\r", "\n", "<", ">", '"', "(", ")", ",", ";")):
        return False
    if candidate.startswith(("https://", "mailto:", "/", "\\", "@")):
        return False
    return _EMAIL_RE.fullmatch(candidate) is not None


def _effective_timeout(settings: object) -> int:
    """Resolve the SMTP timeout from a settings-like object.

    Callers may pass either the app ``Settings`` or a minimal object built from
    org-level email configuration. A missing, non-numeric, or out-of-range value
    falls back to the 30-second default so a malformed org override can never
    break email sending.
    """
    value = getattr(settings, "smtp_timeout", _DEFAULT_TIMEOUT)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return _DEFAULT_TIMEOUT if timeout < 1 else min(timeout, _MAX_TIMEOUT)


def _redact_credentials(message: str, settings: Settings) -> str:
    """Strip configured SMTP credentials from an error message.

    SMTP servers sometimes echo the attempted username (or worse, the AUTH
    command) inside their error responses. Since those strings flow straight
    into ``EmailSendingError`` and callers' logs, redact any configured secret
    before it leaves this module.
    """
    for secret in (settings.smtp_username, settings.smtp_password):
        if secret:
            message = message.replace(secret, _REDACTED)
    return message


def send_email(
    settings: Settings,
    to: list[str],
    subject: str,
    body_html: str,
    body_text: str | None = None,
) -> bool:
    if not settings.smtp_host:
        _log.warning("email.disabled_no_smtp_host")
        return False

    if not to:
        _log.warning("email.no_recipients")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = ", ".join(to)

    if body_text:
        msg.set_content(body_text)
    else:
        msg.set_content(re.sub("<[^<]+?>", "", body_html).strip())

    msg.add_alternative(body_html, subtype="html")

    last_exc: smtplib.SMTPException | OSError | None = None

    timeout = _effective_timeout(settings)

    for attempt in range(_MAX_RETRIES + 1):
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout) as server:
                server.starttls()
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(msg)
            _log.info("email.sent", extra={"to": to, "subject": subject})
            return True
        except OSError as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                _log.warning(
                    "email.send_retry",
                    extra={
                        "to": to,
                        "subject": subject,
                        "attempt": attempt + 1,
                        "error": _redact_credentials(str(exc), settings),
                    },
                )
                time.sleep(_RETRY_DELAY)
                continue

    _log.error(
        "email.send_failed",
        extra={
            "to": to,
            "subject": subject,
            "error": _redact_credentials(str(last_exc), settings),
        },
    )
    raise EmailSendingError(_redact_credentials(str(last_exc), settings)) from last_exc

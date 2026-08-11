import logging
import re
import smtplib
import time
from email.message import EmailMessage

from modulo.settings import Settings

_log = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY = 1.0
_REDACTED = "********"
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120


class EmailSendingError(Exception):
    pass


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
        except (smtplib.SMTPException, OSError) as exc:
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

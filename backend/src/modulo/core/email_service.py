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


class EmailSendingError(Exception):
    pass


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

    for attempt in range(_MAX_RETRIES + 1):
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
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

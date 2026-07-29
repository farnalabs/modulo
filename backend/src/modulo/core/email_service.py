import logging
import smtplib
import time
from email.message import EmailMessage

from modulo.settings import Settings

_log = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY = 1.0


class EmailSendingError(Exception):
    pass


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

    msg.add_alternative(body_html, subtype="html")

    last_exc: smtplib.SMTPException | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
                server.starttls()
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(msg)
            _log.info("email.sent", extra={"to": to, "subject": subject})
            return True
        except smtplib.SMTPException as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                _log.warning(
                    "email.send_retry",
                    extra={"to": to, "subject": subject, "attempt": attempt + 1, "error": str(exc)},
                )
                time.sleep(_RETRY_DELAY)
                continue

    _log.error("email.send_failed", extra={"to": to, "subject": subject, "error": str(last_exc)})
    raise EmailSendingError(str(last_exc)) from last_exc

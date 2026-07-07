import logging
import smtplib
from email.message import EmailMessage

from modulo.settings import Settings

_log = logging.getLogger(__name__)


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

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = ", ".join(to)

    if body_text:
        msg.set_content(body_text)

    msg.add_alternative(body_html, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
        _log.info("email.sent", extra={"to": to, "subject": subject})
        return True
    except smtplib.SMTPException as exc:
        _log.error("email.send_failed", extra={"to": to, "subject": subject, "error": str(exc)})
        raise EmailSendingError(str(exc)) from exc

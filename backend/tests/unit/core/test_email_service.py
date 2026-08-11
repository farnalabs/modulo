"""Tests for email_service — SMTP email sending via stdlib smtplib."""

from unittest.mock import MagicMock, patch

import pytest

from modulo.core.email_service import EmailSendingError, send_email


class MockSettings:
    smtp_host = "smtp.example.com"
    smtp_port = 587
    smtp_username = "user"
    smtp_password = "pass"
    email_from = "noreply@example.com"


class MockSettingsNoSMTP:
    smtp_host = ""
    smtp_port = 587
    smtp_username = ""
    smtp_password = ""
    email_from = ""


class TestSendEmail:
    def test_send_email_success(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                settings,
                to=["admin@example.com"],
                subject="Test Subject",
                body_html="<html><body><h1>Test</h1></body></html>",
                body_text="Test",
            )

            assert result is True
            mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)
            mock_server.starttls.assert_called_once()
            mock_server.login.assert_called_once_with("user", "pass")
            mock_server.send_message.assert_called_once()
            msg = mock_server.send_message.call_args[0][0]
            assert msg["Subject"] == "Test Subject"
            assert msg["From"] == "noreply@example.com"
            assert msg["To"] == "admin@example.com"

    def test_send_email_no_body_text(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                settings,
                to=["admin@example.com"],
                subject="Test",
                body_html="<html><body><h1>Test</h1></body></html>",
            )

            assert result is True
            mock_server.send_message.assert_called_once()

    def test_send_email_no_auth(self) -> None:
        settings = MockSettings()
        settings.smtp_username = ""
        settings.smtp_password = ""
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                settings,
                to=["admin@example.com"],
                subject="Test",
                body_html="<html><body><h1>Test</h1></body></html>",
            )

            assert result is True
            mock_server.starttls.assert_called_once()
            mock_server.login.assert_not_called()

    def test_send_email_multiple_recipients(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                settings,
                to=["a@example.com", "b@example.com"],
                subject="Test",
                body_html="<html><body><h1>Test</h1></body></html>",
            )

            assert result is True
            msg = mock_server.send_message.call_args[0][0]
            assert msg["To"] == "a@example.com, b@example.com"

    def test_send_email_disabled_no_smtp_host(self) -> None:
        settings = MockSettingsNoSMTP()
        result = send_email(
            settings,
            to=["admin@example.com"],
            subject="Test",
            body_html="<html><body><h1>Test</h1></body></html>",
        )
        assert result is False

    def test_send_email_smtp_failure(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.send_message.side_effect = __import__(
                "smtplib"
            ).SMTPException("Connection refused")

            with pytest.raises(EmailSendingError, match="Connection refused"):
                send_email(
                    settings,
                    to=["admin@example.com"],
                    subject="Test",
                    body_html="<html><body><h1>Test</h1></body></html>",
                )

    def test_send_email_oserror_network_failure_retried_and_wrapped(self) -> None:
        """OSError (connection refused, DNS, timeout) must be retried and wrapped
        in EmailSendingError — previously such failures escaped uncaught because
        only smtplib.SMTPException was handled."""
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.send_message.side_effect = ConnectionRefusedError(
                111, "Connection refused"
            )

            with pytest.raises(EmailSendingError, match="Connection refused"):
                send_email(
                    settings,
                    to=["admin@example.com"],
                    subject="Test",
                    body_html="<html><body><h1>Test</h1></body></html>",
                )

            # _MAX_RETRIES + 1 = 3 attempts total
            assert mock_smtp.return_value.__enter__.return_value.send_message.call_count == 3

    def test_send_email_timeout_retried_and_wrapped(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.send_message.side_effect = TimeoutError("timed out")

            with pytest.raises(EmailSendingError, match="timed out"):
                send_email(
                    settings,
                    to=["admin@example.com"],
                    subject="Test",
                    body_html="<html><body><h1>Test</h1></body></html>",
                )

            assert mock_smtp.return_value.__enter__.return_value.send_message.call_count == 3

    def test_send_email_transient_network_error_then_success(self) -> None:
        """A transient OSError on the first attempt must not abort — the retry
        succeeds on the next attempt."""
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server
            mock_server.send_message.side_effect = [
                TimeoutError("timed out"),
                None,
            ]

            result = send_email(
                settings,
                to=["admin@example.com"],
                subject="Test",
                body_html="<html><body><h1>Test</h1></body></html>",
            )

            assert result is True
            assert mock_server.send_message.call_count == 2

    def test_send_email_auth_failure_redacts_credentials(self) -> None:
        """SMTP auth failures can echo the configured username/password in the
        server response — the raised EmailSendingError must redact them."""
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.send_message.side_effect = __import__(
                "smtplib"
            ).SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted. user failed authentication")

            with pytest.raises(EmailSendingError) as exc_info:
                send_email(
                    settings,
                    to=["admin@example.com"],
                    subject="Test",
                    body_html="<html><body><h1>Test</h1></body></html>",
                )

            message = str(exc_info.value)
            assert "user" not in message
            assert "pass" not in message
            assert "********" in message

    def test_send_email_error_redacts_password_in_network_message(self) -> None:
        """Error detail strings must never contain the SMTP password even when
        the underlying exception embeds it."""
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.send_message.side_effect = __import__(
                "smtplib"
            ).SMTPException("authentication failed for pass")

            with pytest.raises(EmailSendingError) as exc_info:
                send_email(
                    settings,
                    to=["admin@example.com"],
                    subject="Test",
                    body_html="<html><body><h1>Test</h1></body></html>",
                )

            assert "pass" not in str(exc_info.value)
            assert "********" in str(exc_info.value)

    def test_send_email_empty_recipients_returns_false(self) -> None:
        settings = MockSettings()
        result = send_email(
            settings,
            to=[],
            subject="Test",
            body_html="<html><body><h1>Test</h1></body></html>",
        )
        assert result is False

    def test_send_email_empty_subject_still_sends(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                settings,
                to=["admin@example.com"],
                subject="",
                body_html="<html><body><h1>Test</h1></body></html>",
            )

            assert result is True
            msg = mock_server.send_message.call_args[0][0]
            assert msg["Subject"] == ""

    def test_send_email_special_characters_in_subject(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                settings,
                to=["admin@example.com"],
                subject="Test: üñíçödé & <special> chars!",
                body_html="<html><body><h1>Test</h1></body></html>",
                body_text="Test",
            )

            assert result is True
            msg = mock_server.send_message.call_args[0][0]
            assert msg["Subject"] == "Test: üñíçödé & <special> chars!"

    def test_send_email_mime_structure(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                settings,
                to=["admin@example.com"],
                subject="MIME Test",
                body_html="<html><body><h1>HTML</h1></body></html>",
                body_text="Plain text version",
            )

            assert result is True
            msg = mock_server.send_message.call_args[0][0]
            assert msg.is_multipart()
            parts = [p.get_content_type() for p in msg.walk() if p.get_content_maintype() != "multipart"]
            assert "text/plain" in parts
            assert "text/html" in parts

    def test_send_email_mime_structure_html_only(self) -> None:
        settings = MockSettings()
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                settings,
                to=["admin@example.com"],
                subject="HTML Only",
                body_html="<html><body><h1>HTML</h1></body></html>",
            )

            assert result is True
            msg = mock_server.send_message.call_args[0][0]
            assert msg.is_multipart()
            parts = [p.get_content_type() for p in msg.walk() if p.get_content_maintype() != "multipart"]
            assert "text/html" in parts
            assert "text/plain" in parts

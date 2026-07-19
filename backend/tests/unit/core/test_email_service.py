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
    def test_send_email_success(self):
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

    def test_send_email_no_body_text(self):
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

    def test_send_email_no_auth(self):
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

    def test_send_email_multiple_recipients(self):
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

    def test_send_email_disabled_no_smtp_host(self):
        settings = MockSettingsNoSMTP()
        result = send_email(
            settings,
            to=["admin@example.com"],
            subject="Test",
            body_html="<html><body><h1>Test</h1></body></html>",
        )
        assert result is False

    def test_send_email_smtp_failure(self):
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

"""Tests for email_service — SMTP email sending via stdlib smtplib."""

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

from modulo.core.email_service import (
    EmailSendingError,
    EmailSendLimiter,
    _effective_timeout,
    _is_valid_recipient,
    _redact_credentials,
    send_email,
)


class MockSettings:
    smtp_host = "smtp.example.com"
    smtp_port = 587
    smtp_username = "user"
    smtp_password = "pass"
    email_from = "noreply@example.com"
    smtp_timeout = 30


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

    def test_send_email_custom_timeout(self) -> None:
        settings = MockSettings()
        settings.smtp_timeout = 15
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
            mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)

    def test_send_email_timeout_missing_attr_falls_back_to_default(self) -> None:
        class MinimalSettings:
            def __init__(self) -> None:
                self.smtp_host = "smtp.example.com"
                self.smtp_port = 587
                self.smtp_username = ""
                self.smtp_password = ""
                self.email_from = "noreply@example.com"

        settings = MinimalSettings()
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
            mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)

    def test_send_email_timeout_invalid_value_falls_back_to_default(self) -> None:
        settings = MockSettings()
        settings.smtp_timeout = "not-a-number"
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
            mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)

    def test_send_email_timeout_zero_or_huge_clamped(self) -> None:
        settings = MockSettings()
        settings.smtp_timeout = 0
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server
            send_email(
                settings,
                to=["admin@example.com"],
                subject="Test",
                body_html="<html><body><h1>Test</h1></body></html>",
            )
            mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)

        settings.smtp_timeout = 9999
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server
            send_email(
                settings,
                to=["admin@example.com"],
                subject="Test",
                body_html="<html><body><h1>Test</h1></body></html>",
            )
            mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=120)

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
            assert not msg["Subject"]

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

    def test_send_email_logs_disabled_no_smtp_host(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="modulo.core.email_service")

        result = send_email(
            MockSettingsNoSMTP(),
            to=["admin@example.com"],
            subject="Test",
            body_html="<html><body><h1>Test</h1></body></html>",
        )

        assert result is False
        assert any(r.getMessage() == "email.disabled_no_smtp_host" for r in caplog.records)

    def test_send_email_logs_warning_when_no_recipients(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="modulo.core.email_service")

        result = send_email(
            MockSettings(),
            to=[],
            subject="Test",
            body_html="<html><body><h1>Test</h1></body></html>",
        )

        assert result is False
        assert any(r.getMessage() == "email.no_recipients" for r in caplog.records)

    def test_send_email_logs_sent_with_recipients_and_subject(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="modulo.core.email_service")

        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value = MagicMock()

            result = send_email(
                MockSettings(),
                to=["admin@example.com"],
                subject="Test Subject",
                body_html="<html><body><h1>Test</h1></body></html>",
            )

        assert result is True
        sent = [r for r in caplog.records if r.getMessage() == "email.sent"]
        assert len(sent) == 1
        assert sent[0].to == ["admin@example.com"]
        assert sent[0].subject == "Test Subject"

    def test_send_email_logs_send_failed_with_redacted_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """The final failure log must use the redacted error, never the raw
        SMTP message that can echo configured credentials."""
        caplog.set_level(logging.ERROR, logger="modulo.core.email_service")

        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value.send_message.side_effect = __import__(
                "smtplib"
            ).SMTPException("authentication failed for pass")

            with pytest.raises(EmailSendingError):
                send_email(
                    MockSettings(),
                    to=["admin@example.com"],
                    subject="Test",
                    body_html="<html><body><h1>Test</h1></body></html>",
                )

        failed = [r for r in caplog.records if r.getMessage() == "email.send_failed"]
        assert len(failed) == 1
        assert "pass" not in failed[0].error
        assert "********" in failed[0].error

    def test_send_email_logs_retry_warning_with_redacted_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Transient failures must log a retry warning carrying the redacted
        error before the next attempt."""
        caplog.set_level(logging.WARNING, logger="modulo.core.email_service")

        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server
            mock_server.send_message.side_effect = [
                __import__("smtplib").SMTPException("connection failed for pass"),
                None,
            ]

            result = send_email(
                MockSettings(),
                to=["admin@example.com"],
                subject="Test",
                body_html="<html><body><h1>Test</h1></body></html>",
            )

        assert result is True
        retries = [r for r in caplog.records if r.getMessage() == "email.send_retry"]
        assert len(retries) == 1
        assert retries[0].attempt == 1
        assert "pass" not in retries[0].error
        assert "********" in retries[0].error

    def test_send_email_html_fallback_creates_plain_text_body(self) -> None:
        """Without body_text, the plain-text part must be the HTML with tags
        stripped — not an empty body."""
        with patch("modulo.core.email_service.smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_server

            result = send_email(
                MockSettings(),
                to=["admin@example.com"],
                subject="Test",
                body_html="<html><body><h1>Test</h1></body></html>",
            )

            assert result is True
            msg = mock_server.send_message.call_args[0][0]
            parts = [p for p in msg.walk() if p.get_content_maintype() != "multipart"]
            plain = next(p for p in parts if p.get_content_type() == "text/plain")
            html = next(p for p in parts if p.get_content_type() == "text/html")
            assert plain.get_content() == "Test\n"
            assert html.get_content() == "<html><body><h1>Test</h1></body></html>\n"


class TestEffectiveTimeout:
    def test_negative_timeout_clamps_to_default(self) -> None:
        settings = MockSettings()
        settings.smtp_timeout = -5
        assert _effective_timeout(settings) == 30

    def test_none_timeout_falls_back_to_default(self) -> None:
        settings = MockSettings()
        settings.smtp_timeout = None
        assert _effective_timeout(settings) == 30

    def test_float_timeout_is_truncated(self) -> None:
        settings = MockSettings()
        settings.smtp_timeout = 15.7
        assert _effective_timeout(settings) == 15

    def test_lower_bound_one_is_allowed(self) -> None:
        settings = MockSettings()
        settings.smtp_timeout = 1
        assert _effective_timeout(settings) == 1

    def test_max_timeout_cap_applied(self) -> None:
        settings = MockSettings()
        settings.smtp_timeout = 120
        assert _effective_timeout(settings) == 120


class TestRedactCredentials:
    def test_redacts_username_and_password(self) -> None:
        message = _redact_credentials("user logging in with pass", MockSettings())
        assert message == "******** logging in with ********"

    def test_redacts_username_only(self) -> None:
        settings = MockSettings()
        settings.smtp_password = ""
        message = _redact_credentials("auth failed for user", settings)
        assert message == "auth failed for ********"

    def test_redacts_password_only(self) -> None:
        settings = MockSettings()
        settings.smtp_username = ""
        message = _redact_credentials("SMTP AUTH failure: pass rejected", settings)
        assert message == "SMTP AUTH failure: ******** rejected"

    def test_empty_secrets_leave_message_unchanged(self) -> None:
        settings = MockSettings()
        settings.smtp_username = ""
        settings.smtp_password = ""
        message = _redact_credentials("no secrets in here", settings)
        assert message == "no secrets in here"


def _await_acquire(limiter: EmailSendLimiter, org_id: int) -> int:
    """Run ``acquire`` to completion and return the retry-after signal.

    The real contract (and the admin test-send route's) is
    ``acquire() -> int``: ``0`` means the send may proceed, a positive value
    is the seconds until a slot frees.
    """

    async def _run() -> int:
        return await limiter.acquire(org_id)

    return asyncio.run(_run())


class TestEmailSendLimiter:
    async def test_allows_up_to_limit_then_blocks(self) -> None:
        clock = [1000.0]
        limiter = EmailSendLimiter(limit=3, window_seconds=60, now_fn=lambda: clock[0])
        for _ in range(3):
            assert await limiter.acquire(1) == 0
        retry = await limiter.acquire(1)
        assert retry > 0

    def test_blocks_with_retry_after(self) -> None:
        clock = [1000.0]
        limiter = EmailSendLimiter(limit=1, window_seconds=60, now_fn=lambda: clock[0])
        assert limiter._now() == 1000.0
        assert _await_acquire(limiter, 1) == 0
        retry_after = _await_acquire(limiter, 1)
        assert retry_after > 0

    async def test_window_rollover_refills_budget(self) -> None:
        clock = [1000.0]
        limiter = EmailSendLimiter(limit=2, window_seconds=60, now_fn=lambda: clock[0])
        assert await limiter.acquire(1) == 0
        assert await limiter.acquire(1) == 0
        assert await limiter.acquire(1) > 0
        clock[0] += 60
        assert await limiter.acquire(1) == 0

    async def test_orgs_are_isolated(self) -> None:
        limiter = EmailSendLimiter(limit=1, window_seconds=60, now_fn=lambda: 1000.0)
        assert await limiter.acquire(1) == 0
        assert await limiter.acquire(1) > 0
        assert await limiter.acquire(2) == 0

    async def test_concurrent_acquire_never_overdraws(self) -> None:
        clock = [1000.0]
        limiter = EmailSendLimiter(limit=5, window_seconds=3600, now_fn=lambda: clock[0])
        results = await asyncio.gather(*(limiter.acquire(1) for _ in range(10)))
        assert sum(1 for r in results if r == 0) == 5
        assert sum(1 for r in results if r > 0) == 5

    async def test_reset_clears_one_org(self) -> None:
        clock = [1000.0]
        limiter = EmailSendLimiter(limit=1, window_seconds=60, now_fn=lambda: clock[0])
        assert await limiter.acquire(1) == 0
        assert await limiter.acquire(1) > 0
        limiter.reset(1)
        assert await limiter.acquire(1) == 0

    async def test_reset_all_clears_every_org(self) -> None:
        clock = [1000.0]
        limiter = EmailSendLimiter(limit=1, window_seconds=60, now_fn=lambda: clock[0])
        assert await limiter.acquire(1) == 0
        assert await limiter.acquire(2) == 0
        limiter.reset()
        assert await limiter.acquire(1) == 0
        assert await limiter.acquire(2) == 0

    def test_invalid_constructor_args_rejected(self) -> None:
        with pytest.raises(ValueError, match="limit must be >= 1"):
            EmailSendLimiter(limit=0)
        with pytest.raises(ValueError, match="window_seconds must be >= 1"):
            EmailSendLimiter(window_seconds=0)


class TestIsValidRecipient:
    def test_accepts_plain_address(self) -> None:
        assert _is_valid_recipient("admin@example.com")
        assert _is_valid_recipient("user.name+tag@sub.example.co.uk")

    def test_rejects_missing_at(self) -> None:
        assert not _is_valid_recipient("admin")
        assert not _is_valid_recipient("admin@")

    def test_rejects_header_injection(self) -> None:
        assert not _is_valid_recipient("admin@example.com\r\nBcc: victim@example.com")
        assert not _is_valid_recipient("admin@example.com\nBcc: victim@example.com")

    def test_rejects_display_name_and_brackets(self) -> None:
        assert not _is_valid_recipient('"Admin" <admin@example.com>')
        assert not _is_valid_recipient("Admin <admin@example.com>")

    def test_rejects_urls_and_paths(self) -> None:
        assert not _is_valid_recipient("https://example.com")
        assert not _is_valid_recipient("/etc/passwd@example.com")

    def test_rejects_multiple_recipients(self) -> None:
        assert not _is_valid_recipient("a@example.com, b@example.com")
        assert not _is_valid_recipient("a@example.com; b@example.com")

    def test_rejects_empty_and_whitespace(self) -> None:
        assert not _is_valid_recipient("")
        assert not _is_valid_recipient("   ")

    def test_rejects_oversized_address(self) -> None:
        assert not _is_valid_recipient(f"{'a' * 400}@example.com")
